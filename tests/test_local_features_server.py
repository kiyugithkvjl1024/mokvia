"""Shared runtime integration over synthetic data; never contacts Google or Microsoft."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen
from webapp.server import create_server

class LocalFeaturesServerTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.incoming=self.base/'incoming'
        self.incoming.mkdir()
        for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):
            (self.root/name).mkdir(parents=True,exist_ok=True)
    @contextlib.contextmanager
    def running(self,**kwargs):
        server=create_server('127.0.0.1',0,self.root,bind_origin='http://127.0.0.1:24873',mutation_origins=('http://127.0.0.1:24873',),**kwargs)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        def get(path):
            with urlopen('http://127.0.0.1:'+str(server.server_port)+path) as r:return r.read()
        try:yield server,get
        finally:server.shutdown();server.server_close();thread.join()
    def test_calendar_and_disabled_capture_without_external_connections(self):
        with self.running() as (server,get):
            self.assertIn(b'local-calendar.js',get('/calendar'))
            self.assertIn(b'href="/calendar"',get('/calendar'))
            self.assertEqual(json.loads(get('/api/v1/capture-import/status')),{'enabled':False})
            value=json.loads(get('/api/v1/calendar?view=week&date=2026-10-04'))
            self.assertEqual(len(value['days']),7)
            self.assertFalse((self.root/'.local-state').exists())
    def test_explicit_importer_uses_configured_api_origin_and_stops_with_server(self):
        from webapp.local_capture import source_key
        cid='11111111-1111-4111-8111-111111111111'
        value={'version':1,'capture_id':cid,'accepted_at':'2026-10-03T15:00:00Z','destination':'today','title':'Synthetic shared capture','due':'2026-10-09','source':{'kind':'teams','scope_id':'synthetic','message_id':'synthetic','url':'https://teams.microsoft.com/l/message/synthetic/synthetic'}}
        (self.incoming/(cid+'.json')).write_text(json.dumps(value))
        with self.running(capture_folder=self.incoming) as (server,get):
            importer=server._capture_importer
            store=getattr(server,'_gtd_store',None) or getattr(server,'_mokvia_store')
            now=dt.datetime.now(dt.timezone.utc)
            importer.poll(store,now);importer.poll(store,now+dt.timedelta(seconds=10))
            result=json.loads(get('/api/v1/entities/tasks/task-capture-'+source_key(value)[:32]))
            self.assertEqual(result['frontmatter']['action_date'],'2026-10-04')
            self.assertEqual(result['frontmatter']['due'],'2026-10-09')
            self.assertEqual(json.loads(get('/api/v1/capture-import/status'))['tracked'],1)
        self.assertFalse(importer.worker.is_alive())
    def test_read_only_preview_rejects_capture_before_creating_state(self):
        with self.assertRaises(ValueError):
            create_server('127.0.0.1',0,self.root,read_only=True,capture_folder=self.incoming)
        self.assertFalse((self.root/'.local-state').exists())
