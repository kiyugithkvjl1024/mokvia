from dataclasses import replace
import unittest

from tests import test_timetracker_nx as fixtures
from webapp.integration_plugins.timetracker_nx import TimeTrackerNXPlugin, describe, build_plugin
from types import SimpleNamespace
from webapp.integrations.contracts import ActualRecord, ActualWriteRequest


def record(end='2026-10-09T10:00:00+09:00', **changes):
    fields = dict(reference_id='task:synthetic-task', source_kind='task', source_id='synthetic-task',
                  source_provider='core', title='合成実績', start='2026-10-09T09:00:00+09:00',
                  end=end, origin='recorded', completed=True, provisional=end is None)
    fields.update(changes)
    return ActualRecord.create(**fields)


class PluginTests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated fixture setup, without inheriting its test methods.
        fixtures.NXTests.setUp(self)
        self.plugin = TimeTrackerNXPlugin(self.adapter)
        self.plugin.register_task('task:synthetic-task','https://example.invalid/task', '145')

    def test_core_dto_preview_explicit_confirmation_and_receipt(self):
        item = record()
        preview = self.plugin.preview_actuals([item])
        self.assertEqual(item.revision, preview['rows'][0]['actual']['revision'])
        self.assertFalse(self.mock.mutations())
        request = ActualWriteRequest(item, 'create', 'key-1')
        receipt = self.plugin.write_actual(request)
        self.assertEqual('applied', receipt.state)
        self.assertTrue(receipt.remote_revision)
        self.assertEqual('unchanged', self.plugin.write_actual(request).state)
        updated = record(end='2026-10-09T10:30:00+09:00')
        request = ActualWriteRequest(updated, 'update', 'key-2',receipt.remote_id,receipt.remote_revision)
        self.assertEqual('applied', self.plugin.write_actual(request).state)
        self.assertEqual('conflict', self.plugin.write_actual(request).state)

    def test_core_unconfirmed_and_provisional_not_sent(self):
        for item in [record(completed=False),record(end=None)]:
            self.assertEqual('not_confirmed', self.plugin.preview_actuals([item])['rows'][0]['code'])
        self.assertFalse(self.mock.mutations())

    def test_external_event_uses_normalized_core_only(self):
        item = record(reference_id='external:synthetic-occurrence', source_kind='external_event',
                      source_id='synthetic-occurrence', source_provider='outlook-classic',origin='manual')
        self.plugin.register_task(item.reference_id,'https://example.invalid/task', '145')
        p = self.plugin.preview_actuals([item])
        self.assertEqual('synced',self.plugin.apply_actuals([item],p['token'])[0]['status'])

    def test_unknown_protocol_receipt_not_retried(self):
        self.mock.fail = 'after'
        request = ActualWriteRequest(record(), 'create', 'key-1')
        self.assertEqual('unknown',self.plugin.write_actual(request).state)
        self.assertEqual('unknown',self.plugin.write_actual(request).state)
        self.assertEqual(1,len(self.mock.mutations()))
        self.assertEqual('synced',self.plugin.reconcile(record().reference_id))
        self.assertEqual('unchanged',self.plugin.write_actual(request).state)

    def test_registry_factory_is_lazy_and_remote_read_uses_core_dto(self):
        context=SimpleNamespace(root=self.path.parent,settings={'api_base':'https://example.invalid/api','timezone':'Asia/Tokyo','granularity':5},services={'timetracker_nx_transport':self.mock})
        self.assertTrue(describe(context)['configured'])
        plugin=build_plugin(context)
        self.assertFalse(self.mock.calls)
        self.plugin.write_actual(ActualWriteRequest(record(),'create','key-1'))
        records=plugin.read_actuals('2026-10-09','2026-10-09')
        self.assertEqual(1,len(records))
        self.assertEqual('timetracker_nx',records[0].source_provider)
        self.assertEqual(record().start,records[0].start)
        context.services={}
        self.assertEqual('unconnected',describe(context)['connection_state'])

    def test_idempotency_key_cannot_bind_different_revision(self):
        self.plugin.write_actual(ActualWriteRequest(record(),'create','key-1'))
        receipt=self.plugin.write_actual(ActualWriteRequest(record(end='2026-10-09T10:30:00+09:00'),'create','key-1'))
        self.assertEqual('conflict',receipt.state)
        self.assertEqual(1,len(self.mock.mutations()))

    def test_update_missing_mapping_is_conflict(self):
        request = ActualWriteRequest(record(),'update','key-1','999','old')
        self.assertEqual('conflict',self.plugin.write_actual(request).state)
        self.assertFalse(self.mock.mutations())


if __name__ == '__main__':unittest.main()
