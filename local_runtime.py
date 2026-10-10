"""Local Linux runtime; no external API, Git, or AI calls."""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tarfile
import tempfile
import threading
import time
from webapp.store import Store, focus_monitor_pause_deadline
from webapp.notification_preferences import Preferences
from scripts.validate_frontmatter import validate_repository

JST = dt.timezone(dt.timedelta(hours=9))
DIRECTORIES = ('inbox','tasks','projects','goals','purposes','visions','areas','roadmap-outcomes','cycles','time-allocation-plans','progress','reviews/daily','reviews/weekly','archive')
DATA_ROOT = Path(os.environ.get('MOKVIA_DATA_ROOT','/data'))
ORIGINS = ('http://localhost:24873','http://127.0.0.1:24873')

def initialize(root: Path):
    root.mkdir(parents=True,exist_ok=True)
    if root.is_symlink(): raise ValueError('data root must not be a symlink')
    for name in DIRECTORIES:
        path=root/name
        if path.is_symlink(): raise ValueError('data directory must not be a symlink')
        path.mkdir(parents=True,exist_ok=True)
    if (root/'.local-state').is_symlink(): raise ValueError('local state must not be a symlink')
    (root/'.local-state').mkdir(mode=0o700,exist_ok=True)

@contextlib.contextmanager
def runtime_claim(root: Path):
    path=root/'.local-state/runtime.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('mokvia already running; refusing duplicate startup')
        yield
    finally: os.close(fd)

def _archive(root: Path, output: Path):
    if output.resolve().is_relative_to(root.resolve()) and output.parent != root/'.local-state':
        raise ValueError('backup output must be outside canonical data')
    with tarfile.open(output,'w') as tar:
        for name in DIRECTORIES:
            base=root/name
            if not base.exists(): continue
            for path in sorted(base.rglob('*')):
                if path.is_symlink(): raise ValueError('symlink in backup')
                if path.is_file() and path.suffix=='.md': tar.add(path,arcname=str(path.relative_to(root)),recursive=False)
        state=root/'.webapp-mutation-state'
        if state.exists(): tar.add(state,arcname=state.name,recursive=False)
        from webapp.local_capture import directory, receipt_records, read_file
        receipts=root/'.local-state/capture-receipts'
        if receipts.exists() or receipts.is_symlink():
            with directory(receipts) as fd:
                for key in receipt_records(fd):
                    raw=read_file(fd,key+'.json')[0]
                    member=tarfile.TarInfo('.local-state/capture-receipts/'+key+'.json')
                    member.size=len(raw); member.mode=0o600
                    tar.addfile(member,__import__('io').BytesIO(raw))

    from webapp.outlook_import import OutlookImport
    outlook=OutlookImport(root)
    if (outlook.directory/'state.json').exists() or (outlook.directory/'state.json').is_symlink():
        outlook.state()  # Validate before exporting; no handoff files or credentials.
        raw=(outlook.directory/'state.json').read_bytes()
        with tarfile.open(output,'a') as tar:
            member=tarfile.TarInfo('.local-state/outlook-import/state.json');member.size=len(raw);member.mode=0o600
            tar.addfile(member,__import__('io').BytesIO(raw))

    preferences=Preferences(root);preferences.read()
    if preferences.path.exists():
        with tarfile.open(output,'a') as tar: tar.add(preferences.path,arcname='.local-state/notifications/settings.json',recursive=False)
    from webapp.integrations.state import IntegrationState
    with tarfile.open(output,'a') as tar:
        for path in IntegrationState(root).snapshot_files():
            raw=path.read_bytes();member=tarfile.TarInfo(str(path.relative_to(root)));member.size=len(raw);member.mode=0o600
            tar.addfile(member,__import__('io').BytesIO(raw))

def backup(root: Path, output: Path):
    initialize(root)
    from webapp.outlook_import import OutlookImport
    from webapp.integrations.state import IntegrationState
    from webapp.timetracker_nx import Journal
    nx_path=IntegrationState(root).directory/'timetracker-nx.json'
    nx_lock=Journal(nx_path).exclusive() if nx_path.exists() else contextlib.nullcontext()
    with runtime_claim(root), contextlib.closing(Store(root)) as store, store.mutation_lock(), OutlookImport(root).lock(), IntegrationState(root).lock(), Preferences(root).lock(), nx_lock:
        errors=validate_repository(root)
        if errors: raise ValueError('data validation failed; backup refused')
        output.parent.mkdir(parents=True,exist_ok=True)
        _archive(root,output)

