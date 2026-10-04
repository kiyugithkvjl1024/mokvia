"""Read one explicitly configured company handoff folder. No network or source writes."""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid
from urllib.parse import quote, urlsplit
from webapp import api
from webapp.security import ALLOWED_MUTATION_ORIGIN, WEB_MARKER_HEADER

JST = dt.timezone(dt.timedelta(hours=9))
MAX_BYTES = 65536
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
FILENAME = re.compile(UUID + r'\.json\Z')
RECEIPT_NAME = re.compile(r'[0-9a-f]{64}\.json\Z')
HOSTS = {'outlook': {'outlook.office.com', 'outlook.office365.com', 'outlook.live.com'},
         'teams': {'teams.microsoft.com', 'teams.cloud.microsoft'}}

class CaptureError(ValueError):
    pass

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise CaptureError('duplicate_json_key')
        value[key] = item
    return value

def decode(raw):
    try: return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(CaptureError('invalid_json')))
    except (UnicodeError, ValueError, RecursionError): raise CaptureError('invalid_json') from None

def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c)<32 for c in value):
        raise CaptureError('invalid_text')
    return value

def timestamp(value):
    text(value, 64)
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|[+-]\d\d:\d\d)',value):
        raise CaptureError('invalid_timestamp')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None: raise ValueError()
        return result
    except ValueError: raise CaptureError('invalid_timestamp') from None

def date(value):
    try:
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d\d-\d\d',value): raise ValueError()
        return dt.date.fromisoformat(value).isoformat()
    except ValueError: raise CaptureError('invalid_date') from None

def validate(value, name):
    required = {'version','capture_id','accepted_at','destination','title','source'}
    if not isinstance(value,dict) or not required <= value.keys() or value.keys() - required - {'body','due','action_date'}:
        raise CaptureError('invalid_schema')
    if type(value['version']) is not int or value['version'] != 1: raise CaptureError('unsupported_version')
    cid = value['capture_id']
    if not isinstance(cid,str) or not re.fullmatch(UUID,cid) or str(uuid.UUID(cid)) != cid or name != cid+'.json':
        raise CaptureError('invalid_filename')
    accepted = timestamp(value['accepted_at']); text(value['title'],300)
    body = value.get('body','')
    if not isinstance(body,str) or len(body)>8000 or any(ord(c)<32 and c not in '\n\r\t' for c in body):
        raise CaptureError('invalid_body')
    if value['destination'] not in ('inbox','today'): raise CaptureError('invalid_destination')
    action_day = accepted.astimezone(JST).date().isoformat()
    if 'action_date' in value and (value['destination']!='today' or date(value['action_date'])!=action_day):
        raise CaptureError('invalid_action_date')
    if 'due' in value: date(value['due'])
    source = value['source']; keys = {'kind','scope_id','message_id','url'}
    if not isinstance(source,dict) or not keys<=source.keys() or source.keys()-keys-{'received_at'}: raise CaptureError('invalid_source')
    if not isinstance(source['kind'],str) or source['kind'] not in HOSTS: raise CaptureError('invalid_source')
    text(source['scope_id'],512); text(source['message_id'],2048); text(source['url'],8192)
    if 'received_at' in source: timestamp(source['received_at'])
    try:
        url=urlsplit(source['url'])
        if (url.scheme!='https' or url.hostname not in HOSTS[source['kind']] or url.username or url.password
                or url.port not in (None,443) or '\\' in source['url'] or any(c.isspace() for c in source['url'])):
            raise ValueError()
    except ValueError: raise CaptureError('invalid_source_url') from None
    return value

@contextlib.contextmanager
def directory(path):
    """Open every path component without following symlinks; never discover other folders."""
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts: raise CaptureError('folder_not_absolute')
    fd=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child
        yield fd
    finally: os.close(fd)

def read_file(fd,name,limit=MAX_BYTES):
    with contextlib.ExitStack() as stack:
        handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd); stack.callback(os.close,handle)
        before=os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size>limit: raise CaptureError('unsafe_file')
        data=b''
        while len(data)<=limit:
            chunk=os.read(handle,min(8192,limit+1-len(data)))
            if not chunk: break
            data+=chunk
        after=os.fstat(handle); current=os.stat(name,dir_fd=fd,follow_symlinks=False)
        signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(data)>limit or signature(before)!=signature(after) or signature(after)!=signature(current):
            raise CaptureError('file_changed')
        return data,signature(current)

def save_receipt(fd,key,value):
    raw=canonical(value).encode(); temp='.'+uuid.uuid4().hex+'.tmp'
    handle=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(handle,'wb') as file: file.write(raw); file.flush(); os.fsync(file.fileno())
        os.replace(temp,key+'.json',src_dir_fd=fd,dst_dir_fd=fd); os.fsync(fd)
    finally:
        try: os.unlink(temp,dir_fd=fd)
        except FileNotFoundError: pass

