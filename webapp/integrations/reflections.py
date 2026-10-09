"""Core actual catalog, explicit batch preview and durable no-retry receipts."""
from .contracts import ActualWriteRequest, ActualWriteReceipt, fingerprint
from .external_events import read_json, write_json
from .state import IntegrationError, validate_receipt


class ActualCatalog:
    def __init__(self, store, external_readers=()):
        self.store, self.external_readers = store, tuple(external_readers)

    def read(self):
        from .actuals import task_actuals
        records = task_actuals(self.store.read_snapshot())
        for reader in self.external_readers:
            records.extend(reader())
        if len({r.reference_id for r in records}) != len(records):
            raise IntegrationError('duplicate_reference')
        return {record.reference_id: record for record in records}


class ReflectionService:
    def __init__(self, registry, catalog):
        self.registry, self.catalog = registry, catalog

    def _path(self, plugin_id, reference):
        return self.registry.state.directory/('receipt-'+fingerprint([plugin_id, reference])+'.json')

    def receipt(self, plugin_id, reference):
        path = self._path(plugin_id, reference)
        if not path.exists() and not path.is_symlink():
            return None
        value = validate_receipt(read_json(path))
        if value['plugin_id'] != plugin_id or value['reference_id'] != reference:
            raise IntegrationError('invalid_receipt')
        return value

    def status(self, plugin_id, record):
        receipt = self.receipt(plugin_id, record.reference_id)
        if receipt and receipt['state'] in {'pending', 'unknown', 'conflict'}:
            state = 'unknown' if receipt['state'] == 'pending' else receipt['state']
        elif receipt and receipt['state'] in {'applied', 'unchanged'}:
            state = 'synced' if receipt['local_revision'] == record.revision else 'update'
        elif receipt:
            state = 'failed'
        else:
            state = 'new'
        plugin = self.registry.status(plugin_id)
        availability = ('disabled' if not plugin['enabled'] else 'unavailable' if not plugin['available']
                        else 'unconnected' if not plugin['configured'] else 'ready')
        return {'reference_id': record.reference_id, 'revision': record.revision, 'state': state,
                'availability': availability, 'provisional': record.provisional,
                'remote_id': receipt.get('remote_id') if receipt else None}

    def _selected(self, references):
        if (not isinstance(references, list) or not references or len(references) > 100
                or any(not isinstance(ref, str) for ref in references) or len(set(references)) != len(references)):
            raise IntegrationError('invalid_request')
        catalog = self.catalog.read()
        try:
            return [catalog[ref] for ref in references]
        except KeyError:
            raise IntegrationError('not_found') from None

    def _preview(self, plugin_id, references, writer):
        records = self._selected(references)
        if not hasattr(writer, 'preview_actuals'):
            raise IntegrationError('unsupported_capability')
        preview = writer.preview_actuals(records)
        for row, record in zip(preview['rows'], records):
            prior = self.receipt(plugin_id, record.reference_id)
            if prior and prior['state'] in {'pending', 'unknown'}:
                row.update(action='blocked', code='unknown')
            elif prior and prior['state'] == 'conflict':
                row.update(action='blocked', code='conflict')
        preview['provider_token'] = preview['token']
        preview['token'] = fingerprint([plugin_id, preview['provider_token'], self.registry.state.flags(),
                                        [(r.reference_id, r.revision) for r in records]])
        return preview, records

    def preview(self, plugin_id, references):
        with self.registry.state.lock():
            writer = self.registry.require(plugin_id, 'actual.write')
            preview, _ = self._preview(plugin_id, references, writer)
            preview.pop('provider_token')
            return preview

    def register_task(self, plugin_id, reference, url, work_item_id, categories):
        with self.registry.state.lock():
            self._selected([reference])  # Only references in this person's core catalog.
            writer = self.registry.require(plugin_id, 'actual.write')
            if not hasattr(writer, 'register_task'):
                raise IntegrationError('unsupported_capability')
            writer.register_task(reference, url, work_item_id, categories)
            return {'ok': True}

    def apply(self, plugin_id, references, token):
        with self.registry.state.lock():
            writer = self.registry.require(plugin_id, 'actual.write')
            preview, records = self._preview(plugin_id, references, writer)
            if not isinstance(token, str) or token != preview['token']:
                raise IntegrationError('stale_preview')
            if any(row['action'] == 'blocked' for row in preview['rows']):
                raise IntegrationError('reflection_requires_readback')
            results = []
            for index, record in enumerate(records):
                # Re-read core before each write. Provider also rechecks NX before mutation.
                current = self.catalog.read().get(record.reference_id)
                if current is None or current.revision != record.revision:
                    results.append({'id': record.reference_id, 'status': 'blocked', 'code': 'stale_preview'})
                    break
                value = self._write(plugin_id, record, writer, intent_revision=fingerprint(preview['rows'][index].get('payload')), force=preview['rows'][index]['action'] != 'unchanged')
                state = {'applied': 'synced', 'unchanged': 'unchanged', 'unknown': 'unknown',
                         'conflict': 'blocked', 'failed': 'rejected', 'pending': 'unknown'}[value['state']]
                if state == 'synced' and preview['rows'][index]['action'] == 'unchanged':
                    state = 'unchanged'
                results.append({'id': record.reference_id, 'status': state, 'code': value.get('error_code')})
                if state not in {'synced', 'unchanged'}:
                    break
            return results

    def write(self, plugin_id, reference_id, revision):
        with self.registry.state.lock():
            record = self._selected([reference_id])[0]
            if record.revision != revision:
                raise IntegrationError('conflict')
            return self._write(plugin_id, record, self.registry.require(plugin_id, 'actual.write'))

    def _write(self, plugin_id, record, writer, *, intent_revision=None, force=False):
        reference, revision = record.reference_id, record.revision
        if not record.completed or record.provisional or record.end is None or record.start == record.end:
            raise IntegrationError('actual_not_confirmed')
        prior = self.receipt(plugin_id, reference)
        if prior and prior['state'] in {'pending', 'unknown', 'conflict'}:
            raise IntegrationError('reflection_requires_readback')
        remote_id = prior.get('remote_id') if prior else None
        remote_revision = prior.get('remote_revision') if prior else None
        operation = 'update' if remote_id else 'create'
        key = fingerprint([plugin_id, reference, revision, operation, remote_id, remote_revision, intent_revision])
        # Existing applied revisions remain cheap, but batch preview has already inspected NX.
        if not force and prior and prior['state'] in {'applied', 'unchanged'} and prior['local_revision'] == revision:
            return prior
        request = ActualWriteRequest(record, operation, key, remote_id, remote_revision)
        value = {'plugin_id': plugin_id, 'state': 'pending', 'reference_id': reference,
                 'local_revision': revision, 'idempotency_key': key, 'remote_id': remote_id,
                 'remote_revision': remote_revision, 'error_code': None}
        path = self._path(plugin_id, reference)
        write_json(path, value)
        try:
            receipt = writer.write_actual(request)
            if (not isinstance(receipt, ActualWriteReceipt) or receipt.reference_id != reference
                    or receipt.local_revision != revision or receipt.idempotency_key != key
                    or (operation == 'update' and receipt.state in {'applied', 'unchanged'} and receipt.remote_id != remote_id)):
                raise ValueError('invalid_write_receipt')
            value = {**receipt.to_dict(), 'plugin_id': plugin_id}
            if receipt.state not in {'applied', 'unchanged'}:
                value['remote_id'] = remote_id or receipt.remote_id
                value['remote_revision'] = remote_revision
        except Exception:
            value['state'] = 'unknown'
            value['error_code'] = 'write_outcome_unknown'
        write_json(path, value)
        return value

    def reconcile(self, plugin_id, reference):
        """Exact provider readback resolves the original revision, even after local edits."""
        with self.registry.state.lock():
            writer = self.registry.require(plugin_id, 'actual.write')
            prior = self.receipt(plugin_id, reference)
            if not prior or prior['state'] not in {'pending', 'unknown'}:
                raise IntegrationError('reflection_requires_readback')
            if not hasattr(writer, 'reconcile_receipt'):
                raise IntegrationError('unsupported_capability')
            receipt = writer.reconcile_receipt(prior)
            if (not isinstance(receipt, ActualWriteReceipt) or receipt.reference_id != reference
                    or receipt.local_revision != prior['local_revision']
                    or receipt.idempotency_key != prior['idempotency_key']):
                raise IntegrationError('invalid_receipt')
            value = {**receipt.to_dict(), 'plugin_id': plugin_id}
            write_json(self._path(plugin_id, reference), value)
            return {'status': 'synced' if receipt.state == 'applied' else receipt.error_code or 'unknown',
                    'receipt': value}