def restore(root: Path, archive: Path):
    initialize(root)
    with runtime_claim(root), tempfile.TemporaryDirectory() as temp:
        staging=Path(temp); initialize(staging)
        with tarfile.open(archive,'r') as tar:
            members=tar.getmembers()
            if len(members)>100000 or sum(m.size for m in members)>512*1024*1024: raise ValueError('archive too large')
            seen=set()
            for member in members:
                p=Path(member.name)
                if p.is_absolute() or '..' in p.parts or member.name in seen or not member.isfile(): raise ValueError('unsafe archive member')
                seen.add(member.name)
                valid=any(p.is_relative_to(Path(d)) for d in DIRECTORIES) and p.suffix=='.md'
                receipt=re.fullmatch(r'\.local-state/capture-receipts/[0-9a-f]{64}\.json',member.name)
                integration=re.fullmatch(r'\.local-state/integrations/(?:settings|timetracker-nx|receipt-[0-9a-f]{64})\.json',member.name)
                if not valid and not integration and member.name!='.webapp-mutation-state' and not receipt and member.name!='.local-state/outlook-import/state.json' and member.name!='.local-state/notifications/settings.json': raise ValueError('unexpected archive member')
            tar.extractall(staging,members=members,filter='data')
        if validate_repository(staging): raise ValueError('restored data fails schema validation')
        recovery=staging/'.webapp-mutation-state'
        if recovery.exists() and recovery.read_bytes()!=b'idle\n': raise ValueError('archive requires mutation recovery')
        from webapp.local_capture import directory, receipt_records
        staged_receipts=staging/'.local-state/capture-receipts'
        if staged_receipts.exists():
            with directory(staged_receipts) as fd: receipt_records(fd)
        from webapp.outlook_import import OutlookImport
        OutlookImport(staging).state()
        from webapp.integrations.state import IntegrationState
        IntegrationState(staging).snapshot_files()
        Preferences(staging).read()
        with contextlib.closing(Store(root)) as store,store.mutation_lock():
            stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            safety=root/'.local-state'/f'pre-restore-{stamp}.tar'; _archive(root,safety)
            marker=root/'.local-state/restore-incomplete'
            marker.write_text(safety.name)
            # Keep marker on any interrupted restore; startup refuses until operator recovery.
            for name in {d.split('/')[0] for d in DIRECTORIES}:
                target=root/name
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(staging/name,target)
            if recovery.exists(): shutil.copyfile(recovery,root/recovery.name)
            elif (root/'.webapp-mutation-state').exists(): (root/'.webapp-mutation-state').unlink()
            target_receipts=root/'.local-state/capture-receipts'
            if target_receipts.exists(): shutil.rmtree(target_receipts)
            if staged_receipts.exists(): shutil.copytree(staged_receipts,target_receipts)
            target_outlook=root/'.local-state/outlook-import'
            staged_outlook=staging/'.local-state/outlook-import'
            if target_outlook.exists(): shutil.rmtree(target_outlook)
            if staged_outlook.exists(): shutil.copytree(staged_outlook,target_outlook)
            target_integrations=root/'.local-state/integrations'
            staged_integrations=staging/'.local-state/integrations'
            if target_integrations.exists():shutil.rmtree(target_integrations)
            if staged_integrations.exists():shutil.copytree(staged_integrations,target_integrations)
            target_notifications=root/'.local-state/notifications'
            staged_notifications=staging/'.local-state/notifications'
            if target_notifications.exists(): shutil.rmtree(target_notifications)
            if staged_notifications.exists(): shutil.copytree(staged_notifications,target_notifications)
            marker.unlink()
        return safety

class Notifications:
    def __init__(self,root):
        self.root=root; self.lock=threading.Lock(); self.native_at=None
        self.event={'id':None,'text':None}; self.last_key=None; self.at=None; self.kind=None; self.preferences=Preferences(root)
    def publish(self,key,text,now,kind=None,occurred_at=None):
        with self.lock:
            if key==self.last_key: return
            self.last_key=key; self.at=now; self.kind=kind
            if not self.preferences.allows(kind,now) or (occurred_at is not None and not self.preferences.allows(kind,occurred_at)):
                self.event={'id':None,'text':None}; return
            self.event={'id':hashlib.sha256(key.encode()).hexdigest()[:24],'text':text}
    def heartbeat(self,now):
        with self.lock: self.native_at=now
    def current(self,now):
        with self.lock:
            active=self.native_at is not None and 0 <= (now-self.native_at).total_seconds()<90
            if not self.preferences.allows(self.kind,now): self.event={'id':None,'text':None}
            value=self.event if self.at and 0 <= (now-self.at).total_seconds()<90 else {'id':None,'text':None}
            return {**value,'native_active':active}

