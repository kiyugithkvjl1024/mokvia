import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from webapp.outlook_import import OutlookImport, ImportError, instant, write_json
from webapp.outlook_graph import GraphSource, BASE
from webapp.local_calendar import calendar_projection

FIRST=instant('2026-10-01T00:00:00Z');LAST=instant('2026-11-01T00:00:00Z')
def event(uid='series', occurrence='2026-10-09T01:00:00Z', **kw):
    return {'uid':uid,'occurrence':occurrence,'title':'合成会議','start':'2026-10-09T01:00:00Z','end':'2026-10-09T02:00:00Z',**kw}
def snapshot(rows,source='classic',scope='synthetic-calendar'):
    return {'version':1,'source':source,'scope':scope,'complete':True,'from':FIRST,'to':LAST,'events':rows}
class OutlookTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.payload=snapshot([event()]);self.service=OutlookImport(self.root,collector=lambda *_:copy.deepcopy(self.payload))
    def run_import(self):return self.service.run(self.payload['source'],FIRST,LAST)
    def state_bytes(self):return (self.service.directory/'state.json').read_bytes()
    def test_repeat_recurring_and_moved_exception_preserves_local_history(self):
        self.payload['events'].append(event(occurrence='2026-10-16T01:00:00Z',start='2026-10-16T01:00:00Z',end='2026-10-16T02:00:00Z'))
        self.assertEqual(self.run_import()['added'],2);self.assertEqual(self.run_import()['added'],0)
        state=self.service.state();key=next(k for k,v in state['events'].items() if v['occurrence']==instant('2026-10-09T01:00:00Z'));self.service.annotate(key,state['revision'],True)
        state=self.service.state();state['events'][key]['local']['work_sessions']=[{'start':'2026-10-09T01:10:00Z','end':'2026-10-09T01:45:00Z'}];state['events'][key]['local']['memo']='合成メモ';write_json(self.service.directory/'state.json',state)
        self.payload['events'][0]['start']='2026-10-10T01:00:00Z';self.payload['events'][0]['end']='2026-10-10T02:00:00Z'
        self.assertEqual(self.run_import()['changed'],1);state=self.service.state();self.assertEqual(len(state['events']),2);self.assertTrue(state['events'][key]['local']['done']);self.assertTrue(state['events'][key]['needs_review']);self.assertEqual(state['events'][key]['local']['memo'],'合成メモ');self.assertEqual(len(state['events'][key]['local']['work_sessions']),1)
    def test_cancel_and_missing_do_not_erase_completion(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));self.service.annotate(key,state['revision'],True)
        self.payload['events'][0]['cancelled']=True;self.assertEqual(self.run_import()['cancelled'],1);self.assertTrue(self.service.state()['events'][key]['local']['done'])
        self.payload['events']=[];self.run_import();self.assertEqual(self.service.state()['events'][key]['presence'],'cancelled')
    def test_empty_snapshot_marks_unseen_not_cancelled_and_reappearance_restores(self):
        self.run_import();key=next(iter(self.service.state()['events']));self.payload['events']=[];self.assertEqual(self.run_import()['unseen'],1);self.assertFalse(self.service.state()['events'][key]['cancelled']);self.payload['events']=[event()];self.run_import();self.assertEqual(self.service.state()['events'][key]['presence'],'present')
    def test_partial_invalid_duplicate_and_collect_failure_preserve_exact_bytes(self):
        self.run_import();before=self.state_bytes()
        for mutate in (lambda p:p.update(complete=False),lambda p:p['events'].append(event(title='競合')),lambda p:p['events'].append(event(uid='bad',start='not-a-date')),lambda p:p.update(scope='other')):
            self.payload=snapshot([event()]);mutate(self.payload)
            with self.assertRaises(ImportError):self.run_import()
            self.assertEqual(self.state_bytes(),before)
        self.service.collector=lambda *_:(_ for _ in ()).throw(OSError('synthetic failure'))
        with self.assertRaises(OSError):self.service.run('classic',FIRST,LAST)
        self.assertEqual(self.state_bytes(),before)
    def test_source_switch_fails_closed_instead_of_duplicating_events(self):
        self.run_import();before=self.state_bytes();self.payload=snapshot([event()],source='graph')
        with self.assertRaisesRegex(ImportError,'source_change'):self.run_import()
        self.assertEqual(before,self.state_bytes())
    def test_revision_conflict_and_reopen_preserve_work(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));self.service.annotate(key,state['revision'],True)
        with self.assertRaisesRegex(ImportError,'conflict'):self.service.annotate(key,state['revision'],False)
        self.service.annotate(key,self.service.state()['revision'],False);local=self.service.state()['events'][key]['local'];self.assertFalse(local['done']);self.assertIsNone(local['completed_at']);self.assertEqual(local['work_sessions'],[])
    def test_disabled_timeout_and_symlink_preserve_data(self):
        self.assertFalse(OutlookImport(self.root).status()['enabled']);folder=self.root/'handoff';folder.mkdir();service=OutlookImport(self.root,folder,timeout=.01)
        with self.assertRaisesRegex(ImportError,'timeout'):service.run('classic',FIRST,LAST)
        self.assertEqual(list(folder.iterdir()),[]);self.assertFalse((service.directory/'state.json').exists())
        (service.directory/'state.json').symlink_to(self.root/'missing')
        with self.assertRaises(OSError):service.status()
    def test_busy_completion_during_import_is_not_lost(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']))
        with self.service.lock():
            with self.assertRaisesRegex(ImportError,'busy'):self.service.annotate(key,state['revision'],True)
    def test_calendar_keeps_done_external_at_original_time(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));self.service.annotate(key,state['revision'],True)
        class Store:
            def read_snapshot(self):
                class Snapshot:entities=[];recovery_required=False
                return Snapshot()
        projection=calendar_projection(Store(),'day','2026-10-09',external=self.service.status()['events']);row=projection['days'][0]['events'][0];self.assertTrue(row['external']);self.assertEqual(row['status'],'done');self.assertEqual(row['kind'],'external')

