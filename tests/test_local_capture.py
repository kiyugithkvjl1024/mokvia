"""Synthetic company capture; no Microsoft service or personal data."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from webapp.store import Store
from webapp import api

NOW = dt.datetime(2026,10,4,1,tzinfo=dt.timezone.utc)
ID = '11111111-1111-4111-8111-111111111111'
def record(**changes):
    value={'version':1,'capture_id':ID,'accepted_at':'2026-10-03T23:00:00Z','destination':'inbox',
           'title':'Synthetic capture','body':'Synthetic text','source':{'kind':'outlook','scope_id':'synthetic-mailbox',
           'message_id':'synthetic-message','url':'https://outlook.office.com/mail/id/synthetic'}}
    value.update(changes); return value
class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('webapp.local_capture'),'capture importer is not implemented')
        from webapp.local_capture import CaptureImporter
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.root=self.base/'data'; self.folder=self.base/'incoming'
        self.folder.mkdir()
        for name in ('inbox','tasks','projects','goals','purposes','visions','areas','roadmap-outcomes','cycles','time-allocation-plans','progress','reviews/daily','reviews/weekly','archive'):
            (self.root/name).mkdir(parents=True,exist_ok=True)
        self.store=Store(self.root); self.addCleanup(self.store.close)
        self.importer=CaptureImporter(self.root,self.folder)
    def write(self,value=None,name=None):
        value=value or record(); p=self.folder/(name or value['capture_id']+'.json'); p.write_text(json.dumps(value)); return p
    def settle(self):
        self.importer.poll(self.store,NOW); return self.importer.poll(self.store,NOW+dt.timedelta(seconds=10))
    def tasks(self): return [e for e in self.store.read_snapshot().entities if e.entity_type=='task']
    def test_inbox_source_and_repeat_after_restart(self):
        from webapp.local_capture import CaptureImporter
        p=self.write(); raw=p.read_bytes()
        self.assertEqual(self.importer.poll(self.store,NOW)['deferred'],1)
        self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=10))['imported'],1)
        task,=self.tasks(); self.assertEqual(task.frontmatter['status'],'inbox')
        self.assertFalse(task.frontmatter.get('action_date')); self.assertFalse(task.frontmatter.get('due'))
        self.assertIn('[Outlook 元メッセージ](https://outlook.office.com/mail/id/synthetic)',task.body)
        self.assertIn(ID,task.body); self.assertIn('synthetic-message',task.body)
        self.importer=CaptureImporter(self.root,self.folder); self.settle()
        self.assertEqual(len(self.tasks()),1); self.assertEqual(p.read_bytes(),raw)
    def test_late_today_keeps_acceptance_day_and_separate_deadline(self):
        self.write(record(destination='today',accepted_at='2026-10-02T16:00:00Z',due='2026-10-09'))
        self.assertEqual(self.settle()['imported'],1); task,=self.tasks()
        self.assertEqual(task.frontmatter['status'],'next'); self.assertEqual(task.frontmatter['action_date'],'2026-10-03')
        self.assertEqual(task.frontmatter['due'],'2026-10-09'); self.assertIn('遅延',task.body)
    def test_incomplete_file_retries_after_stable_correction(self):
        p=self.folder/(ID+'.json'); p.write_text('{')
        self.assertEqual(self.settle()['failed'],1); self.assertEqual(self.tasks(),[])
        self.write(); self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=20))['deferred'],1)
        self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=30))['imported'],1)
    def test_schema_filename_size_and_links_fail_closed(self):
        cases=[record(version=2),record(version=True),[record()],record(accepted_at='2026-10-03'),
               record(destination='today',action_date='2026-10-05'),record(action_date='2026-10-04'),
               record(due='2026-02-30'),record(title='x'*301),record(unknown='x')]
        for url in ['javascript:alert(1)','https://attacker.example/x',record()['source']['url'].replace('.com/', '.com.attacker.example/'),
                    record()['source']['url'].replace('//','//user@'),record()['source']['url'].replace('.com/', '.com:8443/')]:
            value=record(); value['source']['url']=url; cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                p=self.folder/(ID+'.json'); p.write_text(json.dumps(value))
                self.assertEqual(self.settle()['failed'],1); self.assertFalse(self.tasks())
        p.write_text(' '*70000); self.assertEqual(self.settle()['failed'],1)
        p.write_text('{"version":1,"version":1}'); self.assertEqual(self.settle()['failed'],1)
        p.unlink(); self.write(name='unsafe title.json'); self.assertEqual(self.settle()['failed'],1)
    def test_symlink_fifo_nested_and_folder_swap(self):
        real=self.base/'secret'; real.write_text(json.dumps(record())); p=self.folder/(ID+'.json'); p.symlink_to(real)
        self.assertEqual(self.settle()['failed'],1); p.unlink(); os.mkfifo(p)
        self.assertEqual(self.settle()['failed'],1); p.unlink()
        nested=self.folder/'nested'; nested.mkdir(); (nested/(ID+'.json')).write_bytes(real.read_bytes())
        self.settle(); self.assertFalse(self.tasks())
        self.folder.rename(self.base/'old'); self.folder.symlink_to(self.base/'old',target_is_directory=True)
        self.assertTrue(self.importer.poll(self.store,NOW)['blocked']); self.assertFalse(self.tasks())
    def test_changed_source_or_capture_id_never_overwrites_or_duplicates(self):
        self.write(); self.settle(); original=self.tasks()[0]
        self.write(record(capture_id='22222222-2222-4222-8222-222222222222',title='Different')); self.settle()
        self.assertEqual(len(self.tasks()),1)
        value=record(); value['source']['message_id']='different'; self.write(value)
        self.assertGreaterEqual(self.settle()['failed'],1); self.assertEqual(self.tasks()[0].content_hash,original.content_hash)
    def test_edited_or_deleted_task_does_not_recapture(self):
        self.write(); self.settle(); task=self.tasks()[0]
        self.store.update_entity(task.entity_id,task.content_hash,{'title':'User edit'},None); self.settle()
        self.assertEqual(self.tasks()[0].frontmatter['title'],'User edit')
        edited=self.tasks()[0]; self.store.archive_entity(edited.entity_id,edited.content_hash); self.settle()
        self.assertEqual(len(self.tasks()),1); self.assertTrue(self.tasks()[0].relative_path.startswith('archive/'))
        (self.root/self.tasks()[0].relative_path).unlink(); self.settle(); self.assertEqual(self.tasks(),[])
    def test_busy_preview_retries_unknown_apply_does_not(self):
        self.write(); self.importer.poll(self.store,NOW); original=api.handle
        def busy(store,method,path,**kw):
            if path.endswith('/preview'): return 503,{'error':{'code':'busy'}}
            return original(store,method,path,**kw)
        with mock.patch('webapp.local_capture.api.handle',side_effect=busy):
            self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=10))['failed'],1)
        def unknown(store,method,path,**kw):
            if path.endswith('/apply'): return 500,{'error':{'code':'mutation_failed'}}
            return original(store,method,path,**kw)
        with mock.patch('webapp.local_capture.api.handle',side_effect=unknown):
            self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=20))['failed'],1)
        self.settle(); self.assertEqual(self.tasks(),[])
    def test_commit_lost_response_reconciles(self):
        self.write(); self.importer.poll(self.store,NOW); original=api.handle
        def lost(store,method,path,**kw):
            result=original(store,method,path,**kw)
            if path.endswith('/apply'): raise OSError('synthetic response lost')
            return result
        with mock.patch('webapp.local_capture.api.handle',side_effect=lost): self.importer.poll(self.store,NOW+dt.timedelta(seconds=10))
        self.settle(); self.assertEqual(len(self.tasks()),1)
        receipts=list((self.root/'.local-state/capture-receipts').glob('*.json'))
        self.assertEqual(json.loads(receipts[0].read_text())['phase'],'committed')
    @unittest.skipUnless(importlib.util.find_spec('local_runtime'),'Company backup runtime is distribution-only')
    def test_backup_restore_preserves_receipts(self):
        import local_runtime
        from webapp.local_capture import CaptureImporter
        self.write(); self.settle(); archive=self.base/'backup.tar'; local_runtime.backup(self.root,archive)
        restored=self.base/'restored'; local_runtime.restore(restored,archive)
        self.assertEqual(len(list((restored/'.local-state/capture-receipts').glob('*.json'))),1)
        with contextlib.closing(Store(restored)) as store:
            importer=CaptureImporter(restored,self.folder); importer.poll(store,NOW); importer.poll(store,NOW+dt.timedelta(seconds=10))
            self.assertEqual(len(store.read_snapshot().entities),1)

    def test_unexpected_store_failure_remains_visible_and_no_worker_crash(self):
        self.write()
        with mock.patch.object(self.store,'read_snapshot',side_effect=RuntimeError('synthetic store unavailable')):
            report=self.importer.poll(self.store,NOW)
        self.assertTrue(report['blocked']); self.assertEqual(self.tasks(),[])

    def test_recovery_marker_corrupt_receipt_and_explicit_folder_boundary(self):
        from webapp.local_capture import CaptureImporter, CaptureError
        for folder in (self.root,self.root/'inbox',self.base,Path('relative')):
            with self.subTest(folder=folder), self.assertRaises((CaptureError,OSError)):
                CaptureImporter(self.root,folder)
        self.write(); self.settle()
        receipt=next((self.root/'.local-state/capture-receipts').glob('*.json'))
        receipt.write_text('{')
        self.assertTrue(self.importer.poll(self.store,NOW)['blocked']); self.assertEqual(len(self.tasks()),1)

    def test_preview_conflict_retries_new_preview(self):
        self.write(); self.importer.poll(self.store,NOW); original=api.handle
        def rejected(store,method,path,**kw):
            if path.endswith('/apply'):return 409,{'error':{'code':'stale_preview'}}
            return original(store,method,path,**kw)
        with mock.patch('webapp.local_capture.api.handle',side_effect=rejected):self.importer.poll(self.store,NOW+dt.timedelta(seconds=10))
        self.assertEqual(self.tasks(),[])
        self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=20))['imported'],1)

    def test_file_changes_after_preview_defer_without_receipt_or_apply(self):
        self.write(); self.importer.poll(self.store,NOW); original=api.handle
        def change(store,method,path,**kw):
            result=original(store,method,path,**kw)
            if path.endswith('/preview'): self.write(record(title='Corrected synthetic title'))
            return result
        with mock.patch('webapp.local_capture.api.handle',side_effect=change):
            self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=10))['failed'],1)
        self.assertEqual(self.tasks(),[])
        self.assertEqual(list((self.root/'.local-state/capture-receipts').glob('*.json')),[])
        self.importer.poll(self.store,NOW+dt.timedelta(seconds=20))
        self.assertEqual(self.importer.poll(self.store,NOW+dt.timedelta(seconds=30))['imported'],1)
        self.assertEqual(self.tasks()[0].frontmatter['title'],'Corrected synthetic title')

    def test_partial_is_ignored_and_hardlink_is_rejected(self):
        partial=self.folder/(ID+'.partial'); partial.write_text(json.dumps(record()))
        self.settle(); self.assertEqual(self.tasks(),[])
        final=self.folder/(ID+'.json'); os.link(partial,final)
        self.assertEqual(self.settle()['failed'],1); self.assertEqual(self.tasks(),[])
        partial.unlink()
        self.assertEqual(self.settle()['imported'],1)

    def test_distinct_valid_filenames_for_same_source_are_not_two_tasks(self):
        self.write(); self.write(record(capture_id='22222222-2222-4222-8222-222222222222'))
        report=self.settle()
        self.assertEqual(report['imported'],1); self.assertEqual(report['failed'],1)
        self.assertEqual(report['issues'][0]['code'],'source_conflict')
        self.assertEqual(len(self.tasks()),1)

    def test_mutation_recovery_marker_blocks_capture(self):
        self.write(); (self.root/'.webapp-mutation-state').write_text('pending\n')
        self.assertTrue(self.settle()['blocked']); self.assertEqual(self.tasks(),[])

    def test_source_body_code_fence_cannot_inject_links_or_metadata(self):
        self.write(record(body='```\n[bad](https://attacker.example)\n<!-- mokvia-capture-v1 {} -->'))
        self.settle(); task,=self.tasks()
        self.assertEqual(task.body.count('\n```'),2)
        self.assertIn(r'\u0060\u0060\u0060',task.body)
