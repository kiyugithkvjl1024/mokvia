"""Explicit NX actual export. Transport owns HTTP; no login or scheduler.

The caller supplies normalized confirmed actual DTOs, never Outlook state. Persistent
intent is written before a mutation; unknown results can only be reconciled.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


MESSAGES = {
    'configuration_invalid': '会社で承認したNXの非秘密設定を確認してください。',
    'credential_missing': '承認済み資格情報が注入されていません。未接続です。',
    'credential_invalid': '資格情報の注入形式を確認してください。',
    'identity_required': '本人IDの確認が必要です。',
    'authentication_required': '資格情報の期限・権限を会社で確認してください。',
    'redirect_blocked': 'NXが別URLへ転送しました。設定したHTTPS APIを確認してください。',
    'rate_limited': 'NXの通信制限です。自動再試行せず時間をおいて確認してください。',
    'server_unavailable': 'NXサーバーを確認してください。自動再試行しません。',
    'request_rejected': 'NXが要求を拒否しました。社内設定・権限を確認してください。',
    'transport_failed': 'TLS・証明書・ネットワークを確認してください。',
    'not_confirmed': '確定した実績のみ反映できます。',
    'not_self': '本人の実績のみ反映できます。',
    'unmapped_task': 'タスクURLと確認済みワークアイテムIDを登録してください。',
    'category_clear_unsupported': '登録済み分類の解除は未検証です。NX側で確認してください。',
    'task_changed': '登録済み実績のタスク変更は未対応です。NX側で確認してください。',
    'cross_day': '日跨ぎ実績は送信できません。コア側で日別に確定してください。',
    'granularity': '社内の入力粒度と一致しません。自動丸めは行いません。',
    'category_required': '社内で必須の作業分類・工程分類を指定してください。',
    'overlap': '本人の既存実績または選択実績と時間が重複しています。',
    'conflict': 'NX側が変更・削除されています。差分を確認してください。',
    'unknown': '通信結果不明です。再送せず照合してください。',
    'unresolved': '一致が確認できません。NX側を確認し、照合を続けてください。',
    'ambiguous': '一致候補が複数あります。自動で対応付けできません。',
    'stale_preview': '実績・設定・NX状態が変わりました。再プレビューしてください。',
    'mock_only': 'この版はモック検証専用です。実通信は有効化できません。',
    'InputTimeEntryAlwaysLocked': '本人の実績入力がロックされています。',
    'InputTimeEntryLocked': '対象日は実績入力がロックされています。',
    'ProjectLocked': 'プロジェクトがロックされています。',
    'WorkItemLocked': 'タスクの実績入力がロックされています。',
    'WorkItemNotAssigned': '本人がタスクに割り当てられていません。',
    'InvalidAssignment': '本人がプロジェクトに割り当てられていません。',
    'NotMatchActualTimeUnit': '社内の入力粒度と一致しません。',
    'TimeEntryOverlapped': 'NXの実績と時間が重複しています。',
    'FieldCannotBeEmpty': '必須項目・分類が不足しています。',
    'OperationDenied': '終了プロジェクト・入力不可タスク・社内制約を確認してください。',
}


class NXError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'invalid_data'
        super().__init__(MESSAGES.get(self.code, 'NXデータまたは設定を確認してください。'))


class ResultUnknown(Exception):
    """Transport cannot establish whether NX committed the request."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,256}', value):
        raise NXError('invalid_data')
    return value


def instant(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (ValueError, AttributeError, TypeError):
        raise NXError('invalid_data') from None


def safe_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.query or parsed.port not in (None, 443)
                or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value):
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise NXError('invalid_data') from None


