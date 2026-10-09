import pathlib,shutil,subprocess,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class TaskOperationsRuntimeTest(unittest.TestCase):
    def test_operations_contract(self):
        if not shutil.which('node'):self.skipTest('node unavailable')
        result=subprocess.run(['node','tests/task_operations_runtime.js'],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

class ContinuationStoreContractTest(unittest.TestCase):
    def test_completed_copy_preserves_source_actuals_dependencies_and_exact_retry(self):
        import tempfile,datetime
        from webapp.store import Store,DestinationConflict,ConflictError,InputError
        from webapp.integrations.actuals import task_actuals
        for action_date in ('2026-10-09','2026-10-12',''):
            with self.subTest(action_date=action_date),tempfile.TemporaryDirectory() as temporary:
                root=pathlib.Path(temporary)
                for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(root/name).mkdir(parents=True)
                store=Store(root)
                source=store.apply_mutation_plan(store.plan_create_entity('task',{'title':'Synthetic original','status':'done','work_started_at':'2026-10-09T01:00:00+00:00','work_ended_at':'2026-10-09T01:20:00+00:00'},'User body\n'))
                dependent=store.apply_mutation_plan(store.plan_create_entity('task',{'title':'Released downstream','status':'next','depends_on':'['+source.entity_id+']'},''))
                prior=task_actuals(store.read_snapshot())[0].to_dict()
                source_bytes=(root/source.relative_path).read_bytes();dependent_bytes=(root/dependent.relative_path).read_bytes()
                receipt=root/'synthetic-nx-receipt.json';receipt.write_text('{"remote_id":"synthetic-existing-nx","state":"unknown"}');receipt_bytes=receipt.read_bytes()
                fields={'title':'Synthetic original','status':'next','continuation_of':source.entity_id}
                if action_date:fields['action_date']=action_date
                plan=store.plan_create_entity('task',fields,'User body\n');copy=store.apply_mutation_plan(plan)
                self.assertEqual((root/source.relative_path).read_bytes(),source_bytes)
                self.assertEqual((root/dependent.relative_path).read_bytes(),dependent_bytes)
                self.assertEqual(receipt.read_bytes(),receipt_bytes)
                self.assertEqual(task_actuals(store.read_snapshot())[0].to_dict(),prior)
                self.assertEqual(copy.frontmatter['continuation_of'],source.entity_id)
                self.assertEqual(copy.frontmatter.get('action_date',''),action_date)
                for key in ('work_started_at','work_ended_at','calendar_id','calendar_event_id','started_at','completed_at','remote_id','timer_kind'):
                    self.assertNotIn(key,copy.frontmatter)
                with self.assertRaises((DestinationConflict,ConflictError,InputError)):
                    store.apply_mutation_plan(plan)
                copies=[x for x in store.list_entities() if x.frontmatter.get('continuation_of')==source.entity_id]
                self.assertEqual(len(copies),1)
