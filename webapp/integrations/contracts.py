"""Provider-neutral, credential-free integration contracts (version 1)."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import datetime as dt
import hashlib
import json
import re
from typing import Literal, Protocol

CAPABILITIES = frozenset({'calendar.read', 'calendar.write', 'actual.read', 'actual.write'})


def utc(value: str) -> str:
    parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('timezone_required')
    return parsed.astimezone(dt.timezone.utc).isoformat()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ActualRecord:
    reference_id: str
    source_kind: Literal['task','external_event']
    source_id: str
    source_provider: str
    title: str
    start: str
    end: str | None
    origin: Literal['planned','manual','measured','recorded']
    completed: bool
    provisional: bool
    revision: str

    def __post_init__(self):
        prefix='task:' if self.source_kind=='task' else 'external:'
        if self.source_kind not in ('task','external_event') or self.reference_id!=prefix+self.source_id or not self.source_id:raise ValueError('invalid_reference')
        if self.origin not in ('planned','manual','measured','recorded'):raise ValueError('invalid_origin')
        if type(self.completed) is not bool or type(self.provisional) is not bool:raise ValueError('invalid_flags')
        first=utc(self.start);last=utc(self.end) if self.end is not None else None
        if last is not None and last<first:raise ValueError('invalid_interval')
        if self.provisional!=(last is None):raise ValueError('invalid_provisional')
        if not self.source_provider or not self.title:raise ValueError('invalid_actual')
        object.__setattr__(self,'start',first);object.__setattr__(self,'end',last)
        if self.revision!=self.content_revision():raise ValueError('invalid_revision')

    def content_revision(self):
        value=asdict(self);value.pop('revision');return fingerprint(value)

    def to_dict(self):return asdict(self)

    @classmethod
    def create(cls, **fields):
        fields['start']=utc(fields['start']);fields['end']=utc(fields['end']) if fields.get('end') is not None else None
        return cls(**fields,revision=fingerprint(fields))


@dataclass(frozen=True)
class ActualWriteRequest:
    actual: ActualRecord
    operation: Literal['create','update']
    idempotency_key: str
    remote_id: str | None = None
    expected_remote_revision: str | None = None

    def __post_init__(self):
        if self.operation not in ('create','update') or not self.idempotency_key:raise ValueError('invalid_write')
        if self.actual.provisional or self.actual.end is None or self.actual.start==self.actual.end:raise ValueError('actual_not_closed')
        if self.operation=='create' and (self.remote_id is not None or self.expected_remote_revision is not None):raise ValueError('invalid_create')
        if self.operation=='update' and (not self.remote_id or not self.expected_remote_revision):raise ValueError('remote_readback_required')


@dataclass(frozen=True)
class ActualWriteReceipt:
    state: Literal['applied','unchanged','unknown','conflict','failed']
    reference_id: str
    local_revision: str
    idempotency_key: str
    remote_id: str | None = None
    remote_revision: str | None = None
    error_code: str | None = None

    def __post_init__(self):
        if self.state not in ('applied','unchanged','unknown','conflict','failed'):raise ValueError('invalid_receipt')
        if self.state in ('applied','unchanged') and (not self.remote_id or not self.remote_revision):raise ValueError('readback_required')
        for value in (self.reference_id,self.local_revision,self.idempotency_key,self.remote_id,self.remote_revision):
            if value is not None and (not isinstance(value,str) or not value or len(value)>4096 or any(ord(c)<32 for c in value)):raise ValueError('invalid_receipt')
        if self.error_code is not None and (not isinstance(self.error_code,str) or re.fullmatch(r'[a-z0-9_]{1,80}',self.error_code) is None):raise ValueError('invalid_error_code')

    def to_dict(self):return asdict(self)


class ActualWriter(Protocol):
    def write_actual(self, request: ActualWriteRequest) -> ActualWriteReceipt:
        """Return applied only after exact remote readback. Unknown must not be retried."""
        ...
