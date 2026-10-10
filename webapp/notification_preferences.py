"""Non-secret notification policy; no transport, timers, or Entity mutations."""
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from webapp.integrations.external_events import read_json, write_json, _unique_object

TYPES = frozenset({'focus_reminder', 'focus_anomaly', 'break_finished'})
_KEYS = {'version', 'revision', 'quiet_enabled', 'quiet_start', 'quiet_end', 'timezone', 'types'}
_TIME = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z', re.ASCII)
_ZONE = re.compile(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z', re.ASCII)

class PreferenceError(ValueError):
    pass

def resolve_timezone(name):
    try: return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == 'Asia/Tokyo': return dt.timezone(dt.timedelta(hours=9), 'Asia/Tokyo')
        raise PreferenceError('invalid_timezone') from None
    except ValueError: raise PreferenceError('invalid_timezone') from None

def validate(value):
    if type(value) is not dict or set(value) != _KEYS:
        raise PreferenceError('invalid_settings')
    if type(value['version']) is not int or value['version'] != 1 or type(value['revision']) is not int or not 0 <= value['revision'] < 2**53:
        raise PreferenceError('invalid_settings')
    if type(value['quiet_enabled']) is not bool or any(type(value[k]) is not str or not _TIME.fullmatch(value[k]) for k in ('quiet_start', 'quiet_end')) or value['quiet_start'] == value['quiet_end']:
        raise PreferenceError('invalid_settings')
    zone = value['timezone']
    if type(zone) is not str or len(zone) > 128 or not _ZONE.fullmatch(zone):
        raise PreferenceError('invalid_timezone')
    if zone != 'local':
        resolve_timezone(zone)
    if type(value['types']) is not dict or set(value['types']) != TYPES or any(type(v) is not bool for v in value['types'].values()):
        raise PreferenceError('invalid_settings')
    return value

def decode(raw):
    if not isinstance(raw, bytes) or len(raw) > 4096: raise PreferenceError("invalid_settings")
    return validate(json.loads(raw, object_pairs_hook=_unique_object))

class Preferences:
    def __init__(self, root):
        self.directory = Path(root) / '.local-state' / 'notifications'
        self.path = self.directory / 'settings.json'
    def _safe(self):
        if any(p.is_symlink() for p in (self.directory, self.directory.parent, self.directory.parent.parent, self.path)):
            raise PreferenceError('unsafe_state')
    @contextmanager
    def lock(self):
        self._safe()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._safe()
        fd = os.open(self.directory/'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise PreferenceError('unsafe_state')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise PreferenceError('busy') from None
            yield
        finally: os.close(fd)
    def read(self):
        self._safe()
        if not self.path.exists():
            return {'version':1, 'revision':0, 'quiet_enabled':False, 'quiet_start':'22:00', 'quiet_end':'08:00', 'timezone':'local', 'types':{k:True for k in sorted(TYPES)}}
        try:
            if self.path.stat().st_size > 4096: raise PreferenceError('invalid_settings')
            return validate(read_json(self.path))
        except (OSError, ValueError) as error:
            if isinstance(error, PreferenceError): raise
            raise PreferenceError('invalid_settings') from None
    def update(self, value):
        validate(value)
        with self.lock():
            if value['revision'] != self.read()['revision']: raise PreferenceError('conflict')
            value = {**value, 'revision':value['revision']+1, 'types':dict(value['types'])}
            validate(value); write_json(self.path, value)
            return value
    def allows(self, kind, now):
        # Unknown kinds and corrupt policy fail closed, never interrupt completion.
        try:
            value = self.read()
            if kind not in TYPES or not value['types'][kind] or now.tzinfo is None or now.utcoffset() is None: return False
            if not value['quiet_enabled']: return True
            local = now.astimezone() if value['timezone'] == 'local' else now.astimezone(resolve_timezone(value['timezone']))
            minute = local.strftime('%H:%M'); start, end = value['quiet_start'], value['quiet_end']
            quiet = start <= minute < end if start < end else minute >= start or minute < end
            return not quiet
        except (OSError, ValueError, TypeError): return False