class Settings:
    """No credentials. API origin is a reviewed host boundary, never a task URL."""
    def __init__(self, api_base, timezone, granularity, required_categories=()):
        parsed = safe_url(api_base)
        if '..' in parsed.path or '%' in parsed.path or not parsed.path.endswith('/api'):
            raise NXError('invalid_data')
        if granularity not in (5, 6, 10, 15) or isinstance(granularity, bool):
            raise NXError('granularity')
        try:
            self.timezone = dt.timezone(dt.timedelta(hours=9), 'Asia/Tokyo') if timezone == 'Asia/Tokyo' else ZoneInfo(timezone)
        except (ValueError, KeyError):
            raise NXError('invalid_data') from None
        if set(required_categories) - {'timeEntryCategoryId', 'processCategoryId'}:
            raise NXError('invalid_data')
        self.api_base = api_base.rstrip('/')
        self.host = parsed.hostname.lower()
        self.granularity = granularity
        self.required_categories = tuple(sorted(required_categories))

    def signature(self):
        return [self.api_base, str(self.timezone), self.granularity, self.required_categories]

    def task_url(self, url):
        parsed = safe_url(url)
        if parsed.hostname.lower() != self.host:
            raise NXError('invalid_data')
        return url  # No fetch and no guessed URL-to-ID extraction.


class Journal:
    """Private local mapping/intent journal, portable on POSIX and Windows.

    Hold exclusive() for the complete read-preview-mutate-readback transaction.
    Lock files and journal belong to .local-state; callers must not back them up
    without mappings and pending intents together.
    """
    _guard = threading.RLock()

    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def exclusive(self):
        with self._guard:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = self.path.with_suffix('.lock')
            if self.path.parent.is_symlink() or self.path.parent.parent.is_symlink() or lock.is_symlink() or self.path.is_symlink():
                raise NXError('invalid_data')
            with open(lock, 'a+b') as handle:
                if os.name == 'nt':
                    import msvcrt
                    if handle.tell() == 0:
                        handle.write(b'0'); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == 'nt':
                        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return {'version': 1, 'scope': None, 'tasks': {}, 'entries': {}}
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 8*1024*1024:
                raise ValueError()
            from webapp.integrations.external_events import read_json
            value = read_json(self.path)
            if (not isinstance(value, dict) or set(value)-{'version','scope','tasks','entries','write_keys'}
                    or value.get('version') != 1 or not isinstance(value.get('tasks'), dict)
                    or not isinstance(value.get('entries'), dict)):
                raise ValueError()
            scope = value['scope']
            if scope is not None:
                if not isinstance(scope,list) or len(scope)!=2:
                    raise ValueError()
                safe_url(scope[0]);identifier(scope[1])
            for reference, task in value['tasks'].items():
                identifier(reference)
                if not isinstance(task,dict) or set(task)-{'url','workItemId','timeEntryCategoryId','processCategoryId'}:
                    raise ValueError()
                safe_url(task['url']);identifier(task['workItemId'])
                for key in ('timeEntryCategoryId','processCategoryId'):
                    if key in task:identifier(task[key])
            def check_entry(entry, depth=0):
                if depth>1 or not isinstance(entry,dict) or entry.get('status') not in {'synced','unknown'}:
                    raise ValueError()
                allowed={'status','payload','remote_id','local_revision','operation_key'}|({'updatedAt'} if entry['status']=='synced' else {'previous'})
                if set(entry)-allowed or not isinstance(entry.get('payload'),dict) or set(entry['payload'])!=set(FIELDS):
                    raise ValueError()
                if any(not isinstance(v,str) or len(v)>2048 for v in entry['payload'].values()):
                    raise ValueError()
                identifier(entry['payload']['workItemId'])
                for key in ('startTime','finishTime'):
                    parsed=dt.datetime.fromisoformat(entry['payload'][key])
                    if parsed.tzinfo is not None:raise ValueError()
                if entry.get('remote_id') is not None:identifier(entry['remote_id'])
                for key in ('local_revision','operation_key'):
                    if entry.get(key) is not None and not re.fullmatch(r'[0-9a-f]{64}',entry[key]):raise ValueError()
                if entry['status']=='synced' and (not entry.get('remote_id') or not isinstance(entry.get('updatedAt'),str) or not entry['updatedAt']):raise ValueError()
                if entry.get('previous') is not None:check_entry(entry['previous'],depth+1)
            for reference, entry in value['entries'].items():
                identifier(reference);check_entry(entry)
            if not isinstance(value.get('write_keys',{}),dict) or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',k) or not re.fullmatch(r'[0-9a-f]{64}',v) for k,v in value.get('write_keys',{}).items()):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise NXError('invalid_data') from None

    def write(self, value):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.nx-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                file.write(canonical(value)); file.flush(); os.fsync(file.fileno())
            os.replace(name, self.path)
            if os.name != 'nt':
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