def graph_event(**kw):
    return {'id':'immutable-one','iCalUId':'uid-one','type':'singleInstance','subject':'合成Graph予定','start':{'dateTime':'2026-10-09T01:00:00','timeZone':'UTC'},'end':{'dateTime':'2026-10-09T02:00:00','timeZone':'UTC'},**kw}
class GraphTest(unittest.TestCase):
    def source(self,get):return GraphSource('synthetic-mailbox','synthetic-calendar',lambda:'synthetic-token',get=get)
    def test_multiple_pages_and_original_recurrence_identity(self):
        calls=[]
        def get(url,token):
            calls.append(url);self.assertEqual(token,'synthetic-token')
            if '/events/master?' in url:return {'iCalUId':'series-uid'}
            if 'skiptoken=two' in url:return {'value':[graph_event(id='exception',type='exception',seriesMasterId='master',originalStart='2026-10-16T01:00:00Z')]}
            return {'value':[graph_event()], '@odata.nextLink':BASE+'/me/calendars/synthetic-calendar/calendarView?skiptoken=two'}
        value=self.source(get).fetch(FIRST,LAST);self.assertTrue(value['complete']);self.assertEqual(len(value['events']),2);self.assertEqual(value['events'][1]['uid'],'series-uid');self.assertEqual(value['events'][1]['occurrence'],instant('2026-10-16T01:00:00Z'));self.assertEqual(len(calls),3)
    def test_page_failure_does_not_publish_partial_result(self):
        def get(url,_):
            if 'skip=' in url:raise OSError('synthetic timeout')
            return {'value':[graph_event()],'@odata.nextLink':BASE+'/me/calendars/synthetic-calendar/calendarView?skip=2'}
        with self.assertRaises(OSError):self.source(get).fetch(FIRST,LAST)
    def test_untrusted_pagination_never_receives_token(self):
        calls=[]
        def get(url,_):calls.append(url);return {'value':[],'@odata.nextLink':'https://example.invalid/collect'}
        with self.assertRaisesRegex(ImportError,'unsafe'):self.source(get).fetch(FIRST,LAST)
        self.assertEqual(len(calls),1)
    def test_timezone_and_missing_occurrence_fail_closed(self):
        for event in (graph_event(start={'dateTime':'2026-10-09T01:00:00','timeZone':'Tokyo Standard Time'}),graph_event(type='exception',seriesMasterId='master')):
            with self.assertRaises(ImportError):self.source(lambda *_:{'value':[event]}).fetch(FIRST,LAST)

