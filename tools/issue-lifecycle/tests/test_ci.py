import copy
import json
import unittest
from test_gate import Fake, ROOT, HEAD, POLICY, START, packet
from gate import Gate, GateError, MANIFEST
from ci_check import run, recheck_open

class CheckFake(Fake):
    def __init__(self):
        super().__init__(); self.checks=[]; self.move_head=False
    def call(self,path,method='GET',data=None):
        if path == ROOT+'/check-runs' and method=='POST':
            ident=900+len(self.checks); self.checks.append(dict(data,id=ident)); return {'id':ident}
        if path.startswith(ROOT+'/check-runs/') and method=='PATCH':
            target=next(c for c in self.checks if c['id']==int(path.split('/')[-1])); target.update(data); return target
        return super().call(path,method,data)

class CITests(unittest.TestCase):
    def fixture(self):
        api=CheckFake(); gate=Gate(api,copy.deepcopy(POLICY))
        m=gate.start([1],'a'*40,True,'work',POLICY['repository'],workflow_run_id=42);api.publish_start_receipts(m);m['head']=HEAD
        m['issues'][0]['acceptance']={'AC1':{'implementation':'file','verification':'test'}}
        api.data[ROOT+'/pulls/10']['body']='Refs https://github.com/example/private/issues/1\n'+packet(MANIFEST,m)
        return api
    def test_success_published_on_pr_head(self):
        api=self.fixture();result=run(api,POLICY,10)
        self.assertEqual(result['head'],HEAD);self.assertEqual(api.checks[0]['head_sha'],HEAD)
        self.assertEqual(api.checks[0]['conclusion'],'success')
    def test_missing_manifest_publishes_failure_on_head(self):
        api=self.fixture();api.data[ROOT+'/pulls/10']['body']='No Issue'
        with self.assertRaises(RuntimeError):run(api,POLICY,10)
        self.assertEqual(api.checks[0]['head_sha'],HEAD);self.assertEqual(api.checks[0]['conclusion'],'failure')
    def test_head_changed_during_validation_never_publishes_success(self):
        api=self.fixture();original=api.closing_issues
        def mutate(repo,pr):
            api.data[ROOT+'/pulls/10']['head']['sha']='d'*40
            return original(repo,pr)
        api.closing_issues=mutate
        with self.assertRaises(RuntimeError):run(api,POLICY,10)
        self.assertEqual(api.checks[0]['conclusion'],'failure')

    def test_same_head_invalid_second_pr_blocks_shared_status(self):
        api=self.fixture(); second=copy.deepcopy(api.data[ROOT+'/pulls/10']); second.update(number=11,body='No Issue')
        api.data[ROOT+'/pulls/11']=second;api.data[ROOT+'/pulls?state=open'].append(second)
        with self.assertRaises(RuntimeError):run(api,POLICY,10)
        self.assertEqual(api.checks[0]['conclusion'],'failure')

    def test_failed_first_head_does_not_leave_other_head_unchecked(self):
        api=self.fixture(); first=api.data[ROOT+'/pulls/10']; first['body']='No Issue'
        second=copy.deepcopy(first);second.update(number=11,head={'sha':'d'*40})
        api.data[ROOT+'/pulls/11']=second;api.data[ROOT+'/pulls?state=open'].append(second)
        result=recheck_open(api,POLICY)
        self.assertEqual(len(result['failures']),2)
        self.assertEqual({c['head_sha'] for c in api.checks},{HEAD,'d'*40})
        self.assertTrue(all(c['conclusion']=='failure' for c in api.checks))

if __name__=='__main__':unittest.main()
