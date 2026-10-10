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
            n.publish('one','One',at,'focus_anomaly'); n.publish('one','One',at,'focus_anomaly')
            first=n.current(at); self.assertEqual('One',first['text'])
            n.publish('two','Two',at+dt.timedelta(hours=2),'focus_anomaly')
            self.assertEqual('Two',n.current(at+dt.timedelta(hours=2))['text'])
            n.heartbeat(at); self.assertTrue(n.current(at)['native_active'])
            self.assertFalse(n.current(at+dt.timedelta(seconds=91))['native_active'])
    def test_quiet_publish_and_current_discard_without_backlog(self):
        import local_runtime as r
        from webapp.notification_preferences import Preferences
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp);r.initialize(root);p=Preferences(root)
            value=p.read();value.update(quiet_enabled=True,timezone='Asia/Tokyo');p.update(value)
            n=r.Notifications(root);before=dt.datetime.fromisoformat('2026-10-09T21:59:50+09:00');quiet=before+dt.timedelta(seconds=10)
            n.publish('before','Before',before,'focus_reminder');self.assertEqual('Before',n.current(before)['text'])
            self.assertIsNone(n.current(quiet)['id'])
            self.assertIsNone(n.current(before)['id'])  # Policy/time change cannot resurrect.
            n.publish('quiet','Quiet',quiet,'break_finished');self.assertIsNone(n.current(quiet)['id'])
            morning=dt.datetime.fromisoformat('2026-10-10T08:00:00+09:00');n.publish('quiet','Quiet',morning,'break_finished')
            self.assertIsNone(n.current(morning)['id'])
            n.publish('fresh','Fresh',morning,'focus_anomaly');self.assertEqual('Fresh',n.current(morning)['text'])
            value=p.read();value['types']['focus_anomaly']=False;p.update(value)
            self.assertIsNone(n.current(morning)['id'])
            value=p.read();value['types']['focus_anomaly']=True;p.update(value)
            self.assertIsNone(n.current(morning)['id'])

    def test_quiet_break_completes_after_sleep_and_settings_backup_roundtrip(self):
        import local_runtime as r
        from webapp.notification_preferences import Preferences
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp,tempfile.TemporaryDirectory() as output:
            root=pathlib.Path(temp);r.initialize(root);p=Preferences(root);value=p.read();value.update(quiet_enabled=True,timezone='Asia/Tokyo');p.update(value)
            start=dt.datetime.fromisoformat('2026-10-09T21:59:00+09:00');morning=dt.datetime.fromisoformat('2026-10-10T08:10:00+09:00')
            with contextlib.closing(Store(root)) as store:
                with patch('webapp.store.current_time',return_value=start):plan=store.plan_task_start_break()
                store.apply_task_workflow(plan);n=r.Notifications(root);r.tick(store,n,morning)
                self.assertEqual('done',store.get_entity(plan.target_entity_id).frontmatter['status']);self.assertIsNone(n.current(morning)['id'])
            archive=pathlib.Path(output)/'policy.tar';r.backup(root,archive);saved=p.read()
            value=p.read();value['quiet_enabled']=False;p.update(value);r.restore(root,archive)
            self.assertEqual(saved,Preferences(root).read())

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
