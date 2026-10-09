"""Core external-calendar snapshots and local annotations. Never writes to Outlook or Task files."""
from __future__ import annotations
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

UTC = dt.timezone.utc
MAX_BYTES = 8 * 1024 * 1024
class ImportError(ValueError):
    pass

def stamp():
    return dt.datetime.now(UTC).isoformat(timespec='seconds')

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def instant(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ImportError('invalid_time')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(UTC).isoformat()
    except ValueError:
        raise ImportError('invalid_time') from None

def window(first, last):
    first, last = instant(first), instant(last)
    if not first < last or dt.datetime.fromisoformat(last)-dt.datetime.fromisoformat(first) > dt.timedelta(days=93):
        raise ImportError('invalid_range')
    return first, last

def text(value, limit=2048):
    if not isinstance(value, str) or not value or len(value)>limit or any(ord(c)<32 for c in value):
        raise ImportError('invalid_text')
    return value

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportError('invalid_json')
        result[key] = value
    return result

def _invalid_constant(value):
    raise ImportError('invalid_json')

def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode) or os.fstat(file.fileno()).st_size > MAX_BYTES:
            raise ImportError('invalid_file')
        raw = file.read(MAX_BYTES+1)
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError):
        raise ImportError('invalid_json') from None

def write_json(path, value):
    path = Path(path)
    raw = canonical(value).encode()
    if len(raw) > MAX_BYTES:
        raise ImportError('state_too_large')
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(raw); file.flush(); os.fsync(file.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temporary.exists(): temporary.unlink()

def normalize_snapshot(payload, source, first, last):
    """Complete finite snapshots only; both adapters supply occurrence identities."""
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('complete') is not True or payload.get('source') != source:
        raise ImportError('incomplete_snapshot')
    if payload.get('from') != first or payload.get('to') != last:
        raise ImportError('range_mismatch')
    scope = text(payload.get('scope'))
    rows = payload.get('events')
    if not isinstance(rows, list) or len(rows)>5000:
        raise ImportError('invalid_events')
    result = {}
    for row in rows:
        if not isinstance(row, dict): raise ImportError('invalid_event')
        uid = text(row.get('uid')); occurrence = row.get('occurrence', '')
        if not isinstance(occurrence, str): raise ImportError('invalid_identity')
        if occurrence: occurrence = instant(occurrence)
        identity = canonical([source, scope, uid, occurrence])
        key = hashlib.sha256(identity.encode()).hexdigest()
        start, end = instant(row.get('start')), instant(row.get('end'))
        if not start < end: raise ImportError('invalid_interval')
        if type(row.get('cancelled', False)) is not bool or type(row.get('all_day', False)) is not bool: raise ImportError('invalid_event')
        event = {'key': key, 'source': source, 'scope': scope, 'uid': uid, 'occurrence': occurrence,
                 'title': text(row.get('title') or '予定', 300), 'start': start, 'end': end,
                 'all_day': row.get('all_day', False), 'cancelled': row.get('cancelled', False)}
        if key in result and result[key] != event: raise ImportError('duplicate_identity_conflict')
        result[key] = event
    return result

class ExternalEvents:
    def __init__(self, root, handoff=None, *, collector=None, timeout=25, sources=(), state_namespace='external-events'):
        self.sources=tuple(sources)
        if not state_namespace or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in state_namespace):raise ImportError('invalid_namespace')
        self.root = Path(root); self.handoff = Path(handoff) if handoff else None
        self.collector = collector; self.timeout = timeout
        if self.handoff and (not self.handoff.is_absolute() or not self.handoff.is_dir() or self.handoff.is_symlink()):
            raise ImportError('invalid_handoff')
        self.directory = self.root/'.local-state'/state_namespace
        for directory in (self.root/'.local-state', self.directory):
            if directory.is_symlink(): raise ImportError('unsafe_state')
        # No files/directories are created merely by reading the calendar.
    @contextlib.contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.root/'.local-state', self.directory):
            if directory.is_symlink(): raise ImportError('unsafe_state')
        fd = os.open(self.directory/'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ImportError('busy') from None
            yield
        finally: os.close(fd)
    def state(self):
        path = self.directory/'state.json'
        if not path.exists() and not path.is_symlink(): return {'version':1, 'revision':0, 'events':{}, 'last_import':None, 'binding':None}
        value = read_json(path)
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('events'), dict) or type(value.get('revision')) is not int or value['revision'] < 0:
            raise ImportError('invalid_state')
        for key,event in value['events'].items():
            if not isinstance(event,dict) or event.get('source') not in self.sources or not isinstance(event.get('local'),dict) or type(event['local'].get('done')) is not bool or event.get('presence') not in ('present','unseen','cancelled') or type(event.get('needs_review')) is not bool:
                raise ImportError('invalid_state')
            normalized=normalize_snapshot({'version':1,'source':event['source'],'scope':event.get('scope'),'complete':True,'from':'a','to':'b','events':[event]},event['source'],'a','b')
            if key not in normalized:raise ImportError('invalid_state')
            actual=event['local'].get('actual')
            if actual is not None and (not isinstance(actual,dict) or actual.get('origin') not in ('planned','manual','measured') or not instant(actual.get('start'))<instant(actual.get('end'))):raise ImportError('invalid_state')
            sessions=event['local'].get('work_sessions', [])
            if not isinstance(sessions,list):raise ImportError('invalid_state')
            previous_end=None
            for index,session in enumerate(sessions):
                if not isinstance(session,dict):raise ImportError('invalid_state')
                first=instant(session.get('start'));last=instant(session['end']) if session.get('end') else None
                if (last and last<first) or (previous_end and first<previous_end) or (last is None and index!=len(sessions)-1):raise ImportError('invalid_state')
                previous_end=last
        return value
    def status(self):
        value = self.state()
        return {'enabled': bool(self.handoff or self.collector), 'revision':value['revision'], 'last_import':value['last_import'], 'binding':value.get('binding'), 'events':list(value['events'].values())}
    def collect(self, source, first, last):
        if self.collector: return self.collector(source, first, last)
        if not self.handoff: raise ImportError('not_configured')
        request_id = uuid.uuid4().hex
        request = self.handoff/(request_id+'.request.json'); response = self.handoff/(request_id+'.response.json')
        write_json(request, {'version':1, 'request_id':request_id, 'source':source, 'from':first, 'to':last})
        try:
            deadline = time.monotonic()+self.timeout
            while time.monotonic() < deadline:
                if response.exists() or response.is_symlink():
                    value=read_json(response)
                    if value.get('request_id') != request_id or value.get('ok') is not True: raise ImportError('collector_failed')
                    return value.get('snapshot')
                time.sleep(.1)
            raise ImportError('collector_timeout')
        finally:
            request.unlink(missing_ok=True); response.unlink(missing_ok=True)
    def run(self, source, first, last):
        if source not in self.sources: raise ImportError('invalid_source')
        first,last=window(first,last)
        with self.lock():
            old=self.state()
            payload=self.collect(source,first,last)
            fresh=normalize_snapshot(payload,source,first,last)
            binding={'source':source,'scope':payload['scope']}
            if old.get('binding') and old['binding'] != binding: raise ImportError('source_change_requires_migration')
            value=copy.deepcopy(old); value['binding']=binding; counts={'added':0,'changed':0,'cancelled':0,'unseen':0,'review':0}
            scopes={payload['scope']}
            # Empty valid snapshots still carry a source scope.
            # Collector scope is retained even when no event is returned.
            for key,event in fresh.items():
                prior=old['events'].get(key)
                local=copy.deepcopy(prior.get('local',{})) if prior else {'done':False,'completed_at':None,'work_sessions':[]}
                important=bool(prior and any(prior[k] != event[k] for k in ('title','start','end','all_day')))
                needs_review=bool(prior and prior.get('needs_review')) or bool(local.get('done') and important)
                value['events'][key]={**event,'local':local,'presence':'cancelled' if event['cancelled'] else 'present','needs_review':needs_review}
                counts['added' if not prior else 'changed'] += int(not prior or any(prior.get(k)!=v for k,v in event.items()))
                counts['cancelled'] += int(event['cancelled'] and (not prior or not prior.get('cancelled')))
                counts['review'] += int(needs_review)
            for key,prior in value['events'].items():
                if key not in fresh and prior['source']==source and (not scopes or prior['scope'] in scopes) and prior['start']<last and prior['end']>first and not prior['cancelled']:
                    prior['presence']='unseen';counts['unseen']+=1
            value['undo']=None;value['revision']+=1;value['last_import']={'at':stamp(),'source':source,'from':first,'to':last,'counts':counts}
            write_json(self.directory/'state.json',value)
            return {**counts,'revision':value['revision']}
    def annotate(self, key, revision, done, *, actual=None, undo=None):
        if type(done) is not bool or type(revision) is not int: raise ImportError('invalid_annotation')
        with self.lock():
            value=self.state()
            if revision!=value['revision']: raise ImportError('conflict')
            if key not in value['events']: raise ImportError('not_found')
            event=value['events'][key]
            if event['cancelled'] or event['presence']=='unseen': raise ImportError('event_unavailable')
            local=event['local']
            if undo is not None:
                if not isinstance(undo, dict) or undo.get('revision')!=revision or undo.get('key')!=key or 'before' not in undo:
                    raise ImportError('invalid_undo')
                before=undo['before']
                if not isinstance(before, dict) or type(before.get('done')) is not bool:raise ImportError('invalid_undo')
                # Only a previously stored, server-owned last change can be undone.
                pending=value.get('undo')
                if pending!=undo or dt.datetime.now(UTC)-dt.datetime.fromisoformat(undo['at'])>dt.timedelta(seconds=30):raise ImportError('undo_expired')
                event['local']=copy.deepcopy(before);value['undo']=None
            else:
                before=copy.deepcopy(local)
                measured=(local.get('actual') or {}).get('origin')=='measured' or bool(local.get('work_sessions'))
                if done and any(not session.get('end') for session in local.get('work_sessions',[])):
                    raise ImportError('measurement_running')
                if actual is not None:
                    if measured:raise ImportError('measured_time_requires_correction')
                    if not isinstance(actual,dict) or set(actual)!={'start','end'}:raise ImportError('invalid_actual')
                    first,last=instant(actual['start']),instant(actual['end'])
                    if not first<last or dt.datetime.fromisoformat(last)-dt.datetime.fromisoformat(first)>dt.timedelta(days=7):raise ImportError('invalid_actual')
                    local['actual']={'start':first,'end':last,'origin':'manual'}
                elif done and not local.get('actual'):
                    sessions=local.get('work_sessions',[])
                    if sessions:
                        local['actual']={'start':instant(sessions[0]['start']),'end':instant(sessions[-1]['end']),'origin':'measured'}
                    elif not event['all_day']:
                        local['actual']={'start':event['start'],'end':event['end'],'origin':'planned'}
                if local.get('done')!=done:
                    local['done']=done;local['completed_at']=stamp() if done else None
                if not done:event['needs_review']=False
                if local==before:return {'revision':value['revision'], 'undo':value.get('undo')}
                value['undo']={'key':key,'before':before,'revision':value['revision']+1,'at':stamp()}
            value['revision']+=1;write_json(self.directory/'state.json',value)
            return {'revision':value['revision'], 'undo':value.get('undo')}
