"""NX plugin bridge for the provider-neutral actual DTO/write contract.

Only explicitly completed core records enter the exporter. Source ownership is
established by the core repository, not by accepting an arbitrary NX user ID.
The NX identity always comes from /system/users/me.
"""
from dataclasses import asdict
import datetime as dt
import re
from pathlib import Path

from webapp.integrations.contracts import ActualRecord, ActualWriteReceipt
from webapp.timetracker_nx import NXError, ResultUnknown, digest, Settings, Journal, NXAdapter


class TimeTrackerNXPlugin:
    id = 'timetracker_nx'
    capabilities = frozenset({'actual.read', 'actual.write'})
    settings_schema = {
        'api_base': 'reviewed_https_api_base',
        'timezone': 'company_iana_timezone',
        'granularity': [5, 6, 10, 15],
        'required_categories': ['timeEntryCategoryId', 'processCategoryId'],
    }

    def __init__(self, adapter):
        self.adapter = adapter  # Dedicated HTTPS class or synthetic double; no startup I/O.

    def _rows(self, records, operation_key=None):
        if any(not isinstance(record, ActualRecord) for record in records):
            raise NXError('invalid_data')
        with self.adapter.journal.exclusive():
            user = self.adapter._self(self.adapter.journal.read())
        return [{
            'id': record.reference_id,
            'task_id': record.reference_id,
            'owner_id': user,
            'confirmed': record.completed and not record.provisional,
            'start': record.start,
            'end': record.end,
            'revision': record.revision,
            'operation_key': digest(operation_key) if operation_key is not None else None,
        } for record in records]

    def register_task(self, reference_id, task_url, work_item_id, categories=None):
        return self.adapter.register_task(reference_id, task_url, work_item_id, categories)

    def preview_actuals(self, records):
        rows = self._rows(records)
        preview = self.adapter.preview(rows)
        for row, record in zip(preview['rows'], records):
            row['actual'] = asdict(record)
        return preview

    def apply_actuals(self, records, token):
        return self.adapter.apply(self._rows(records), token)

    def read_actuals(self, first, last):
        """Explicit本人 remote read; remote actuals must not join a local export selection."""
        try:
            start, finish = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
            if finish < start or (finish-start).days > 92:
                raise ValueError()
        except (ValueError, TypeError):
            raise NXError('invalid_data') from None
        with self.adapter.journal.exclusive():
            user = self.adapter._self(self.adapter.journal.read())
            days = {(start+dt.timedelta(days=i)).isoformat() for i in range((finish-start).days+1)}
            rows = self.adapter._days(user, days)
        result = []
        for remote_id, row in rows.items():
            source_id = 'timetracker_nx:'+remote_id
            result.append(ActualRecord.create(reference_id='external:'+source_id, source_kind='external_event',
                source_id=source_id, source_provider='timetracker_nx', title='NX実績 '+row['workItemId'],
                start=dt.datetime.fromisoformat(row['startTime']).replace(tzinfo=self.adapter.settings.timezone).isoformat(),
                end=dt.datetime.fromisoformat(row['finishTime']).replace(tzinfo=self.adapter.settings.timezone).isoformat(),
                origin='recorded', completed=True, provisional=False))
        return result

    def reconcile(self, reference_id):
        return self.adapter.reconcile(reference_id)

    def reconcile_receipt(self, prior):
        reference = prior['reference_id']
        with self.adapter.journal.exclusive():
            entry = self.adapter.journal.read()['entries'].get(reference)
        if not entry or (entry['status']=='synced' and (entry.get('local_revision') != prior['local_revision'] or entry.get('operation_key') != digest(prior['idempotency_key']))):
            # Durable core intent precedes provider intent. No provider intent means no send.
            return ActualWriteReceipt(state='failed', reference_id=reference,
                local_revision=prior['local_revision'], idempotency_key=prior['idempotency_key'],
                remote_id=entry.get('remote_id') if entry else prior.get('remote_id'),
                remote_revision=entry.get('updatedAt') if entry else prior.get('remote_revision'), error_code='not_sent')
        if (entry.get('local_revision') != prior['local_revision'] or entry.get('operation_key') != digest(prior['idempotency_key'])):
            return ActualWriteReceipt(state='unknown', reference_id=reference,
                local_revision=prior['local_revision'], idempotency_key=prior['idempotency_key'], error_code='unknown')
        status = 'synced' if entry['status'] == 'synced' else self.reconcile(reference)
        with self.adapter.journal.exclusive():
            entry = self.adapter.journal.read()['entries'][reference]
        return ActualWriteReceipt(state='applied' if status == 'synced' else 'unknown',
            reference_id=reference, local_revision=prior['local_revision'], idempotency_key=prior['idempotency_key'],
            remote_id=entry.get('remote_id'), remote_revision=entry.get('updatedAt') if status == 'synced' else None,
            error_code=None if status == 'synced' else status)

    def write_actual(self, request):
        try:
            with self.adapter.journal.exclusive():
                state = self.adapter.journal.read()
                key = digest(request.idempotency_key)
                binding = digest([request.actual.reference_id, request.actual.revision, request.operation,
                                  request.remote_id, request.expected_remote_revision])
                keys = state.setdefault('write_keys', {})
                if key in keys and keys[key] != binding:
                    raise NXError('conflict')
                keys[key] = binding
                self.adapter.journal.write(state)
            return self._write_actual(request)
        except (NXError, ResultUnknown) as error:
            code = error.code if isinstance(error, NXError) else 'unknown'
            state = 'unknown' if code == 'unknown' else ('conflict' if code in ('conflict','stale_preview') else 'failed')
            return ActualWriteReceipt(state=state, reference_id=request.actual.reference_id,
                local_revision=request.actual.revision, idempotency_key=request.idempotency_key, error_code=machine_code(code))

    def _write_actual(self, request):
        """Protocol single-record entrypoint, preserving the same no-retry journal."""
        actual = request.actual
        rows = self._rows([actual], request.idempotency_key)
        preview = self.adapter.preview(rows)
        row = preview['rows'][0]
        with self.adapter.journal.exclusive():
            entry = self.adapter.journal.read()['entries'].get(actual.reference_id)
        state, code = None, None
        if row['action'] == 'blocked':
            code = row['code']
            state = 'unknown' if code == 'unknown' else ('conflict' if code in ('conflict','task_changed') else 'failed')
        elif request.operation == 'update' and (
                not entry or request.remote_id != entry['remote_id']
                or request.expected_remote_revision != entry['updatedAt']):
            state, code = 'conflict', 'conflict'
        elif request.operation == 'create' and row['action'] == 'update':
            state, code = 'conflict', 'conflict'
        if state is None:
            try:
                result = self.adapter.apply(rows, preview['token'])[0]
                code = result.get('code')
                state = {'synced':'applied', 'unchanged':'unchanged', 'unknown':'unknown',
                         'unresolved':'unknown', 'ambiguous':'unknown', 'rejected':'failed', 'blocked':'conflict'}[result['status']]
            except NXError as error:
                state, code = 'conflict', error.code
            with self.adapter.journal.exclusive():
                entry = self.adapter.journal.read()['entries'].get(actual.reference_id)
        return ActualWriteReceipt(state=state, reference_id=actual.reference_id, local_revision=actual.revision,
                                  idempotency_key=request.idempotency_key,
                                  remote_id=entry.get('remote_id') if entry else None,
                                  remote_revision=entry.get('updatedAt') if entry and entry['status']=='synced' else None,
                                  error_code=machine_code(code))


def describe(context):
    """Lazy registry status: never resolves identity, authenticates or contacts NX."""
    transport = context.services.get('timetracker_nx_transport')
    from webapp.timetracker_nx_transport import NXHTTPTransport
    available = transport is None or isinstance(transport,NXHTTPTransport) or getattr(transport, 'synthetic_only', False) is True
    configured = False
    if transport is not None and available:
        try:
            Settings(**dict(context.settings))
            configured = transport.configured() if isinstance(transport,NXHTTPTransport) else True
        except (NXError, TypeError):
            pass
    return {'available': available, 'configured': configured,
            'connection_state': 'connected' if configured else 'unconnected'}


def build_plugin(context):
    settings = Settings(**dict(context.settings))
    journal = Journal(Path(context.root)/'.local-state/integrations/timetracker-nx.json')
    return TimeTrackerNXPlugin(NXAdapter(settings, journal, context.services.get('timetracker_nx_transport')))


def machine_code(code):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', code).lower() if code else None