class OutlookHttpTest(unittest.TestCase):
    def setUp(self):
        from webapp.server import create_server
        import threading
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for directory in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(self.root/directory).mkdir(parents=True,exist_ok=True)
        self.server=create_server('127.0.0.1',0,self.root,bind_origin='http://127.0.0.1:24873',mutation_origins=('http://127.0.0.1:24873',),outlook_collector=lambda *_:snapshot([event()]))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.close)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path,body=None,origin='http://127.0.0.1:24873'):
        from urllib.request import Request,urlopen
        from webapp.security import WEB_MARKER_HEADER
        headers={'Host':'127.0.0.1:24873','Origin':origin,'Content-Type':'application/json',WEB_MARKER_HEADER:'1'}
        with urlopen(Request('http://127.0.0.1:'+str(self.server.server_address[1])+path,data=json.dumps(body).encode() if body else None,headers=headers)) as response:return json.load(response)
    def test_real_http_import_complete_reopen_and_projection(self):
        self.assertTrue(self.request('/api/v1/outlook-import/status')['enabled'])
        self.request('/api/v1/outlook-import/run',{'source':'classic','from':FIRST,'to':LAST})
        state=self.request('/api/v1/outlook-import/status');key=state['events'][0]['key']
        self.request('/api/v1/outlook-import/local',{'key':key,'revision':state['revision'],'done':True})
        rows=self.request('/api/v1/calendar?view=day&date=2026-10-09')['days'][0]['events'];self.assertEqual(rows[0]['status'],'done');self.assertTrue(rows[0]['external'])
        state=self.request('/api/v1/outlook-import/status');self.request('/api/v1/outlook-import/local',{'key':key,'revision':state['revision'],'done':False})
        self.assertEqual(list((self.root/'tasks').iterdir()),[])
    def test_cross_origin_rejected_before_acquisition(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as error:self.request('/api/v1/outlook-import/run',{'source':'classic','from':FIRST,'to':LAST},origin='https://example.invalid')
        self.assertEqual(error.exception.code,403);self.assertFalse((self.root/'.local-state/outlook-import/state.json').exists())
    def test_conflict_keeps_local_annotation(self):
        from urllib.error import HTTPError
        self.request('/api/v1/outlook-import/run',{'source':'classic','from':FIRST,'to':LAST});state=self.request('/api/v1/outlook-import/status');body={'key':state['events'][0]['key'],'revision':state['revision'],'done':True};self.request('/api/v1/outlook-import/local',body)
        with self.assertRaises(HTTPError) as error:self.request('/api/v1/outlook-import/local',body)
        self.assertEqual(error.exception.code,409)

class MeetingActualTest(OutlookTest):
    def test_direct_done_seeds_planned_actual_once_and_repeat_preserves_timestamp(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));self.service.annotate(key,state['revision'],True);state=self.service.state();actual=copy.deepcopy(state['events'][key]['local']['actual']);at=state['events'][key]['local']['completed_at'];revision=state['revision']
        self.assertEqual(actual['origin'],'planned');self.assertEqual(actual['start'],instant(event()['start']));self.service.annotate(key,revision,True);self.assertEqual(self.service.state()['revision'],revision);self.assertEqual(self.service.state()['events'][key]['local']['completed_at'],at)
        self.payload['events'][0]['end']='2026-10-09T03:00:00Z';self.run_import();self.assertEqual(self.service.state()['events'][key]['local']['actual'],actual)
    def test_manual_extension_retained_reimport_and_undo(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));self.service.annotate(key,state['revision'],True);state=self.service.state();before=copy.deepcopy(state['events'][key]['local'])
        applied=self.service.annotate(key,state['revision'],True,actual={'start':'2026-10-09T01:00:00Z','end':'2026-10-09T02:45:00Z'});self.assertEqual(self.service.state()['events'][key]['local']['actual']['origin'],'manual')
        self.service.annotate(key,applied['revision'],True,undo=applied['undo']);self.assertEqual(self.service.state()['events'][key]['local'],before)
        state=self.service.state();self.service.annotate(key,state['revision'],True,actual={'start':'2026-10-09T01:00:00Z','end':'2026-10-09T02:45:00Z'});self.run_import();self.assertEqual(self.service.state()['events'][key]['local']['actual']['end'],instant('2026-10-09T02:45:00Z'))
    def test_measured_actual_never_replaced_with_planned_or_drag(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));local=state['events'][key]['local'];local['work_sessions']=[{'start':'2026-10-09T01:10:00Z','end':'2026-10-09T02:10:00Z'}];write_json(self.service.directory/'state.json',state)
        self.service.annotate(key,state['revision'],True);state=self.service.state();self.assertEqual(state['events'][key]['local']['actual']['origin'],'measured');self.assertEqual(state['events'][key]['local']['actual']['start'],instant('2026-10-09T01:10:00Z'))
        with self.assertRaisesRegex(ImportError,'measured'):self.service.annotate(key,state['revision'],True,actual={'start':event()['start'],'end':event()['end']})
    def test_open_measurement_blocks_estimated_completion(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));state['events'][key]['local']['work_sessions']=[{'start':event()['start'],'end':None}];write_json(self.service.directory/'state.json',state);before=self.state_bytes()
        with self.assertRaisesRegex(ImportError,'measurement_running'):self.service.annotate(key,state['revision'],True)
        self.assertEqual(before,self.state_bytes())
    def test_invalid_interval_preserves_state_and_undo_conflicts_after_reimport(self):
        self.run_import();state=self.service.state();key=next(iter(state['events']));before=self.state_bytes()
        with self.assertRaises(ImportError):self.service.annotate(key,state['revision'],True,actual={'start':event()['end'],'end':event()['start']})
        self.assertEqual(before,self.state_bytes());applied=self.service.annotate(key,state['revision'],True);self.run_import()
        with self.assertRaises(ImportError):self.service.annotate(key,applied['revision'],True,undo=applied['undo'])

