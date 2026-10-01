import datetime as dt
import unittest
import tempfile
import pathlib
import json
from webapp import api
from webapp.store import Store
from types import SimpleNamespace
from webapp.store import Entity, InputError
from webapp import local_calendar


def task(identifier, **fields):
    return Entity(identifier, 'task', 'tasks/'+identifier+'.md', {'title': 'Synthetic task', 'status': 'next', **fields}, '', 'abc')

class CalendarTest(unittest.TestCase):
    def project(self, entities=(), view='week', date='2026-11-04', **kwargs):
        store = SimpleNamespace(read_snapshot=lambda: SimpleNamespace(entities=entities, recovery_required=False))
        return local_calendar.calendar_projection(store, view, date, **kwargs)

    def test_day_week_month_jst_ranges(self):
        self.assertEqual(len(self.project(view='day')['days']), 1)
        week = self.project()
        self.assertEqual(week['days'][0]['date'], '2026-11-02')
        self.assertEqual(len(week['days']), 7)
        self.assertEqual(len(self.project(view='month')['days']), 42)
        self.assertEqual(week['timezone'], 'Asia/Tokyo')

    def test_plan_actual_and_deadline_are_separate_and_midnight_split(self):
        result = self.project([task('sample', status='done', work_started_at='2026-11-03T14:30:00Z', work_ended_at='2026-11-03T15:30:00Z'), task('plan', scheduled_start='2026-11-03T23:00:00+09:00', scheduled_end='2026-11-04T01:00:00+09:00', action_date='2026-11-03', due='2026-11-05')])
        events = [e for day in result['days'] for e in day['events']]
        self.assertEqual([e['kind'] for e in events].count('actual'), 2)
        self.assertEqual([e['kind'] for e in events].count('planned'), 2)
        self.assertEqual([e['kind'] for e in events].count('deadline'), 1)
        self.assertFalse(any(e['kind']=='action' for e in events))
        self.assertEqual(sum(e['minutes'] for e in events if e['kind']=='actual'), 60)

    def test_all_day_and_open_session_use_injected_clock(self):
        result = self.project([task('all', action_date='2026-11-04'), task('active', status='doing', work_started_at='2026-11-04T09:00:00+09:00')], now=dt.datetime.fromisoformat('2026-11-04T09:25:00+09:00'))
        events = result['days'][2]['events']
        self.assertEqual(events[0]['kind'], 'action')
        actual = next(e for e in events if e['kind']=='actual')
        self.assertEqual(actual['minutes'], 25)
        self.assertTrue(actual['provisional'])

    def test_archived_actual_remains_visible_and_completed_plan_is_hidden(self):
        archived = task('archived', status='done', action_date='2026-11-04', work_started_at='2026-11-04T10:00:00+09:00', work_ended_at='2026-11-04T10:30:00+09:00')
        archived = Entity(archived.entity_id, archived.entity_type, 'archive/tasks/archived.md', archived.frontmatter, archived.body, archived.content_hash)
        done = task('done',status='done',scheduled_start='2026-11-04T09:00:00+09:00',scheduled_end='2026-11-04T10:00:00+09:00')
        events = self.project([archived,done],view='day')['days'][0]['events']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['kind'],'actual')
        self.assertTrue(events[0]['archived'])

    def test_range_clips_cross_boundary_and_jst_deadline(self):
        entity = task('cross',scheduled_start='2026-11-03T23:00:00+09:00',scheduled_end='2026-11-05T01:00:00+09:00',due='2026-11-03T15:00:00Z')
        events = self.project([entity],view='day')['days'][0]['events']
        planned = next(e for e in events if e['kind']=='planned')
        self.assertEqual(planned['minutes'],1440)
        self.assertEqual(planned['start'],'2026-11-04T00:00:00+09:00')
        self.assertEqual(planned['end'],'2026-11-05T00:00:00+09:00')
        self.assertTrue(any(e['kind']=='deadline' for e in events))

    def test_invalid_query_and_naive_timestamps(self):
        for view, date in [('bad','2026-11-04'), ('day','2026-02-30'), ('day','26-11-04'), ('week','0001-01-01')]:
            with self.assertRaises(InputError): self.project(view=view,date=date)
        result = self.project([task('bad',scheduled_start='2026-11-04T09:00:00',scheduled_end='2026-11-04T10:00:00')])
        self.assertFalse(any(d['events'] for d in result['days']))

class CalendarMutationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        for name in ('inbox','tasks','purposes','visions','areas','projects','goals','reviews/daily','reviews/weekly','archive'):
            (self.root/name).mkdir(parents=True)
        self.store = Store(self.root)
        self.state = api.ApiState(self.store)
        self.headers = {'Origin':'http://127.0.0.1:24873','X-GTD-Web':'1','Content-Type':'application/json'}

    def mutate(self, operation):
        status, preview = api.handle(self.store,'POST','/api/v1/mutations/preview',headers=self.headers,body=json.dumps(operation).encode(),state=self.state)
        self.assertEqual(status, 200, preview)
        status, result = api.handle(self.store,'POST','/api/v1/mutations/apply',headers=self.headers,body=json.dumps({'preview':{k:v for k,v in preview.items() if k!='preview_hash'},'preview_hash':preview['preview_hash']}).encode(),state=self.state)
        self.assertIn(status,(200,201),result)
        return result

    def test_create_and_reschedule_using_existing_api_preserves_body(self):
        created = self.mutate({'action':'create','kind':'tasks','fields':{'title':'Synthetic schedule','status':'scheduled','action_date':'2026-11-04','scheduled_start':'2026-11-04T09:00:00+09:00','scheduled_end':'2026-11-04T10:00:00+09:00'},'body':'Synthetic notes'})
        changed = self.mutate({'action':'update','kind':'tasks','id':created['id'],'base_hash':created['content_hash'],'fields':{'title':'Synthetic schedule','status':'next','action_date':'2026-11-05','scheduled_start':'','scheduled_end':''}})
        self.assertEqual(changed['body'],created['body'])
        events = local_calendar.calendar_projection(self.store,'day','2026-11-05')['days'][0]['events']
        self.assertEqual([e['kind'] for e in events],['action'])

    def test_create_all_day_with_empty_schedule_fields(self):
        self.mutate({'action':'create','kind':'tasks','fields':{'title':'Synthetic day','status':'next','action_date':'2026-11-04','scheduled_start':'','scheduled_end':''},'body':''})

if __name__ == '__main__': unittest.main()
