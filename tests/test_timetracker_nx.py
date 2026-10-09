import copy
import tempfile
import unittest
from pathlib import Path

from webapp.timetracker_nx import Journal, NXAdapter, NXError, ResultUnknown, Settings, values


def actual(uid='actual-1', start='09:00', end='10:00', **changes):
    row = {'id': uid, 'task_id': 'task-1', 'owner_id': '21', 'confirmed': True,
           'start': f'2026-10-09T{start}:00+09:00', 'end': f'2026-10-09T{end}:00+09:00'}
    row.update(changes)
    return row


class MockNX:
    synthetic_only = True

    def __init__(self):
        self.user = '21'
        self.rows = {}
        self.calls = []
        self.fail = None
        self.serial = 0
        self.page_size = 100
        self.reject = None
        self.readback_fail = False

    def add(self, payload, uid=None):
        self.serial += 1
        uid = uid or str(self.serial)
        self.rows[uid] = {**values(payload), 'id': uid, 'userId': self.user, 'updatedAt': str(self.serial)}
        return uid

    def request(self, method, path, query=None, body=None):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if path == '/system/users/me':
            return {'id': self.user}
        if method == 'GET':
            if self.readback_fail:
                raise NXError('invalid_data')
            data = [r for r in self.rows.values() if query['startDate'] <= r['startTime'][:10] <= query['finishDate']]
            offset = query['offset']
            return {'totalCount': len(data), 'data': copy.deepcopy(data[offset:offset+self.page_size])}
        if self.reject:
            raise NXError(self.reject)
        if self.fail == 'before':
            raise ResultUnknown()
        if method == 'POST':
            rid = self.add(body)
        elif method == 'PUT':
            rid = path.split('/')[-1]
            assert 'workItemId' not in body
            self.serial += 1
            self.rows[rid].update(body, updatedAt=str(self.serial))
        else:
            raise AssertionError('unexpected mutation')
        if self.fail == 'after':
            raise ResultUnknown()
        if self.fail == 'malformed':
            return {}
        if self.fail == 'readback':
            self.readback_fail = True
        return {'id': rid} if method == 'POST' else None

    def mutations(self):
        return [c for c in self.calls if c[0] != 'GET']


class NXTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'state.json'
        self.mock = MockNX()
        self.settings = Settings('https://example.invalid/api', 'Asia/Tokyo', 5)
        self.adapter = NXAdapter(self.settings, Journal(self.path), self.mock)
        self.adapter.register_task('task-1', 'https://example.invalid/unverified-task-path', '145')

    def preview(self, rows):
        return self.adapter.preview(rows)

    def apply(self, rows):
        preview = self.preview(rows)
        return self.adapter.apply(rows, preview['token'])

    def test_create_many_unchanged_and_update(self):
        rows = [actual(), actual('actual-2', '10:00', '11:00')]
        self.assertEqual(['create', 'create'], [r['action'] for r in self.preview(rows)['rows']])
        self.assertFalse(self.mock.mutations())
        self.assertEqual(['synced', 'synced'], [r['status'] for r in self.apply(rows)])
        before = len(self.mock.mutations())
        self.assertEqual(['unchanged', 'unchanged'], [r['status'] for r in self.apply(rows)])
        self.assertEqual(before, len(self.mock.mutations()))
        rows[1]['end'] = '2026-10-09T11:30:00+09:00'
        self.assertEqual(['unchanged', 'synced'], [r['status'] for r in self.apply(rows)])
        self.assertEqual('PUT', self.mock.mutations()[-1][0])
        entry = Journal(self.path).read()['entries']['actual-2']
        self.assertEqual('11:30:00', entry['payload']['finishTime'][11:])
        self.assertTrue(entry['updatedAt'])

    def test_journal_rejects_unexpected_secret_fields_and_bad_operation_key(self):
        import json
        self.apply([actual()]);valid=json.loads(self.path.read_text())
        modified=copy.deepcopy(valid);modified['api_key']='synthetic-secret'
        self.path.write_text(json.dumps(modified))
        with self.assertRaises(NXError) as caught:Journal(self.path).read()
        self.assertEqual('invalid_data',caught.exception.code)
        modified=copy.deepcopy(valid);modified['entries']['actual-1']['operation_key']='not-a-hash'
        self.path.write_text(json.dumps(modified))
        with self.assertRaises(NXError) as caught:Journal(self.path).read()
        self.assertEqual('invalid_data',caught.exception.code)

    def test_other_user_and_unconfirmed_block_entire_selection(self):
        for changes, code in [({'owner_id': '22'}, 'not_self'), ({'confirmed': False}, 'not_confirmed'),
                              ({'task_id': 'missing'}, 'unmapped_task')]:
            rows = [actual(), actual('bad', '11:00', '12:00', **changes)]
            self.assertEqual(code, self.preview(rows)['rows'][1]['code'])
            with self.assertRaises(NXError):
                self.apply(rows)
        self.assertFalse(self.mock.mutations())

    def test_validation_does_not_round(self):
        for row, code in [(actual(start='09:01'), 'granularity'),
                          (actual(end='09:00'), 'invalid_data'),
                          (dict(actual(start='23:00'), end='2026-10-10T00:00:00+09:00'), 'cross_day'),
                          (dict(actual(), start='2026-10-09T09:00:00'), 'invalid_data')]:
            self.assertEqual(code, self.preview([row])['rows'][0]['code'])
        self.settings.required_categories = ('processCategoryId',)
        self.assertEqual('category_required', self.preview([actual()])['rows'][0]['code'])
        self.adapter.register_task('task-1', 'https://example.invalid/task', '145', {'processCategoryId':'6'})
        self.assertEqual('create', self.preview([actual()])['rows'][0]['action'])

    def test_granularity_and_timezone(self):
        for unit in [5, 6, 10, 15]:
            self.settings.granularity = unit
            self.assertEqual('create', self.preview([actual()])['rows'][0]['action'])
        row = dict(actual(), start='2026-10-09T00:00:00Z', end='2026-10-09T01:00:00Z')
        self.assertEqual('2026-10-09T09:00:00', self.preview([row])['rows'][0]['payload']['startTime'])

    def test_overlap_paging_all_tasks_and_selected_rows(self):
        self.mock.page_size = 1
        self.mock.add({'workItemId':'unrelated', 'startTime':'2026-10-09T08:00:00', 'finishTime':'2026-10-09T08:30:00'})
        self.mock.add({'workItemId':'another', 'startTime':'2026-10-09T09:30:00', 'finishTime':'2026-10-09T10:30:00'})
        self.assertEqual('overlap', self.preview([actual()])['rows'][0]['code'])
        gets = [c for c in self.mock.calls if c[2]]
        self.assertEqual([0,1], [c[2]['offset'] for c in gets])
        self.assertTrue(all('workItemId' not in c[2] for c in gets))
        self.mock.rows.clear()
        self.assertTrue(all(r['code']=='overlap' for r in self.preview([actual(), actual('a2', '09:30', '10:30')])['rows']))

    def test_remote_conflict_even_local_unchanged(self):
        self.apply([actual()])
        self.mock.rows['1']['updatedAt'] = 'remote-new'
        self.assertEqual('conflict', self.preview([actual()])['rows'][0]['code'])
        self.mock.rows.clear()
        self.assertEqual('conflict', self.preview([actual()])['rows'][0]['code'])

    def test_task_change_locked_and_stale_preview(self):
        self.apply([actual()])
        self.adapter.register_task('task-1', 'https://example.invalid/task', '146')
        self.assertEqual('task_changed', self.preview([actual()])['rows'][0]['code'])
        self.adapter.register_task('task-1', 'https://example.invalid/task', '145')
        self.mock.rows['1']['isLocked'] = True
        self.assertEqual('WorkItemLocked', self.preview([actual()])['rows'][0]['code'])
        self.mock.rows['1']['isLocked'] = False
        token = self.preview([actual()])['token']
        with self.assertRaises(NXError) as caught:
            self.adapter.apply([actual(memo='changed')], token)
        self.assertEqual('stale_preview', caught.exception.code)

    def test_timeout_after_commit_and_restart_never_retry(self):
        self.mock.fail = 'after'
        self.assertEqual('unknown', self.apply([actual()])[0]['status'])
        self.adapter = NXAdapter(self.settings, Journal(self.path), self.mock)
        self.assertEqual('unknown', self.preview([actual()])['rows'][0]['code'])
        with self.assertRaises(NXError):
            self.apply([actual()])
        self.assertEqual(1, len(self.mock.mutations()))
        self.mock.fail = None
        self.assertEqual('synced', self.adapter.reconcile('actual-1'))
        self.assertEqual('unchanged', self.apply([actual()])[0]['status'])

    def test_no_match_remains_pending_and_multiple_candidates_ambiguous(self):
        self.mock.fail = 'before'
        self.apply([actual()])
        self.assertEqual('unresolved', self.adapter.reconcile('actual-1'))
        payload = Journal(self.path).read()['entries']['actual-1']['payload']
        self.mock.add(payload); self.mock.add(payload)
        self.assertEqual('ambiguous', self.adapter.reconcile('actual-1'))
        self.assertEqual('unknown', Journal(self.path).read()['entries']['actual-1']['status'])

    def test_put_unknown_reconciles_same_id(self):
        self.apply([actual()])
        self.mock.fail = 'after'
        self.assertEqual('unknown', self.apply([actual(end='10:30')])[0]['status'])
        self.assertEqual('synced', self.adapter.reconcile('actual-1'))
        self.assertEqual(1, len(self.mock.rows))

    def test_readback_error_never_drops_committed_intent(self):
        self.mock.fail = 'readback'
        self.assertEqual('unknown', self.apply([actual()])[0]['status'])
        self.assertEqual('unknown', Journal(self.path).read()['entries']['actual-1']['status'])
        self.mock.readback_fail = False
        self.assertEqual('synced', self.adapter.reconcile('actual-1'))

    def test_known_rejection_sanitized_and_batch_stops(self):
        self.mock.reject = 'WorkItemNotAssigned'
        result = self.apply([actual(), actual('a2','10:00','11:00')])
        self.assertEqual('WorkItemNotAssigned', result[0]['code'])
        self.assertEqual({}, Journal(self.path).read()['entries'])
        self.assertEqual(1, len(self.mock.mutations()))

    def test_optional_categories_omitted_and_server_iso_variants_match(self):
        self.apply([actual()])
        sent = self.mock.mutations()[0][3]
        self.assertNotIn('timeEntryCategoryId', sent)
        self.assertNotIn('processCategoryId', sent)
        self.mock.rows['1']['startTime'] += '.000'
        self.mock.rows['1']['timeEntryCategoryId'] = None
        self.assertEqual('unchanged', self.preview([actual()])['rows'][0]['action'])

    def test_category_clear_is_blocked(self):
        self.adapter.register_task('task-1','https://example.invalid/task','145',{'timeEntryCategoryId':'4'})
        self.apply([actual()])
        self.adapter.register_task('task-1','https://example.invalid/task','145')
        self.assertEqual('category_clear_unsupported',self.preview([actual()])['rows'][0]['code'])

    def test_malformed_success_response_and_exception_text_are_unknown(self):
        self.mock.fail='malformed'
        self.assertEqual('unknown',self.apply([actual()])[0]['status'])
        self.assertEqual('synced',self.adapter.reconcile('actual-1'))
        original=self.mock.request
        def fail(method,path,**kw):
            if method!='GET':raise RuntimeError('synthetic-secret-must-not-surface')
            return original(method,path,**kw)
        self.mock.request=fail
        result=self.apply([actual('actual-2','10:00','11:00')])
        self.assertEqual('unknown',result[0]['status'])
        self.assertNotIn('synthetic-secret',str(result))

    def test_identity_scope_changes_block(self):
        self.apply([actual()])
        self.mock.user = '22'
        with self.assertRaises(NXError):
            self.preview([actual()])
        self.mock.user = '21'
        self.settings.api_base = 'https://evil.invalid/api'
        with self.assertRaises(NXError):
            self.preview([actual()])

    def test_bad_urls_and_live_transport_rejected_without_requests(self):
        for url in ['https://evil.invalid/task', '/'.join(['https:', '', 'evil.invalid@example.invalid', 't']),
                    'http://example.invalid/t', 'https://example.invalid/t?token=synthetic',
                    'https://example.invalid/t#fragment', 'https://example.invalid:444/t']:
            with self.assertRaises(NXError):
                self.adapter.register_task('task-1', url, '145')
        with self.assertRaises(NXError):
            NXAdapter(self.settings, Journal(self.path), object())
        self.assertFalse(self.mock.calls)

    def test_duplicate_ids_bad_paging_and_crash_intent(self):
        with self.assertRaises(NXError):
            self.preview([actual(), actual()])
        original = self.mock.request
        def bad(method, path, **kw):
            if kw.get('query'):
                return {'totalCount':1, 'data':[]}
            return original(method, path, **kw)
        self.mock.request = bad
        with self.assertRaises(NXError):
            self.preview([actual()])
        self.mock.request = original
        original_write = self.adapter.journal.write
        def crash(value):
            original_write(value)
            if value['entries']:
                raise SystemExit('synthetic crash after durable intent')
        self.adapter.journal.write = crash
        with self.assertRaises(SystemExit):
            self.apply([actual()])
        self.assertFalse(self.mock.mutations())
        self.assertEqual('unknown', self.preview([actual()])['rows'][0]['code'])


if __name__ == '__main__':
    unittest.main()