class StateValidationTest(unittest.TestCase):
    def test_corrupt_state_fails_closed_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            service=OutlookImport(Path(temporary),collector=lambda *_:snapshot([event()]));service.run('classic',FIRST,LAST)
            state=service.state();key=next(iter(state['events']));path=service.directory/'state.json'
            for malformed in [None, {}, 'invalid', [{'start':event()['end'],'end':event()['start']}]]:
                value=copy.deepcopy(state);value['events'][key]['local']['work_sessions']=malformed;write_json(path,value);before=path.read_bytes()
                with self.assertRaises(ImportError):service.run('classic',FIRST,LAST)
                self.assertEqual(path.read_bytes(),before)
            for raw in [b'{"version":1,"version":2}',b'{"revision":NaN}']:
                path.write_bytes(raw)
                with self.assertRaises(ImportError):service.state()
                self.assertEqual(path.read_bytes(),raw)

class BackupAndAtomicTest(unittest.TestCase):
    def test_outlook_state_backup_restore_and_safety_backup(self):
        import importlib.util
        path=Path(__file__).resolve().parents[1]/'distribution/mokvia/files/local_runtime.py'
        if not path.exists():path=Path(__file__).resolve().parents[1]/'local_runtime.py'
        spec=importlib.util.spec_from_file_location('outlook_test_runtime',path);runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'data';runtime.initialize(root);service=OutlookImport(root,collector=lambda *_:snapshot([event()]));service.run('classic',FIRST,LAST);state=service.state();key=next(iter(state['events']));service.annotate(key,state['revision'],True);expected=(service.directory/'state.json').read_bytes();archive=Path(temporary)/'backup.tar';runtime.backup(root,archive)
            service.annotate(key,service.state()['revision'],False);runtime.restore(root,archive);self.assertEqual((service.directory/'state.json').read_bytes(),expected)
    def test_atomic_replace_failure_keeps_prior_bytes(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            service=OutlookImport(Path(temporary),collector=lambda *_:snapshot([event()]));service.run('classic',FIRST,LAST);before=(service.directory/'state.json').read_bytes();state=service.state();key=next(iter(state['events']))
            with patch('webapp.outlook_import.os.replace',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):service.annotate(key,state['revision'],True)
            self.assertEqual((service.directory/'state.json').read_bytes(),before);self.assertFalse(list(service.directory.glob('*.tmp')))