def receipt_records(fd):
    records={}; seen=set()
    for name in os.listdir(fd):
        if name.startswith('.') and name.endswith('.tmp'): continue
        if not RECEIPT_NAME.fullmatch(name): raise CaptureError('invalid_receipt')
        value=decode(read_file(fd,name)[0]); key=name[:-5]
        keys={'version','source_key','capture_id','record_hash','task_id','phase','planned_hash','accepted_at'}
        if (not isinstance(value,dict) or set(value)!=keys or value['version']!=1 or value['source_key']!=key
                or value['task_id']!='task-capture-'+key[:32] or value['phase'] not in ('pending','committed','retry')
                or not isinstance(value['capture_id'],str) or not re.fullmatch(UUID,value['capture_id'])
                or value['capture_id'] in seen or not re.fullmatch(r'[0-9a-f]{64}',value['record_hash'])
                or not re.fullmatch(r'[0-9a-f]{64}',value['planned_hash'])):
            raise CaptureError('invalid_receipt')
        timestamp(value['accepted_at']); seen.add(value['capture_id']); records[key]=value
    return records

def source_key(value):
    source=value['source']
    return hashlib.sha256(canonical([source['kind'],source['scope_id'],source['message_id']]).encode()).hexdigest()

def operation(value,key,now):
    metadata={k:v for k,v in value.items() if k not in {'body','title'}}
    metadata['record_hash']=hashlib.sha256(canonical(value).encode()).hexdigest()
    encoded=canonical(metadata).replace('<','\\u003c').replace('>','\\u003e')
    label={'outlook':'Outlook','teams':'Teams'}[value['source']['kind']]
    link=quote(value['source']['url'],safe=":/?#@!$&'*+,;=%")
    body=f'<!-- mokvia-capture-v1 {encoded} -->\n\n[{label} 元メッセージ]({link})\n\n初回受付: {value["accepted_at"]}\n'
    fields={'title':value['title'],'status':'inbox' if value['destination']=='inbox' else 'next'}
    if value['destination']=='today':
        day=timestamp(value['accepted_at']).astimezone(JST).date()
        fields['action_date']=day.isoformat()
        body+=f'初回の対応予定日: {day}（初回受付の日本時間。期限とは別です）\n'
        if day<now.astimezone(JST).date(): body+='遅延して取り込みました。対応予定日は初回受付日を保持しています。Focusで対応予定日超過を確認し、必要な日付変更は本人が行います。\n'
    if value.get('due'): fields['due']=value['due']
    if value.get('body'):
        # Keep source text literal: no source-provided HTML, Markdown links or marker injection.
        body+='\n元メッセージの抜粋（JSON文字列）:\n\n```json\n'+json.dumps(value['body'],ensure_ascii=False).replace('`',r'\u0060')+'\n```\n'
    return {'action':'create','kind':'tasks','id':'task-capture-'+key[:32],'fields':fields,'body':body}

