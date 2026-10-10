"""Loopback only; no real credentials, send, or company connection."""
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from webapp.server import create_server
from webapp.security import WEB_MARKER_HEADER

class NotificationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(self.root/name).mkdir(parents=True,exist_ok=True)
        self.server=create_server('127.0.0.1',0,self.root,bind_origin='http://127.0.0.1:24873',mutation_origins=('http://127.0.0.1:24873',))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.close)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,body=None,origin='http://127.0.0.1:24873',marker='1',path='/api/v1/notifications/settings'):
        req=Request('http://127.0.0.1:'+str(self.server.server_address[1])+path,data=(body if type(body) is bytes else json.dumps(body).encode()) if body is not None else None,headers={'Host':'127.0.0.1:24873','Origin':origin,WEB_MARKER_HEADER:marker,'Content-Type':'application/json'})
        with urlopen(req,timeout=5) as response:return json.load(response)
    def test_save_readback_conflict_and_security(self):
        value=self.request();value['quiet_enabled']=True
        saved=self.request(value);self.assertEqual(1,saved['revision']);self.assertEqual(saved,self.request())
        for kwargs in ({},{'origin':'https://example.invalid'},{'marker':'0'}):
            with self.assertRaises(HTTPError) as error:self.request(value,**kwargs)
            self.assertEqual(409 if not kwargs else 403,error.exception.code);error.exception.close()
        self.assertEqual(saved,self.request());self.assertEqual([],list((self.root/'tasks').glob('*')))
    def test_query_and_corruption_rejected(self):
        with self.assertRaises(HTTPError) as error:self.request(path='/api/v1/notifications/settings?a=b')
        self.assertEqual(400,error.exception.code);error.exception.close()
        value=self.request();value['unexpected']='x'
        with self.assertRaises(HTTPError) as error:self.request(value)
        self.assertEqual(400,error.exception.code);error.exception.close()

    def test_recovery_blocks_write_and_duplicate_keys_rejected(self):
        value=self.request();(self.root/'.webapp-mutation-state').write_bytes(b'armed\n')
        with self.assertRaises(HTTPError) as error:self.request(value)
        self.assertEqual(409,error.exception.code);error.exception.close()
        (self.root/'.webapp-mutation-state').write_bytes(b'idle\n')
        raw=json.dumps(value)[:-1]+',"revision":0}'
        with self.assertRaises(HTTPError) as error:self.request(raw.encode())
        self.assertEqual(400,error.exception.code);error.exception.close()
        self.assertEqual(0,self.request()['revision'])
