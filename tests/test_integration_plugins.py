"""Core/plugin boundary checks. All providers and Task records are synthetic."""
import copy
import contextlib
import dataclasses
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from webapp.integrations.actuals import task_actuals
from webapp.integrations.contracts import ActualRecord, ActualWriteReceipt, fingerprint
from webapp.integrations.registry import Registry, PluginSpec, builtins
from webapp.integrations.reflections import ActualCatalog, ReflectionService
from webapp.integrations.state import IntegrationError, IntegrationState
from webapp.integrations.external_events import read_json,write_json
from webapp.outlook_import import OutlookImport
from webapp.integration_plugins.outlook import saved_actuals


def entity(**fields):
    return SimpleNamespace(entity_type='task',entity_id='synthetic-task',frontmatter={'title':'Synthetic task','status':'done','work_started_at':'2026-10-09T10:00:00+09:00','work_ended_at':'2026-10-09T11:00:00+09:00',**fields})

class Writer:
    def __init__(self):self.requests=[];self.mode='ok'
    def preview_actuals(self,records):
        return {'token':fingerprint([r.to_dict() for r in records]),'rows':[{'id':r.reference_id,'action':'unchanged' if any(q.actual.reference_id==r.reference_id and q.actual.revision==r.revision for q in self.requests) else 'create'} for r in records]}
    def write_actual(self,request):
        self.requests.append(request)
        if self.mode=='unknown':raise OSError('synthetic upstream secret must not appear')
        state='failed' if self.mode=='failed' else 'applied'
        return ActualWriteReceipt(state,request.actual.reference_id,request.actual.revision,request.idempotency_key,request.remote_id or 'synthetic-remote',request.actual.revision,error_code='synthetic_failure' if state=='failed' else None)

class CorePluginsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.entities=[entity()];self.store=SimpleNamespace(read_snapshot=lambda:SimpleNamespace(entities=self.entities));self.writer=Writer();self.factory=Mock(return_value=self.writer)
        self.spec=PluginSpec('timetracker_nx',frozenset({'actual.read','actual.write'}),self.factory,lambda _:{'available':True,'configured':True,'connection_state':'connected'})
        self.registry=Registry(self.root,specs=[self.spec]);self.catalog=ActualCatalog(self.store);self.service=ReflectionService(self.registry,self.catalog)
    def record(self):return next(iter(self.catalog.read().values()))
    def test_registration_and_status_default_on_do_not_instantiate_or_create_state(self):
        value=self.registry.status('timetracker_nx');self.assertTrue(value['enabled']);self.factory.assert_not_called();self.assertFalse((self.root/'.local-state').exists());self.assertEqual({spec.id for spec in builtins()},{'outlook','google_calendar','timetracker_nx'})
    def test_task_actual_utc_stable_reference_and_historical_origin(self):
        first=self.record();self.assertEqual(first.reference_id,'task:synthetic-task');self.assertEqual(first.origin,'recorded');self.assertEqual(first.start,'2026-10-09T01:00:00+00:00')
        self.entities[0].frontmatter['work_started_at']='2026-10-09T01:00:00Z';self.assertEqual(self.record().revision,first.revision)
        self.entities[0].frontmatter['work_ended_at']='2026-10-09T12:00:00+09:00';self.assertEqual(self.record().reference_id,first.reference_id);self.assertNotEqual(self.record().revision,first.revision)
        del self.entities[0].frontmatter['work_started_at'];del self.entities[0].frontmatter['work_ended_at'];self.entities[0].frontmatter.update(started_at=first.start,completed_at=first.end);self.assertEqual(self.record().revision,first.revision)
    def test_create_update_and_repeat_reflection_state(self):
        first=self.record();self.assertEqual(self.service.status('timetracker_nx',first)['state'],'new');receipt=self.service.write('timetracker_nx',first.reference_id,first.revision);self.assertEqual(receipt['state'],'applied');self.assertEqual(self.writer.requests[0].operation,'create')
        self.service.write('timetracker_nx',first.reference_id,first.revision);self.assertEqual(len(self.writer.requests),1);self.assertEqual(self.service.status('timetracker_nx',first)['state'],'synced')
        self.entities[0].frontmatter['work_ended_at']='2026-10-09T12:00:00+09:00';second=self.record();self.assertEqual(self.service.status('timetracker_nx',second)['state'],'update');self.service.write('timetracker_nx',second.reference_id,second.revision);request=self.writer.requests[-1];self.assertEqual(request.operation,'update');self.assertEqual(request.remote_id,'synthetic-remote');self.assertEqual(request.expected_remote_revision,first.revision)
    def test_off_preserves_actuals_receipts_and_rejects_writes(self):
        record=self.record();self.service.write('timetracker_nx',record.reference_id,record.revision);self.registry.state.set_enabled('timetracker_nx',False,0)
        self.assertEqual(self.record(),record);status=self.service.status('timetracker_nx',record);self.assertEqual(status['state'],'synced');self.assertEqual(status['availability'],'disabled')
        with self.assertRaisesRegex(IntegrationError,'disabled'):self.service.write('timetracker_nx',record.reference_id,record.revision)
        self.assertEqual(len(self.writer.requests),1)
    def test_stale_or_running_actual_never_calls_writer(self):
        record=self.record()
        with self.assertRaisesRegex(IntegrationError,'conflict'):self.service.write('timetracker_nx',record.reference_id,'stale')
        self.entities[0].frontmatter['work_ended_at']=None;self.entities[0].frontmatter['status']='doing';record=self.record();self.assertTrue(record.provisional)
        with self.assertRaisesRegex(ValueError,'not_confirmed'):self.service.write('timetracker_nx',record.reference_id,record.revision)
        self.assertEqual(self.writer.requests,[])
    def test_unknown_write_is_durable_and_never_retried(self):
        self.writer.mode='unknown';record=self.record();value=self.service.write('timetracker_nx',record.reference_id,record.revision);self.assertEqual(value['state'],'unknown');self.assertNotIn('secret',json.dumps(value))
        with self.assertRaisesRegex(IntegrationError,'requires_readback'):self.service.write('timetracker_nx',record.reference_id,record.revision)
        self.assertEqual(len(self.writer.requests),1);self.assertEqual(self.service.status('timetracker_nx',record)['state'],'unknown')
    def test_ack_disk_failure_retains_pending_and_blocks_duplicate_write(self):
        from webapp.integrations import external_events
        record=self.record();original=external_events.os.replace;count=0
        def replace(*args):
            nonlocal count
            count+=1
            if count==2:raise OSError('Synthetic acknowledgement disk failure')
            return original(*args)
        with patch.object(external_events.os,'replace',side_effect=replace):
            with self.assertRaises(OSError):self.service.write('timetracker_nx',record.reference_id,record.revision)
        self.assertEqual(self.service.status('timetracker_nx',record)['state'],'unknown')
        with self.assertRaises(IntegrationError):self.service.write('timetracker_nx',record.reference_id,record.revision)
        self.assertEqual(len(self.writer.requests),1)
    def test_failed_update_keeps_remote_binding_for_explicit_retry(self):
        record=self.record();self.service.write('timetracker_nx',record.reference_id,record.revision);self.entities[0].frontmatter['title']='Synthetic correction';record=self.record();self.writer.mode='failed';self.service.write('timetracker_nx',record.reference_id,record.revision);self.writer.mode='ok';self.service.write('timetracker_nx',record.reference_id,record.revision);self.assertEqual(self.writer.requests[-1].operation,'update');self.assertEqual(self.writer.requests[-1].remote_id,'synthetic-remote')
    def test_outlook_external_dto_and_task_actual_share_catalog_without_internal_identity(self):
        first='2026-10-01T00:00:00+00:00';last='2026-11-01T00:00:00+00:00'
        payload={'version':1,'complete':True,'source':'classic','scope':'synthetic','from':first,'to':last,'events':[{'uid':'synthetic-provider-id','occurrence':'','title':'Synthetic meeting','start':'2026-10-09T01:00:00Z','end':'2026-10-09T02:00:00Z'}]}
        events=OutlookImport(self.root,collector=lambda *_:copy.deepcopy(payload));events.run('classic',first,last);self.assertEqual(saved_actuals(events),[]);state=events.state();key=next(iter(state['events']));events.annotate(key,state['revision'],True);catalog=ActualCatalog(self.store,[lambda:saved_actuals(events)]);records=catalog.read();self.assertEqual(len(records),2);record=records['external:'+key];self.assertEqual(record.origin,'planned');self.assertNotIn('uid',record.to_dict());self.assertNotIn('scope',record.to_dict());self.assertEqual(record.source_provider,'outlook')
    def test_flags_cas_and_symlink_fail_closed(self):
        self.registry.state.set_enabled('outlook',False,0)
        with self.assertRaisesRegex(IntegrationError,'conflict'):self.registry.state.set_enabled('outlook',True,0)
        path=self.registry.state.directory/'settings.json';path.unlink();path.symlink_to(self.root/'absent')
        with self.assertRaises(OSError):self.registry.state.flags()
    @unittest.skipIf(importlib.util.find_spec('calendar_sync') is None,'Legacy Google service is not shipped in the company profile')
    def test_google_off_skips_service_factory_without_touching_timer(self):
        from calendar_sync.service import main
        self.registry.state.set_enabled('google_calendar',False,0);factory=Mock();output=io.StringIO()
        with contextlib.redirect_stdout(output):self.assertEqual(main(['run'],service_factory=factory,integration_root=self.root),0)
        factory.assert_not_called();self.assertEqual(json.loads(output.getvalue())['code'],'plugin_disabled')
    def test_backup_restore_keeps_off_flag_and_reflection_binding(self):
        path=Path(__file__).resolve().parents[1]/'distribution/mokvia/files/local_runtime.py'
        if not path.exists():path=Path(__file__).resolve().parents[1]/'local_runtime.py'
        spec=importlib.util.spec_from_file_location('synthetic_integration_runtime',path);runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime);runtime.initialize(self.root)
        record=self.record();self.service.write('timetracker_nx',record.reference_id,record.revision);self.registry.state.set_enabled('outlook',False,0);archive=self.root.parent/(self.root.name+'-synthetic.tar');self.addCleanup(archive.unlink,missing_ok=True);runtime.backup(self.root,archive)
        self.registry.state.set_enabled('outlook',True,1);runtime.restore(self.root,archive);self.assertFalse(self.registry.state.enabled('outlook'));self.assertEqual(self.service.status('timetracker_nx',record)['state'],'synced')

