"""Whole core/router/NX tests with isolated Task catalog and synthetic NX."""
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from tests.test_timetracker_nx import MockNX
from webapp.server import create_server
from webapp.store import Store
from webapp.security import WEB_MARKER_HEADER


class IntegratedNXTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):
            (self.root/name).mkdir(parents=True,exist_ok=True)
        with contextlib.closing(Store(self.root)) as store:
            for index,(start,end) in enumerate([('01:00','02:00'),('02:00','03:00')]):
                store.create_entity('task',{'title':'合成実績'+str(index),'status':'done','work_started_at':'2026-10-09T'+start+':00Z','work_ended_at':'2026-10-09T'+end+':00Z'},'Synthetic only')
        self.mock=MockNX()
        self.server=create_server('127.0.0.1',0,self.root,bind_origin='http://127.0.0.1:24873',mutation_origins=('http://127.0.0.1:24873',),integration_options={'timetracker_nx':{'api_base':'https://example.invalid/api','timezone':'Asia/Tokyo','granularity':5}},integration_services={'timetracker_nx_transport':self.mock})
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.close)
        self.actuals=self.request('/api/v1/actuals')['actuals'];self.refs=[r['reference_id'] for r in self.actuals]
        for ref in self.refs:self.request('/api/v1/integrations/task',{'id':'timetracker_nx','reference_id':ref,'task_url':'https://example.invalid/task','work_item_id':'145','categories':{}})

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()

    def request(self,path,body=None,origin='http://127.0.0.1:24873'):
        request=Request('http://127.0.0.1:'+str(self.server.server_address[1])+path,data=json.dumps(body).encode() if body is not None else None,headers={'Host':'127.0.0.1:24873','Origin':origin,WEB_MARKER_HEADER:'1','Content-Type':'application/json'})
        with urlopen(request,timeout=5) as response:return json.load(response)

    def preview(self,refs=None):return self.request('/api/v1/integrations/actual-preview',{'id':'timetracker_nx','references':self.refs if refs is None else refs})
    def apply(self,preview,refs=None):return self.request('/api/v1/integrations/actual-apply',{'id':'timetracker_nx','references':self.refs if refs is None else refs,'token':preview['token']})

    def error(self,path,body,code,origin='http://127.0.0.1:24873'):
        with self.assertRaises(HTTPError) as caught:self.request(path,body,origin)
        with caught.exception as response:self.assertEqual(code,json.load(response)['error']['code'])

    def test_batch_create_unchanged_update_and_stale_core_preview(self):
        preview=self.preview();self.assertFalse(self.mock.mutations())
        self.assertEqual(['synced','synced'],[r['status'] for r in self.apply(preview)])
        count=len(self.mock.mutations());self.assertEqual(['unchanged','unchanged'],[r['action'] for r in self.preview()['rows']])
        self.apply(self.preview());self.assertEqual(count,len(self.mock.mutations()))
        preview=self.preview()
        task_id=self.actuals[1]['source_id']
        task=self.request('/api/v1/entities/tasks/'+task_id)
        operation={'action':'update','kind':'tasks','id':task_id,'base_hash':task['content_hash'],'fields':{'work_ended_at':'2026-10-09T03:30:00Z'}}
        mutation=self.request('/api/v1/mutations/preview',operation);hash=mutation.pop('preview_hash');self.request('/api/v1/mutations/apply',{'preview':mutation,'preview_hash':hash})
        self.error('/api/v1/integrations/actual-apply',{'id':'timetracker_nx','references':self.refs,'token':preview['token']},'stale_preview')
        self.assertEqual(['unchanged','synced'],[r['status'] for r in self.apply(self.preview())]);self.assertEqual('PUT',self.mock.mutations()[-1][0])

    def test_unknown_reconcile_updates_core_receipt_and_never_reposts(self):
        self.mock.fail='after';results=self.apply(self.preview());self.assertEqual('unknown',results[0]['status']);self.assertEqual(1,len(self.mock.mutations()))
        state=self.request('/api/v1/actuals');self.assertEqual('unknown',state['reflection']['timetracker_nx'][0]['state'])
        self.assertEqual('unknown',self.preview()['rows'][0]['code']);self.assertEqual(1,len(self.mock.mutations()))
        response=self.request('/api/v1/integrations/actual-reconcile',{'id':'timetracker_nx','reference_id':self.refs[0]})
        self.assertEqual('synced',response['status']);self.assertTrue(response['receipt']['remote_revision'])
        self.assertEqual('synced',self.request('/api/v1/actuals')['reflection']['timetracker_nx'][0]['state'])
        self.assertEqual(1,len(self.mock.mutations()))

    def test_category_only_updates_and_crash_before_provider_intent(self):
        from webapp.integrations.reflections import ReflectionService
        from webapp.integrations.contracts import fingerprint
        from webapp.integrations.external_events import write_json
        self.apply(self.preview())
        def category(value):
            self.request('/api/v1/integrations/task',{'id':'timetracker_nx','reference_id':self.refs[0],'task_url':'https://example.invalid/task','work_item_id':'145','categories':{'timeEntryCategoryId':value}})
        category('4');preview=self.preview();self.assertEqual('update',preview['rows'][0]['action'])
        self.apply(preview);self.assertEqual('4',self.mock.rows['1']['timeEntryCategoryId']);self.assertEqual(3,len(self.mock.mutations()))
        category('6');self.apply(self.preview());self.assertEqual('6',self.mock.rows['1']['timeEntryCategoryId']);self.assertEqual(4,len(self.mock.mutations()))
        # Core intent persisted but the provider never saw this new operation.
        receipt_path=self.root/'.local-state/integrations'/('receipt-'+fingerprint(['timetracker_nx',self.refs[0]])+'.json')
        receipt=json.loads(receipt_path.read_text());receipt.update(state='pending',idempotency_key=fingerprint('new-unsent-operation'))
        write_json(receipt_path,receipt);category('8')
        result=self.request('/api/v1/integrations/actual-reconcile',{'id':'timetracker_nx','reference_id':self.refs[0]})
        self.assertEqual('not_sent',result['status']);self.assertEqual('failed',result['receipt']['state'])
        self.assertEqual('6',self.mock.rows['1']['timeEntryCategoryId']);self.assertEqual(4,len(self.mock.mutations()))
        self.apply(self.preview());self.assertEqual('8',self.mock.rows['1']['timeEntryCategoryId']);self.assertEqual(5,len(self.mock.mutations()))

    def test_known_nx_rejection_is_failed_and_explicit_retry_works(self):
        self.mock.reject='InputTimeEntryLocked'
        result=self.apply(self.preview());self.assertEqual('rejected',result[0]['status']);self.assertEqual('input_time_entry_locked',result[0]['code'])
        receipt=self.request('/api/v1/actuals')['reflection']['timetracker_nx'][0]
        self.assertEqual('failed',receipt['state'])
        self.assertFalse(json.loads((self.root/'.local-state/integrations/timetracker-nx.json').read_text())['entries'])
        self.mock.reject=None;self.apply(self.preview());self.assertEqual(2,len(self.mock.rows))

    def test_disabled_preserves_mapping_and_receipt_and_invalidates_preview(self):
        self.apply(self.preview());before=self.request('/api/v1/actuals');journal=(self.root/'.local-state/integrations/timetracker-nx.json').read_bytes();preview=self.preview()
        self.request('/api/v1/integrations/settings',{'id':'timetracker_nx','enabled':False,'revision':0})
        self.error('/api/v1/integrations/actual-apply',{'id':'timetracker_nx','references':self.refs,'token':preview['token']},'plugin_disabled')
        after=self.request('/api/v1/actuals');self.assertEqual(before['actuals'],after['actuals']);self.assertEqual('synced',after['reflection']['timetracker_nx'][0]['state']);self.assertEqual(journal,(self.root/'.local-state/integrations/timetracker-nx.json').read_bytes())
        self.request('/api/v1/integrations/settings',{'id':'timetracker_nx','enabled':True,'revision':1})
        self.error('/api/v1/integrations/actual-apply',{'id':'timetracker_nx','references':self.refs,'token':preview['token']},'stale_preview')
        self.assertEqual(2,len(self.mock.mutations()))

    def test_remote_conflict_even_unchanged_and_target_injection_refused(self):
        self.apply(self.preview());self.mock.rows['1']['updatedAt']='external-change'
        self.assertEqual('conflict',self.preview()['rows'][0]['code'])
        self.error('/api/v1/integrations/actual-write',{'id':'timetracker_nx','reference_id':self.refs[0],'revision':self.actuals[0]['revision']},'preview_required')
        self.error('/api/v1/integrations/actual-preview',{'id':'timetracker_nx','references':self.refs,'userId':'other'},'invalid_request')
        self.error('/api/v1/integrations/actual-preview',{'id':'timetracker_nx','references':self.refs},'forbidden',origin='https://evil.invalid')
        self.assertEqual(2,len(self.mock.mutations()))

    def test_backup_restore_preserves_unknown_mapping_core_receipts_and_off(self):
        self.mock.fail='after';self.apply(self.preview());self.request('/api/v1/integrations/settings',{'id':'timetracker_nx','enabled':False,'revision':0})
        self.close();self._cleanups.pop()  # Runtime backup owns the stopped root.
        module_path=Path(__file__).resolve().parents[1]/'distribution/mokvia/files/local_runtime.py'
        if not module_path.exists():module_path=Path(__file__).resolve().parents[1]/'local_runtime.py'
        spec=importlib.util.spec_from_file_location('nx_company_runtime',module_path);runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        archive=self.root.parent/(self.root.name+'-nx.tar');self.addCleanup(archive.unlink,missing_ok=True)
        runtime.backup(self.root,archive)
        original=(self.root/'.local-state/integrations/timetracker-nx.json').read_bytes()
        state=(self.root/'.local-state/integrations/settings.json');state.write_text('{"version":1,"revision":2,"enabled":{"timetracker_nx":true}}')
        runtime.restore(self.root,archive)
        self.assertEqual(original,(self.root/'.local-state/integrations/timetracker-nx.json').read_bytes())
        self.assertFalse(json.loads(state.read_text())['enabled']['timetracker_nx'])
        receipts=list((self.root/'.local-state/integrations').glob('receipt-*.json'));self.assertEqual('unknown',json.loads(receipts[0].read_text())['state'])