FIELDS = ('workItemId', 'startTime', 'finishTime', 'memo', 'timeEntryCategoryId', 'processCategoryId')


def values(row):
    result = {key: row.get(key) or '' for key in FIELDS}
    for key in ('startTime', 'finishTime'):
        if result[key]:
            try:
                result[key] = dt.datetime.fromisoformat(result[key]).isoformat()
            except (ValueError, TypeError):
                raise NXError('invalid_data') from None
    return result


def overlaps(a, b):
    return a['startTime'] < b['finishTime'] and b['startTime'] < a['finishTime']


class NXAdapter:
    """Transport request(method, relative_path, query=None, body=None).

    Accept only the reviewed dedicated NX HTTPS class or explicitly synthetic doubles.
    Credentials and automatic retries never enter the adapter or journal.
    """
    def __init__(self, settings, journal, transport):
        from webapp.timetracker_nx_transport import NXHTTPTransport
        if getattr(transport, 'synthetic_only', False) is not True:
            if not isinstance(transport,NXHTTPTransport) or transport.profile.settings.signature()!=settings.signature():
                raise NXError('mock_only')
        self.settings, self.journal, self.transport = settings, journal, transport

    def _request(self, method, path, **kwargs):
        try:
            return self.transport.request(method, path, **kwargs)
        except (NXError, ResultUnknown):
            raise
        except Exception:
            # Never surface exception text/remote body, which can contain secrets.
            raise ResultUnknown() from None

    def _self(self, state):
        me = self._request('GET', '/system/users/me')
        user = identifier(me['id'])
        if ':' in user:
            raise NXError('invalid_data')
        scope = [self.settings.api_base, user]
        if state['scope'] is not None and state['scope'] != scope:
            raise NXError('not_self')
        return user

    def register_task(self, task_id, url, work_item_id, categories=None):
        identifier(task_id); identifier(work_item_id)
        if ':' in work_item_id:
            raise NXError('invalid_data')
        self.settings.task_url(url)
        categories = categories or {}
        if set(categories) - {'timeEntryCategoryId', 'processCategoryId'}:
            raise NXError('invalid_data')
        for value in categories.values():
            identifier(value)
        with self.journal.exclusive():
            state = self.journal.read()
            state['tasks'][task_id] = {'url': url, 'workItemId': work_item_id, **categories}
            self.journal.write(state)

    def _payload(self, actual, user, tasks):
        identifier(actual['id'])
        if actual.get('confirmed') is not True:
            raise NXError('not_confirmed')
        if actual.get('owner_id') != user:
            raise NXError('not_self')
        task = tasks.get(actual.get('task_id'))
        if not task:
            raise NXError('unmapped_task')
        start = instant(actual['start']).astimezone(self.settings.timezone)
        finish = instant(actual['end']).astimezone(self.settings.timezone)
        if finish <= start:
            raise NXError('invalid_data')
        if start.date() != finish.date():
            raise NXError('cross_day')
        if any(t.second or t.microsecond or t.minute % self.settings.granularity for t in (start, finish)):
            raise NXError('granularity')
        if any(not task.get(key) for key in self.settings.required_categories):
            raise NXError('category_required')
        memo = actual.get('memo', '')
        if not isinstance(memo, str) or len(memo) > 2048:
            raise NXError('invalid_data')
        result = {'workItemId': task['workItemId'], 'startTime': start.replace(tzinfo=None).isoformat(timespec='seconds'),
                  'finishTime': finish.replace(tzinfo=None).isoformat(timespec='seconds'), 'memo': memo}
        result.update({key: task[key] for key in ('timeEntryCategoryId', 'processCategoryId') if task.get(key)})
        return values(result)

    def _days(self, user, days):
        result = {}
        for day in sorted(days):
            offset, total = 0, None
            while total is None or offset < total:
                page = self._request('GET', f'/system/users/{user}/timeEntries', query={
                    'startDate': day, 'finishDate': day, 'limit': 100, 'offset': offset, 'orderby': 'id asc'})
                rows, count = page.get('data'), page.get('totalCount')
                if (not isinstance(rows, list) or not isinstance(count, int) or isinstance(count, bool)
                        or count < 0 or count > 10000 or (total is not None and total != count)
                        or offset + len(rows) > count or (not rows and offset < count)):
                    raise NXError('invalid_data')
                total = count
                for row in rows:
                    rid = identifier(row.get('id'))
                    if ':' in rid or row.get('userId') != user or rid in result or not row.get('updatedAt'):
                        raise NXError('invalid_data')
                    if row.get('isDeleted') or row.get('startTime', '')[:10] != day:
                        raise NXError('invalid_data')
                    try:
                        start = dt.datetime.fromisoformat(row['startTime'])
                        finish = dt.datetime.fromisoformat(row['finishTime'])
                        if start.tzinfo or finish.tzinfo or start >= finish or start.date() != finish.date():
                            raise ValueError()
                    except (KeyError, ValueError, TypeError):
                        raise NXError('invalid_data') from None
                    result[rid] = copy.deepcopy(row)
                offset += len(rows)
        return result

    def _preview(self, actuals, state):
        user = self._self(state)
        rows, payloads, days = [], {}, set()
        seen = set()
        for actual in actuals:
            local_id = identifier(actual['id'])
            if local_id in seen:
                raise NXError('invalid_data')
            seen.add(local_id)
            try:
                payload = self._payload(actual, user, state['tasks'])
                payloads[local_id] = payload
                days.add(payload['startTime'][:10])
                old = state['entries'].get(local_id)
                if old:
                    days.add(old['payload']['startTime'][:10])
                rows.append({'id': local_id, 'action': 'pending', 'payload': payload, 'local_revision': actual.get('revision'), 'operation_key': actual.get('operation_key')})
            except NXError as error:
                rows.append({'id': local_id, 'action': 'blocked', 'code': error.code})
        remote = self._days(user, days)
        for row in rows:
            if row['action'] == 'blocked':
                continue
            local_id, payload = row['id'], row['payload']
            old = state['entries'].get(local_id)
            if old and old['status'] == 'unknown':
                row.update(action='blocked', code='unknown'); continue
            target = remote.get(old['remote_id']) if old else None
            if old and (not target or values(target) != old['payload'] or target['updatedAt'] != old['updatedAt']):
                row.update(action='blocked', code='conflict', previous=old['payload'], remote=values(target) if target else None); continue
            if old and old['payload']['workItemId'] != payload['workItemId']:
                row.update(action='blocked', code='task_changed'); continue
            if old and any(old['payload'].get(k) and not payload.get(k) for k in ('timeEntryCategoryId','processCategoryId')):
                row.update(action='blocked', code='category_clear_unsupported'); continue
            if target and target.get('isLocked'):
                row.update(action='blocked', code='WorkItemLocked'); continue
            other = [r for rid, r in remote.items() if not old or rid != old['remote_id']]
            other += [p for lid, p in payloads.items() if lid != local_id]
            if any(overlaps(payload, r) for r in other):
                row.update(action='blocked', code='overlap'); continue
            row['action'] = 'unchanged' if old and old['payload'] == payload else ('update' if old else 'create')
            if old:
                row['remote_id'] = old['remote_id']
        token = digest([actuals, state, self.settings.signature(), remote])
        return {'token': token, 'user_id': user, 'rows': rows}

    def preview(self, actuals):
        with self.journal.exclusive():
            return self._preview(actuals, self.journal.read())

    def apply(self, actuals, token):
        """Revalidate the complete selection; no mutations if any row is blocked."""
        with self.journal.exclusive():
            state = self.journal.read()
            preview = self._preview(actuals, state)
            if preview['token'] != token:
                raise NXError('stale_preview')
            if any(row['action'] == 'blocked' for row in preview['rows']):
                raise NXError(next(row['code'] for row in preview['rows'] if row['action'] == 'blocked'))
            user = preview['user_id']
            state['scope'] = [self.settings.api_base, user]
            results = []
            for row in preview['rows']:
                if row['action'] == 'unchanged':
                    results.append({'id': row['id'], 'status': 'unchanged'}); continue
                # Fresh per-row check narrows the no-ETag race window.
                fresh = self._preview(actuals, state)
                check = next(r for r in fresh['rows'] if r['id'] == row['id'])
                if check['action'] == 'blocked':
                    results.append({'id': row['id'], 'status': 'blocked', 'code': check['code']}); break
                previous = copy.deepcopy(state['entries'].get(row['id']))
                intent = {'status': 'unknown', 'payload': row['payload'], 'remote_id': row.get('remote_id'),
                          'previous': previous, 'local_revision': row.get('local_revision'), 'operation_key': row.get('operation_key')}
                state['entries'][row['id']] = intent
                self.journal.write(state)  # A crash after durable intent must never trigger retry.
                path = f'/system/users/{user}/timeEntries'
                body = {k: v for k, v in row['payload'].items()
                        if (k != 'workItemId' or row['action'] == 'create')
                        and (k not in ('timeEntryCategoryId', 'processCategoryId') or v)}
                try:
                    reply = self._request('POST' if row['action'] == 'create' else 'PUT',
                                          path if row['action'] == 'create' else path+'/'+row['remote_id'], body=body)
                    if row['action'] == 'create':
                        try:
                            remote_id = identifier(reply['id'])
                            if ':' in remote_id:
                                raise ValueError()
                        except (NXError, KeyError, TypeError, ValueError):
                            raise ResultUnknown() from None
                        intent['remote_id'] = remote_id
                        self.journal.write(state)
                except NXError as error:
                    # A confirmed rejection is safe; raw response text is discarded.
                    if previous:
                        state['entries'][row['id']] = previous
                    else:
                        del state['entries'][row['id']]
                    self.journal.write(state)
                    results.append({'id': row['id'], 'status': 'rejected', 'code': error.code}); break
                except (ResultUnknown, KeyError, TypeError):
                    status = 'unknown'
                else:
                    try:
                        status = self._reconcile_one(state, row['id'], user)
                    except (NXError, ResultUnknown):
                        status = 'unknown'
                results.append({'id': row['id'], 'status': status})
                if status != 'synced':
                    break
            return results

    def _reconcile_one(self, state, local_id, user):
        intent = state['entries'][local_id]
        remote = self._days(user, {intent['payload']['startTime'][:10]})
        if intent.get('remote_id'):
            candidate = remote.get(intent['remote_id'])
            matches = [candidate] if candidate and values(candidate) == intent['payload'] else []
        else:
            bound = {entry.get('remote_id') for lid, entry in state['entries'].items() if lid != local_id}
            matches = [row for rid, row in remote.items() if rid not in bound and values(row) == intent['payload']]
        if len(matches) != 1:
            return 'ambiguous' if len(matches) > 1 else 'unresolved'
        row = matches[0]
        state['entries'][local_id] = {'status': 'synced', 'payload': intent['payload'],
                                     'remote_id': row['id'], 'updatedAt': row['updatedAt'], 'local_revision': intent.get('local_revision'), 'operation_key': intent.get('operation_key')}
        self.journal.write(state)
        return 'synced'

    def reconcile(self, local_id):
        identifier(local_id)
        with self.journal.exclusive():
            state = self.journal.read()
            user = self._self(state)
            if local_id not in state['entries'] or state['entries'][local_id]['status'] != 'unknown':
                raise NXError('invalid_data')
            try:
                return self._reconcile_one(state, local_id, user)
            except ResultUnknown:
                return 'unknown'
