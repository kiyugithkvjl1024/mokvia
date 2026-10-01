import contextlib
import datetime as dt
import io
import pathlib
import tarfile
import tempfile
import unittest
from webapp.store import Store
from scripts.validate_frontmatter import validate_repository

class RuntimeTest(unittest.TestCase):
    def test_initialize_empty_external_root_idempotently(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t:
            root = pathlib.Path(t)
            r.initialize(root); r.initialize(root)
            self.assertEqual([], validate_repository(root))
            self.assertFalse(list((root/'tasks').glob('*.md')))
            self.assertTrue((root/'reviews/weekly').is_dir())
            self.assertFalse((root/'.git').exists())
    def test_duplicate_server_claim_fails(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t:
            root = pathlib.Path(t); r.initialize(root)
            with r.runtime_claim(root):
                with self.assertRaises(RuntimeError):
                    with r.runtime_claim(root): pass
    def test_backup_restore_roundtrip_and_traversal_rejection(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as o:
            root = pathlib.Path(t); r.initialize(root)
            archive = pathlib.Path(o)/'backup.tar'
            Store(root).create_entity('task', {'title':'Synthetic','status':'next'}, '')
            r.backup(root,archive)
            original={p.relative_to(root):p.read_bytes() for p in root.rglob('*.md')}
            for p in (root/'tasks').glob('*.md'): p.unlink()
            r.restore(root,archive)
            self.assertEqual(original,{p.relative_to(root):p.read_bytes() for p in root.rglob('*.md')})
            malicious=pathlib.Path(o)/'malicious.tar'
            with tarfile.open(malicious,'w') as tar:
                item=tarfile.TarInfo('../escaped'); item.size=1; tar.addfile(item,io.BytesIO(b'x'))
            with self.assertRaises(ValueError): r.restore(root,malicious)
            self.assertEqual(original,{p.relative_to(root):p.read_bytes() for p in root.rglob('*.md')})
    def test_notification_resume_deduplicates_and_coalesces(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t:
            root=pathlib.Path(t); r.initialize(root)
            n=r.Notifications(root)
            at=dt.datetime(2026,10,1,9,0,tzinfo=dt.timezone(dt.timedelta(hours=9)))
            n.publish('one','One',at); n.publish('one','One',at)
            first=n.current(at); self.assertEqual('One',first['text'])
            n.publish('two','Two',at+dt.timedelta(hours=2))
            self.assertEqual('Two',n.current(at+dt.timedelta(hours=2))['text'])
            n.heartbeat(at); self.assertTrue(n.current(at)['native_active'])
            self.assertFalse(n.current(at+dt.timedelta(seconds=91))['native_active'])
    def test_restore_refuses_running_service(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as o:
            root=pathlib.Path(t); r.initialize(root); p=pathlib.Path(o)/'backup.tar'; r.backup(root,p)
            with r.runtime_claim(root):
                with self.assertRaises(RuntimeError): r.restore(root,p)

class TickTest(unittest.TestCase):
    def test_availability_release_is_read_back_and_idempotent(self):
        import local_runtime as r
        from unittest.mock import patch
        now=dt.datetime.fromisoformat('2026-11-01T09:30:00+09:00')
        with tempfile.TemporaryDirectory() as t:
            root=pathlib.Path(t);r.initialize(root)
            with contextlib.closing(Store(root)) as store:
                entity=store.create_entity('task',{'title':'Synthetic gate','status':'waiting','available_from':'2026-11-01'},'')
                notifications=r.Notifications(root)
                with patch('webapp.store.current_time',return_value=now):r.tick(store,notifications,now)
                released=store.get_entity(entity.entity_id)
                self.assertEqual('next',released.frontmatter['status'])
                with patch('webapp.store.current_time',return_value=now):r.tick(store,notifications,now)
                self.assertEqual(released.content_hash,store.get_entity(entity.entity_id).content_hash)
    def test_break_completes_at_deadline_once_after_resume(self):
        import local_runtime as r
        from unittest.mock import patch
        now=dt.datetime.fromisoformat('2026-11-01T09:00:00+09:00')
        with tempfile.TemporaryDirectory() as t:
            root=pathlib.Path(t);r.initialize(root)
            with contextlib.closing(Store(root)) as store:
                with patch('webapp.store.current_time',return_value=now):
                    plan=store.plan_task_start_break()
                    store.apply_task_workflow(plan)
                entity=store.get_entity(plan.effects[-1].entity_id)
                notifications=r.Notifications(root)
                r.tick(store,notifications,now+dt.timedelta(minutes=4))
                self.assertEqual('doing',store.get_entity(entity.entity_id).frontmatter['status'])
                r.tick(store,notifications,now+dt.timedelta(minutes=60))
                done=store.get_entity(entity.entity_id)
                self.assertEqual('done',done.frontmatter['status'])
                self.assertEqual(entity.frontmatter['timer_ends_at'],done.frontmatter['work_ended_at'])
                self.assertIn('休憩',notifications.current(now+dt.timedelta(minutes=60))['text'])
    def test_focus_threshold_and_quiet_hours(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t:
            root=pathlib.Path(t);r.initialize(root)
            with contextlib.closing(Store(root)) as store:
                store.create_entity('task',{'title':'Synthetic focus','status':'doing','work_started_at':'2026-11-01T09:00:00+09:00','resume_status':'next'},'')
                notifications=r.Notifications(root)
                r.tick(store,notifications,dt.datetime.fromisoformat('2026-11-01T09:24:59+09:00'))
                self.assertIsNone(notifications.event['id'])
                r.tick(store,notifications,dt.datetime.fromisoformat('2026-11-01T09:25:00+09:00'))
                self.assertIn('25分',notifications.event['text'])
                last=notifications.event['id']
                r.tick(store,notifications,dt.datetime.fromisoformat('2026-11-01T09:25:10+09:00'))
                self.assertEqual(last,notifications.event['id'])
                r.tick(store,notifications,dt.datetime.fromisoformat('2026-11-01T22:00:00+09:00'))
                self.assertEqual(last,notifications.event['id'])