def tick(store,notifications,now):
    snapshot=store.read_snapshot()
    if snapshot.recovery_required: raise RuntimeError('mutation recovery required')
    doing=[e for e in snapshot.entities if e.entity_type=='task' and not e.relative_path.startswith('archive/') and e.frontmatter.get('status')=='doing']
    if len(doing)==1 and doing[0].frontmatter.get('timer_kind')=='break':
        task=doing[0]; end=dt.datetime.fromisoformat(task.frontmatter['timer_ends_at'])
        if now>=end:
            plan=store.plan_break_timer_completion(task.entity_id,task.content_hash,now=now)
            store.apply_task_workflow(plan)
            actual=store.get_entity(task.entity_id)
            if actual.frontmatter.get('status')!='done': raise RuntimeError('break completion readback failed')
            notifications.publish('break:'+task.entity_id,'5分休憩が終わりました',now,'break_finished',end)
        return
    plan=store.plan_available_task_releases(now)
    if plan is not None:
        store.apply_task_workflow(plan)
        for effect in plan.effects:
            if store.get_entity(effect.entity_id).content_hash != effect.planned_entity.content_hash:
                raise RuntimeError("availability release readback failed")
    local=now.astimezone(JST)
    if not 6<=local.hour<22: return
    slot=int(now.timestamp()//300)
    if len(doing)!=1:
        text='進行中タスクなし' if not doing else '監視不能（複数doing）'
        notifications.publish(f'focus:{slot}:{len(doing)}',text,now,'focus_anomaly'); return
    task=doing[0]
    if focus_monitor_pause_deadline(task.body,now): return
    kind='focus_anomaly'
    start=task.frontmatter.get('work_started_at')
    try:
        started=dt.datetime.fromisoformat(start)
        if started.tzinfo is None: raise ValueError()
        elapsed=(now-started).total_seconds()
        if elapsed<0: text='監視不能（開始時刻が未来）'
        elif elapsed<1500: return
        else: text='進行中タスクが25分を超えています。中断または完了してください'; kind='focus_reminder'
    except (TypeError,ValueError): text='開始時刻未記録'
    notifications.publish(f'focus:{task.entity_id}:{slot}',text,now,kind)

def serve(root: Path,*,container=False,capture_folder=None,outlook_folder=None,timetracker_config=None):
    initialize(root)
    if (root/'.local-state/restore-incomplete').exists(): raise RuntimeError('restore incomplete; operator recovery required')
    if validate_repository(root): raise RuntimeError('data validation failed')
    from webapp.server import create_server
    with runtime_claim(root):
        notifications=Notifications(root)
        def extra(store,method,path,query,headers,body):
            now=dt.datetime.now(dt.timezone.utc)
            if path=='/api/v1/local-notifications':
                if method!='GET' or query: return 400,{'error':{'code':'invalid_request'}}
                return 200,notifications.current(now)
            if path=='/api/v1/local-notifications/heartbeat':
                if method!='POST' or query or body!=b'{}': return 400,{'error':{'code':'invalid_request'}}
                notifications.heartbeat(now); return 200,{'status':'ok'}
            return None
        server=create_server('0.0.0.0' if container else '127.0.0.1',24873,root,bind_origin=ORIGINS[0],mutation_origins=ORIGINS,extra_handler=extra,health_details={'distribution':'mokvia','mode':'local'},strict_hosts=('localhost:24873','127.0.0.1:24873'),capture_folder=capture_folder,outlook_folder=outlook_folder,timetracker_config=timetracker_config)
        stopped=threading.Event()
        def worker():
            while not stopped.is_set():
                try: tick(server._mokvia_store,notifications,dt.datetime.now(dt.timezone.utc))
                except Exception:
                    notifications.publish('monitor-failed:'+str(int(time.time()//300)),'監視不能（状態を確認してください）',dt.datetime.now(dt.timezone.utc),'focus_anomaly')
                    # Stateful failures stop automatic mutations; no retry of an unknown apply.
                    return
                stopped.wait(10)
        thread=threading.Thread(target=worker,daemon=True); thread.start()
        def stop(*_):
            stopped.set(); threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        print('mokvia ready at http://localhost:24873',flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally:
            stopped.set(); thread.join(timeout=15)
            server.server_close()

def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init'); p=sub.add_parser('serve'); p.add_argument('--container',action='store_true')
    p.add_argument('--outlook-folder',type=Path,help='Explicit dedicated read-write local Outlook command folder; disabled when omitted')
    p.add_argument('--timetracker-config',type=Path,help='Reviewed non-secret NX configuration outside source and data; credentials are environment references only')
    p.add_argument('--capture-folder',type=Path,help='Explicit absolute read-only company handoff folder; disabled when omitted')
    p=sub.add_parser('backup'); p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('restore'); p.add_argument('--input',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.command=='init': initialize(DATA_ROOT)
        elif args.command=='serve': serve(DATA_ROOT,container=args.container,capture_folder=args.capture_folder,outlook_folder=args.outlook_folder,timetracker_config=args.timetracker_config)
        elif args.command=='backup': backup(DATA_ROOT,args.output)
        else: print('Pre-restore backup:',restore(DATA_ROOT,args.input))
    except Exception as error:
        print('mokvia operation failed:',type(error).__name__,flush=True); return 1
    return 0
if __name__=='__main__': raise SystemExit(main())