class PluginHttpTest(unittest.TestCase):
    def setUp(self):
        import threading
        from webapp.server import create_server
        from webapp.store import Store
        from webapp.outlook_import import instant
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(self.root/name).mkdir(parents=True,exist_ok=True)
        with contextlib.closing(Store(self.root)) as store:
            self.task=store.create_entity('task',{'title':'Synthetic HTTP actual','status':'done','work_started_at':'2026-10-09T01:00:00Z','work_ended_at':'2026-10-09T02:00:00Z'},'Synthetic only')
        self.writer=Writer();spec=PluginSpec('timetracker_nx',frozenset({'actual.write'}),lambda _:self.writer,lambda _:{'available':True,'configured':True,'connection_state':'connected'})
        specs=[s for s in builtins() if s.id!='timetracker_nx']+[spec]
        def collect(source,first,last):return {'version':1,'complete':True,'source':source,'scope':'synthetic','from':first,'to':last,'events':[{'uid':'synthetic','occurrence':'','title':'Synthetic HTTP meeting','start':'2026-10-09T01:00:00Z','end':'2026-10-09T02:00:00Z'}]}
        self.server=create_server('127.0.0.1',0,self.root,bind_origin='http://127.0.0.1:24873',mutation_origins=('http://127.0.0.1:24873',),outlook_collector=collect,integration_specs=specs,integration_availability={'google_calendar':False});self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.close)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path,body=None,origin='http://127.0.0.1:24873'):
        from urllib.request import Request,urlopen
        from webapp.security import WEB_MARKER_HEADER
        with urlopen(Request('http://127.0.0.1:'+str(self.server.server_address[1])+path,data=json.dumps(body).encode() if body is not None else None,headers={'Host':'127.0.0.1:24873','Origin':origin,WEB_MARKER_HEADER:'1','Content-Type':'application/json'})) as response:return json.load(response)
    def test_actual_write_endpoint_uses_core_refs_new_update_and_off_state(self):
        state=self.request('/api/v1/integrations/status');self.assertTrue(all(p['enabled'] for p in state['plugins']));self.assertFalse((self.root/'.local-state').exists());actual=self.request('/api/v1/actuals')['actuals'][0]
        preview=self.request('/api/v1/integrations/actual-preview',{'id':'timetracker_nx','references':[actual['reference_id']]});receipt=self.request('/api/v1/integrations/actual-apply',{'id':'timetracker_nx','references':[actual['reference_id']],'token':preview['token']});self.assertEqual(receipt[0]['status'],'synced');self.assertEqual(len(self.writer.requests),1)
        preview=self.request('/api/v1/integrations/actual-preview',{'id':'timetracker_nx','references':[actual['reference_id']]});self.request('/api/v1/integrations/actual-apply',{'id':'timetracker_nx','references':[actual['reference_id']],'token':preview['token']});self.assertEqual(len(self.writer.requests),1)
        self.request('/api/v1/integrations/settings',{'id':'timetracker_nx','enabled':False,'revision':state['revision']});after=self.request('/api/v1/actuals');self.assertEqual(after['actuals'][0],actual);self.assertEqual(after['reflection']['timetracker_nx'][0]['state'],'synced');self.assertEqual(after['reflection']['timetracker_nx'][0]['availability'],'disabled')
    def test_outlook_off_keeps_saved_actual_and_local_edit_but_rejects_acquisition(self):
        from urllib.error import HTTPError
        body={'source':'classic','from':'2026-10-01T00:00:00Z','to':'2026-11-01T00:00:00Z'};self.request('/api/v1/outlook-import/run',body);state=self.request('/api/v1/outlook-import/status');key=state['events'][0]['key'];self.request('/api/v1/outlook-import/local',{'key':key,'revision':state['revision'],'done':True});before=self.request('/api/v1/actuals');self.request('/api/v1/integrations/settings',{'id':'outlook','enabled':False,'revision':0})
        with self.assertRaises(HTTPError) as error:self.request('/api/v1/outlook-import/run',body)
        self.assertEqual(error.exception.code,400);self.assertEqual(self.request('/api/v1/actuals')['actuals'],before['actuals']);state=self.request('/api/v1/outlook-import/status');self.assertFalse(state['enabled']);self.request('/api/v1/outlook-import/local',{'key':key,'revision':state['revision'],'done':False});self.assertEqual(len(self.request('/api/v1/calendar?view=day&date=2026-10-09')['days'][0]['events']),2)
    def test_cross_origin_and_remote_target_injection_never_write(self):
        from urllib.error import HTTPError
        actual=self.request('/api/v1/actuals')['actuals'][0];body={'id':'timetracker_nx','reference_id':actual['reference_id'],'revision':actual['revision']}
        with self.assertRaises(HTTPError) as error:self.request('/api/v1/integrations/actual-write',body,origin='https://evil.invalid')
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError):self.request('/api/v1/integrations/actual-write',{**body,'remote_id':'injected-synthetic'})
        self.assertEqual(self.writer.requests,[])
