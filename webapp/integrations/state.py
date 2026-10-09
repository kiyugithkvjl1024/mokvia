"""Non-secret enable flags and durable write receipts, separate from Entity files."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import re
from .contracts import ActualWriteReceipt, fingerprint
from .external_events import read_json, write_json

PLUGIN_IDS=frozenset({'outlook','google_calendar','timetracker_nx'})

class IntegrationError(ValueError):pass

class IntegrationState:
    def __init__(self,root):self.directory=Path(root)/'.local-state'/'integrations'
    def _safe(self):
        if self.directory.is_symlink() or self.directory.parent.is_symlink():raise IntegrationError('unsafe_state')
    @contextmanager
    def lock(self):
        self._safe();self.directory.mkdir(parents=True,exist_ok=True,mode=0o700);self._safe()
        fd=os.open(self.directory/'lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise IntegrationError('busy') from None
            yield
        finally:os.close(fd)
    def flags(self):
        self._safe();path=self.directory/'settings.json'
        if not path.exists() and not path.is_symlink():return {'version':1,'revision':0,'enabled':{}}
        value=read_json(path)
        if not isinstance(value,dict) or set(value)!={'version','revision','enabled'} or value['version']!=1 or type(value['revision']) is not int or value['revision']<0 or not isinstance(value['enabled'],dict) or set(value['enabled'])-PLUGIN_IDS or any(type(v) is not bool for v in value['enabled'].values()):raise IntegrationError('invalid_settings')
        return value
    def enabled(self,plugin_id):return self.flags()['enabled'].get(plugin_id,True)
    def set_enabled(self,plugin_id,enabled,revision):
        if plugin_id not in PLUGIN_IDS or type(enabled) is not bool or type(revision) is not int:raise IntegrationError('invalid_settings')
        with self.lock():
            value=self.flags()
            if value['revision']!=revision:raise IntegrationError('conflict')
            value['enabled'][plugin_id]=enabled;value['revision']+=1;write_json(self.directory/'settings.json',value)
            return {'revision':value['revision']}

    def snapshot_files(self):
        self.flags()
        if not self.directory.exists():return []
        files=[]
        for path in sorted(self.directory.iterdir()):
            if path.is_symlink() or not path.is_file():raise IntegrationError('unsafe_state')
            if path.name in {'lock','timetracker-nx.lock'}:continue
            if path.name=='timetracker-nx.json':
                from webapp.timetracker_nx import Journal
                Journal(path).read()
                files.append(path);continue
            if path.name=='settings.json':files.append(path);continue
            if not re.fullmatch(r'receipt-[0-9a-f]{64}\.json',path.name):raise IntegrationError('unexpected_state_file')
            value=validate_receipt(read_json(path))
            if path.name!='receipt-'+fingerprint([value['plugin_id'],value['reference_id']])+'.json':raise IntegrationError('invalid_receipt')
            files.append(path)
        return files

def validate_receipt(value):
    keys={'plugin_id','state','reference_id','local_revision','idempotency_key','remote_id','remote_revision','error_code'}
    if not isinstance(value,dict) or set(value)!=keys or value['plugin_id'] not in PLUGIN_IDS or value['state'] not in {'pending','applied','unchanged','unknown','conflict','failed'}:raise IntegrationError('invalid_receipt')
    fields={k:v for k,v in value.items() if k!='plugin_id'}
    if fields['state']=='pending':fields['state']='unknown'
    try:ActualWriteReceipt(**fields)
    except (ValueError,TypeError):raise IntegrationError('invalid_receipt') from None
    return value
