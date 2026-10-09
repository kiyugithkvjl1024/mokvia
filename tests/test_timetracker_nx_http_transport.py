"""Production HTTPS transport tested only against an isolated loopback TLS double."""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from webapp.timetracker_nx import NXError, ResultUnknown, NXAdapter, Journal, Settings
from tests.test_timetracker_nx import MockNX, actual


def profile(**changes):
    return {'version':1,'api_base':'https://localhost/nx/api','allowed_host':'localhost','user_id':'21',
            'timezone':'Asia/Tokyo','granularity':5,'required_categories':[],
            'auth_mode':'api_key','credential_env':'MOKVIA_NX_TEST_CREDENTIAL','company_approved':True,**changes}


class ConfigurationTests(unittest.TestCase):
    def module(self):
        spec=importlib.util.find_spec('webapp.timetracker_nx_transport')
        self.assertIsNotNone(spec,'Production transport module must exist')
        from webapp import timetracker_nx_transport
        return timetracker_nx_transport

    def test_safe_configuration_is_not_a_credential_or_connection(self):
        m=self.module()
        with patch.dict(os.environ,{},clear=True):
            p=m.HTTPProfile.from_dict(profile());self.assertFalse(p.configured())
            with self.assertRaises(NXError) as caught:p.credential()
            self.assertEqual('credential_missing',caught.exception.code)
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'config.json';path.write_text(json.dumps(profile()));path.chmod(0o600)
            with patch.object(http.client.HTTPSConnection,'connect',side_effect=AssertionError('No startup connection')):
                options,services=m.load_integration_configuration(path)
                self.assertEqual('https://localhost/nx/api',options['timetracker_nx']['api_base'])
                self.assertIn('timetracker_nx_transport',services)

    def test_registry_stays_unconnected_without_secret_and_never_authenticates(self):
        from webapp.integrations.registry import Registry
        m=self.module()
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'config.json';path.write_text(json.dumps(profile()));path.chmod(0o600)
            options,services=m.load_integration_configuration(path)
            with patch.dict(os.environ,{},clear=True),patch.object(http.client.HTTPSConnection,'connect',side_effect=AssertionError('No auth')):
                registry=Registry(Path(t),options=options,services=services)
                status=registry.status('timetracker_nx');self.assertTrue(status['enabled']);self.assertFalse(status['configured']);self.assertEqual('unconnected',status['connection_state'])
                with self.assertRaises(ValueError):registry.require('timetracker_nx','actual.write')
            with patch.dict(os.environ,{'MOKVIA_NX_TEST_CREDENTIAL':'synthetic-only-value'}),patch.object(http.client.HTTPSConnection,'connect',side_effect=AssertionError('No startup auth')):
                self.assertTrue(registry.status('timetracker_nx')['configured']);registry.require('timetracker_nx','actual.write')
            self.assertFalse((Path(t)/'.local-state').exists())

    @unittest.skipIf(os.name=='nt','POSIX Docker bind metadata test')
    def test_writable_profile_is_rejected_but_readonly_mount_metadata_is_supported(self):
        from types import SimpleNamespace
        m=self.module()
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'config.json';path.write_text(json.dumps(profile()));path.chmod(0o666)
            with self.assertRaises(NXError):m.load_integration_configuration(path)
            with patch('webapp.timetracker_nx_transport.os.statvfs',return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                options,services=m.load_integration_configuration(path)
                self.assertIn('timetracker_nx',options);self.assertIn('timetracker_nx_transport',services)

    def test_unsafe_or_incomplete_config_is_rejected(self):
        m=self.module()
        for changes in [{'api_base':'http://localhost/nx/api'},{'allowed_host':'evil.invalid'},{'company_approved':False},
                        {'credential_env':'HOME'},{'api_key':'synthetic'}, {'user_id':'21/../22'}, {'auth_mode':'basic'},
                        {'api_base':'https://localhost/nx/api?secret=x'}]:
            with self.subTest(changes=changes),self.assertRaises(NXError):m.HTTPProfile.from_dict(profile(**changes))
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'config.json';path.write_text(json.dumps(profile()));path.chmod(0o600);link=Path(t)/'link';link.symlink_to(path)
            with self.assertRaises(NXError):m.load_integration_configuration(link)


@unittest.skipUnless(shutil.which('openssl'),'OpenSSL CLI required for disposable loopback TLS fixture')
class HTTPSDoubleTests(ConfigurationTests):
    def setUp(self):
        self.module();self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.mock=MockNX();self.mode=None;self.requests=[];self.redirect_hits=0
        cert=self.root/'cert.pem';key=self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
        parent=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.handle_request()
            def do_POST(self):self.handle_request()
            def do_PUT(self):self.handle_request()
            def handle_request(self):
                from urllib.parse import urlsplit,parse_qs
                parent.requests.append((self.command,self.path,dict(self.headers)))
                rawbody=self.rfile.read(int(self.headers.get('Content-Length','0'))) if self.command!='GET' else None
                if self.path.startswith('/redirect-target'):parent.redirect_hits+=1
                mode=parent.mode
                if mode=='timeout':time.sleep(.25)
                if isinstance(mode,int) or mode=='unknown_4xx':
                    self.send_response(mode if isinstance(mode,int) else 400)
                    if mode in (301,302,303,307,308):self.send_header('Location','https://evil.invalid/redirect-target')
                    self.send_header('Content-Type','application/json');self.end_headers()
                    with contextlib.suppress(OSError):self.wfile.write(json.dumps([{'code':'InputTimeEntryLocked' if mode!='unknown_4xx' else 'UnknownServerCode','message':'synthetic-private-response'}]).encode())
                    return
                parsed=urlsplit(self.path);query={k:v[0] for k,v in parse_qs(parsed.query).items()}
                for k in ('offset','limit'):
                    if k in query:query[k]=int(query[k])
                body=json.loads(rawbody) if rawbody is not None else None
                response=parent.mock.request(self.command,parsed.path.removeprefix('/nx/api'),query or None,body)
                if mode=='disconnect' or (mode=='disconnect_post' and self.command=='POST'):self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                payload=b'' if response is None else json.dumps(response).encode()
                if mode=='malformed':payload=b'not-json synthetic-private-response'
                with contextlib.suppress(OSError):self.wfile.write(payload)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);self.server.socket=ctx.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.close)
        self.client_context=ssl.create_default_context(cafile=str(cert));self.profile=self.module().HTTPProfile.from_dict(profile())
        self.env=patch.dict(os.environ,{'MOKVIA_NX_TEST_CREDENTIAL':'synthetic-only-value'});self.env.start();self.addCleanup(self.env.stop)
        self.transport=self.new_transport()

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def new_transport(self,context=None,timeout=2):
        def connection(host,port,*,context,timeout):
            self.assertEqual(host,'localhost');self.assertEqual(port,443)
            return http.client.HTTPSConnection(host,self.server.server_port,context=context,timeout=timeout)
        return self.module().NXHTTPTransport(self.profile,connection_factory=connection,ssl_context=context or self.client_context,timeout=timeout)
    def test_auth_is_lazy_and_me_pins_the_user_and_paths(self):
        self.assertFalse(self.requests);self.assertEqual({'id':'21'},self.transport.request('GET','/system/users/me'))
        self.assertEqual('synthetic-only-value',self.requests[-1][2]['X-TT-ApiKey'])
        self.assertNotIn('synthetic-only-value',repr(self.transport))
        for method,path in [('GET','/system/users/22/timeEntries'),('POST','/system/users/22/timeEntries'),('DELETE','/system/users/21/timeEntries/1'),('GET','https://evil.invalid/api'),('GET','/auth')]:
            with self.assertRaises(NXError):self.transport.request(method,path)
        self.assertEqual(1,len(self.requests))
        self.mock.user='22'
        with self.assertRaises(NXError) as caught:self.transport.request('GET','/system/users/me')
        self.assertEqual('not_self',caught.exception.code)
    def test_bearer_and_missing_credential_do_not_generate_or_save_credentials(self):
        self.profile=self.module().HTTPProfile.from_dict(profile(auth_mode='bearer'));transport=self.new_transport();transport.request('GET','/system/users/me')
        self.assertEqual('Bearer synthetic-only-value',self.requests[-1][2]['Authorization'])
        with patch.dict(os.environ,{},clear=True),self.assertRaises(NXError):transport.request('GET','/system/users/me')
        self.assertEqual(1,len(self.requests))
    def test_redirect_never_follows_and_write_outcome_is_unknown(self):
        self.transport.request('GET','/system/users/me')
        for code in [301,302,303,307,308]:
            self.mode=code
            with self.assertRaises(NXError):self.transport.request('GET','/system/users/me')
            with self.assertRaises(ResultUnknown):self.transport.request('POST','/system/users/21/timeEntries',body={'workItemId':'145','startTime':'2026-10-09T09:00:00','finishTime':'2026-10-09T10:00:00'})
        self.assertEqual(0,self.redirect_hits);self.assertEqual(11,len(self.requests))
    def test_tls_verification_is_required(self):
        with self.assertRaises(NXError):self.new_transport(context=ssl.create_default_context()).request('GET','/system/users/me')
        self.assertFalse(self.requests)
    def test_get_rate_limit_and_server_error_stop_without_retry(self):
        for status,code in [(429,'rate_limited'),(500,'server_unavailable'),(503,'server_unavailable'),(401,'authentication_required'),(403,'OperationDenied')]:
            self.mode=status;before=len(self.requests)
            with self.assertRaises(NXError) as caught:self.transport.request('GET','/system/users/me')
            self.assertEqual(code,caught.exception.code);self.assertEqual(before+1,len(self.requests));self.assertNotIn('synthetic-private',str(caught.exception))
    def test_mutation_429_5xx_disconnect_timeout_and_bad_success_are_unknown(self):
        body={'workItemId':'145','startTime':'2026-10-09T09:00:00','finishTime':'2026-10-09T10:00:00'}
        for mode in [408,429,500,503,'disconnect','timeout','malformed','unknown_4xx']:
            self.mode=None;transport=self.new_transport(timeout=.1 if mode=='timeout' else 2);transport.request('GET','/system/users/me');self.mode=mode;before=len(self.requests)
            with self.assertRaises(ResultUnknown):transport.request('POST','/system/users/21/timeEntries',body=body)
            self.assertEqual(before+1,len(self.requests))
    def test_known_rejection_stays_failed_and_omits_remote_message(self):
        self.transport.request('GET','/system/users/me');self.mode=400
        with self.assertRaises(NXError) as caught:self.transport.request('PUT','/system/users/21/timeEntries/1',body={'memo':'synthetic'})
        self.assertEqual('InputTimeEntryLocked',caught.exception.code);self.assertNotIn('synthetic-private',str(caught.exception))
    def test_secret_rotation_requires_new_me_and_unsafe_tls_is_rejected(self):
        self.transport.request('GET','/system/users/me')
        with patch.dict(os.environ,{'MOKVIA_NX_TEST_CREDENTIAL':'synthetic-rotated-value'}):
            with self.assertRaises(NXError) as caught:self.transport.request('POST','/system/users/21/timeEntries',body={'workItemId':'145','startTime':'2026-10-09T09:00:00','finishTime':'2026-10-09T10:00:00'})
            self.assertEqual('identity_required',caught.exception.code)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
        with self.assertRaises(NXError):self.new_transport(context=ctx)
        self.assertEqual(1,len(self.requests))

    def test_committed_post_disconnect_reconciles_without_second_post(self):
        adapter=NXAdapter(Settings('https://localhost/nx/api','Asia/Tokyo',5),Journal(self.root/'journal.json'),self.transport)
        adapter.register_task('task-1','https://localhost/task','145');rows=[actual()];preview=adapter.preview(rows)
        self.mode='disconnect_post'
        result=adapter.apply(rows,preview['token']);self.assertEqual('unknown',result[0]['status'])
        self.mode=None;self.assertEqual('synced',adapter.reconcile('actual-1'))
        self.assertEqual(1,len([r for r in self.requests if r[0]=='POST']))

    def test_common_http_router_uses_production_transport_and_keeps_secrets_out_of_state(self):
        from webapp.server import create_server
        from webapp.store import Store
        from webapp.security import WEB_MARKER_HEADER
        from urllib.request import Request,urlopen
        data=self.root/'data'
        for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(data/name).mkdir(parents=True,exist_ok=True)
        with contextlib.closing(Store(data)) as store:
            store.create_entity('task',{'title':'合成実績','status':'done','work_started_at':'2026-10-09T01:00:00Z','work_ended_at':'2026-10-09T02:00:00Z'},'Synthetic only')
        options={'timetracker_nx':{'api_base':'https://localhost/nx/api','timezone':'Asia/Tokyo','granularity':5}}
        server=create_server('127.0.0.1',0,data,bind_origin='http://127.0.0.1:24873',mutation_origins=('http://127.0.0.1:24873',),integration_options=options,integration_services={'timetracker_nx_transport':self.transport})
        thread=threading.Thread(target=server.serve_forever);thread.start()
        def request(path,body=None):
            req=Request('http://127.0.0.1:'+str(server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers={'Host':'127.0.0.1:24873','Origin':'http://127.0.0.1:24873',WEB_MARKER_HEADER:'1','Content-Type':'application/json'})
            with urlopen(req) as response:return json.load(response)
        try:
            status=request('/api/v1/integrations/status');self.assertTrue(next(p for p in status['plugins'] if p['id']=='timetracker_nx')['configured']);self.assertFalse(self.requests)
            ref=request('/api/v1/actuals')['actuals'][0]['reference_id'];self.assertFalse(self.requests)
            request('/api/v1/integrations/task',{'id':'timetracker_nx','reference_id':ref,'task_url':'https://localhost/task','work_item_id':'145','categories':{}})
            selection={'id':'timetracker_nx','references':[ref]};preview=request('/api/v1/integrations/actual-preview',selection)
            self.assertEqual('synced',request('/api/v1/integrations/actual-apply',{**selection,'token':preview['token']})[0]['status'])
            for file in (data/'.local-state').rglob('*.json'):self.assertNotIn('synthetic-only-value',file.read_text())
        finally:server.shutdown();server.server_close();thread.join()

    def test_adapter_paging_create_and_update_use_production_transport(self):
        self.mock.page_size=1;self.mock.add({'workItemId':'other','startTime':'2026-10-09T07:00:00','finishTime':'2026-10-09T08:00:00'})
        adapter=NXAdapter(Settings('https://localhost/nx/api','Asia/Tokyo',5),Journal(self.root/'journal.json'),self.transport)
        adapter.register_task('task-1','https://localhost/task','145');rows=[actual()];preview=adapter.preview(rows);result=adapter.apply(rows,preview['token']);self.assertEqual('synced',result[0]['status'])
        self.assertEqual('unchanged',adapter.preview(rows)['rows'][0]['action'])
        rows=[actual(end='10:30')];result=adapter.apply(rows,adapter.preview(rows)['token']);self.assertEqual('synced',result[0]['status'])
        self.assertEqual(['POST','PUT'],[r[0] for r in self.requests if r[0]!='GET'])
        self.assertTrue(any('offset=1' in r[1] for r in self.requests))
        self.assertNotIn('synthetic-only-value',(self.root/'journal.json').read_text())