class CaptureImporter:
    def __init__(self,root,folder,*,bind_origin=ALLOWED_MUTATION_ORIGIN):
        self.root=Path(root); self.folder=Path(folder); self.seen={}; self.lock=threading.Lock()
        self.bind_origin=bind_origin; self.stopped=threading.Event(); self.worker=None
        if not self.folder.is_absolute() or self.folder==Path('/') or '..' in self.folder.parts:
            raise CaptureError('folder_not_absolute')
        if self.folder.is_relative_to(self.root) or self.root.is_relative_to(self.folder): raise CaptureError('folder_overlaps_data')
        with directory(self.folder) as fd:
            st=os.fstat(fd); self.identity=(st.st_dev,st.st_ino)
        with directory(self.root) as fd:
            try: os.mkdir('.local-state',mode=0o700,dir_fd=fd)
            except FileExistsError: pass
        with directory(self.root/'.local-state'): pass
        self.state=None; self.report={'enabled':True,'checked_at':None,'imported':0,'duplicate':0,'deferred':0,'failed':0,'blocked':False,'issues':[],'tracked':0,'pending':0}
    def start(self,store):
        def work():
            while not self.stopped.is_set():
                self.poll(store,dt.datetime.now(dt.timezone.utc)); self.stopped.wait(10)
        self.worker=threading.Thread(target=work,name='local-message-import',daemon=True)
        self.worker.start()
    def close(self):
        self.stopped.set()
        if self.worker is not None and self.worker.ident is not None: self.worker.join()
    def status(self):
        with self.lock: return json.loads(json.dumps(self.report))
    def _request(self,store,path,payload):
        if self.state is None: self.state=api.ApiState(store)
        return api.handle(store,'POST','/api/v1/mutations/'+path,
                          headers={'Origin':self.bind_origin,'Content-Type':'application/json',WEB_MARKER_HEADER:'1'},
                          body=canonical(payload).encode(),state=self.state,
                          bind_origin=self.bind_origin,mutation_origins=(self.bind_origin,))
    def poll(self,store,now):
        with self.lock:
            report={'enabled':True,'checked_at':now.isoformat(),'imported':0,'duplicate':0,'deferred':0,'failed':0,'blocked':False,'issues':[],'tracked':0,'pending':0}
            def issue(name,code):
                report['failed']+=1
                if len(report['issues'])<50: report['issues'].append({'file':name if FILENAME.fullmatch(name) else '(unsafe filename)','code':code})
            try:
                if store.read_snapshot().recovery_required: raise CaptureError('mutation_recovery_required')
                with directory(self.root/'.local-state') as statefd:
                    try: os.mkdir('capture-receipts',mode=0o700,dir_fd=statefd)
                    except FileExistsError: pass
                    lockfd=os.open('capture-import.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=statefd)
                    try:
                        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        with directory(self.root/'.local-state/capture-receipts') as receipts, directory(self.folder) as incoming:
                            st=os.fstat(incoming)
                            if (st.st_dev,st.st_ino)!=self.identity: raise CaptureError('folder_changed')
                            records=receipt_records(receipts); report['tracked']=sum(r['phase']=='committed' for r in records.values())
                            report['pending']=sum(r['phase']=='pending' for r in records.values())
                            names=sorted(n for n in os.listdir(incoming) if n.endswith('.json'))
                            if len(names)>1000: raise CaptureError('handoff_capacity')
                            live=set(names); self.seen={n:v for n,v in self.seen.items() if n in live}
                            for name in names:
                                try:
                                    if not FILENAME.fullmatch(name): raise CaptureError('invalid_filename')
                                    raw,signature=read_file(incoming,name)
                                    stamp=(signature,hashlib.sha256(raw).hexdigest())
                                    previous=self.seen.get(name)
                                    if previous is None or previous[0]!=stamp:
                                        self.seen[name]=(stamp,now); report['deferred']+=1; continue
                                    if (now-previous[1]).total_seconds()<2: report['deferred']+=1; continue
                                    value=validate(decode(raw),name)
                                    if timestamp(value['accepted_at'])>now+dt.timedelta(minutes=5): raise CaptureError('future_acceptance')
                                    key=source_key(value); digest=hashlib.sha256(canonical(value).encode()).hexdigest()
                                    receipt=records.get(key)
                                    if any(r['capture_id']==value['capture_id'] and k!=key for k,r in records.items()): raise CaptureError('capture_id_conflict')
                                    if receipt and (receipt['record_hash']!=digest or receipt['capture_id']!=value['capture_id']): raise CaptureError('source_conflict')
                                    if receipt and receipt['phase']=='committed': report['duplicate']+=1; continue
                                    task_id='task-capture-'+key[:32]
                                    snapshot=store.read_snapshot()
                                    if snapshot.recovery_required: raise CaptureError('mutation_recovery_required')
                                    existing=next((e for e in snapshot.entities if e.entity_id==task_id),None)
                                    if receipt and receipt['phase']=='pending':
                                        if existing is None or existing.content_hash!=receipt['planned_hash']: raise CaptureError('outcome_unknown')
                                        receipt['phase']='committed'; save_receipt(receipts,key,receipt)
                                        report['tracked']+=1; report['duplicate']+=1; continue
                                    if existing is not None: raise CaptureError('task_id_conflict')
                                    status,preview=self._request(store,'preview',operation(value,key,now))
                                    if status!=200: raise CaptureError('preview_'+preview.get('error',{}).get('code','failed'))
                                    if read_file(incoming,name)[0]!=raw: raise CaptureError('file_changed')
                                    planned=preview['proposed']
                                    receipt={'version':1,'source_key':key,'capture_id':value['capture_id'],'record_hash':digest,
                                             'task_id':task_id,'phase':'pending','planned_hash':planned['content_hash'],'accepted_at':value['accepted_at']}
                                    save_receipt(receipts,key,receipt); records[key]=receipt
                                    preview_hash=preview.pop('preview_hash')
                                    status,result=self._request(store,'apply',{'preview':preview,'preview_hash':preview_hash})
                                    if status not in (200,201):
                                        code=result.get('error',{}).get('code','failed')
                                        if code in {'busy','stale_preview','conflict','validation_failed','invalid_request'}:
                                            receipt['phase']='retry'; save_receipt(receipts,key,receipt)
                                        raise CaptureError('apply_'+code)
                                    actual=store.get_entity(task_id)
                                    if actual.content_hash!=receipt['planned_hash']: raise CaptureError('readback_mismatch')
                                    receipt['phase']='committed'; save_receipt(receipts,key,receipt)
                                    report['imported']+=1; report['tracked']+=1
                                except CaptureError as error: issue(name,str(error))
                                except Exception: issue(name,'read_or_apply_failed')
                            report['pending']=sum(r['phase']=='pending' for r in records.values())
                    finally: os.close(lockfd)
            except Exception as error:
                report['blocked']=True; issue('',str(error) if isinstance(error,CaptureError) else 'folder_or_receipt_unavailable')
            self.report=report; return json.loads(json.dumps(report))
