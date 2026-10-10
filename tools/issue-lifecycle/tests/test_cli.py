"""Real CLI subprocesses, isolated git directory and fake gh transport only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from test_gate import Fake, POLICY, REPO, ROOT, HEAD
from gate import Gate, APPROVAL, MANIFEST, packet, digest

SCRIPT = Path(__file__).resolve().parents[1] / 'gate.py'
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = args[args.index('--method') + 2] if '--method' in args else args[1]
method = args[args.index('--method') + 1] if '--method' in args else 'GET'
db = json.loads(Path(os.environ['FAKE_DB']).read_text())
with open(os.environ['FAKE_CALLS'], 'a') as f: f.write(method + ' ' + path + '\\n')
if method == 'POST' and path == 'graphql':
    print(json.dumps({'data': {'repository': {'pullRequest': {'closingIssuesReferences': {'totalCount': 0, 'nodes': []}}}}}))
    sys.exit(0)
path = path.replace('?per_page=100&page=1', '').replace('&per_page=100&page=1', '')
if path.endswith('/artifacts'):
    deploy='/runs/77/' in path
    print(json.dumps({'artifacts':[{'id':322 if deploy else 321,'name':'issue-deploy-receipts' if deploy else 'issue-start-receipts','expired':False}]}));sys.exit(0)
if path.endswith('/artifacts/321/zip'):
    import io,zipfile
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:z.writestr('receipts.json',json.dumps(db['_start_proof']))
    sys.stdout.buffer.write(buffer.getvalue());sys.exit(0)
if path.endswith('/artifacts/322/zip'):
    import io,zipfile
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:z.writestr('receipts.json',json.dumps(db['_deploy_proof']))
    sys.stdout.buffer.write(buffer.getvalue());sys.exit(0)
if method != 'GET': sys.exit(77)
path = path.replace('?per_page=100&page=1', '').replace('&per_page=100&page=1', '')
if path not in db: sys.exit(78)
value = db[path]
if path.endswith('/check-runs'): value = {'check_runs': value}
print(json.dumps(value))
'''

class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fake = self.root / 'gh'; fake.write_text(FAKE_GH); fake.chmod(0o700)
        self.policy = self.root / 'policy.json'; self.policy.write_text(json.dumps(POLICY))
        self.db = self.root / 'db.json'
        self.calls = self.root / 'calls.log'
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        FAKE_DB=str(self.db), FAKE_CALLS=str(self.calls))

    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), '--policy', str(self.policy), *args],
                              env=self.env, cwd=cwd, capture_output=True, text=True)

    def fixture(self):
        api = Fake(); gate = Gate(api, POLICY)
        m = gate.start([1], 'a' * 40, True, 'work', REPO, workflow_run_id=42)
        api.publish_start_receipts(m)
        m['head'] = HEAD
        m['issues'][0]['acceptance'] = {ac: {'implementation': 'src/file.py', 'verification': 'test'} for ac in ('AC1', 'AC2')}
        api.data[ROOT + '/pulls/10']['body'] += '\n' + packet(MANIFEST, m)
        c = api.call(ROOT + '/issues/1/comments', 'POST', {'body': packet(APPROVAL,
                     {'repo': REPO, 'issue': 1, 'approved': True, 'heads': [HEAD], 'runs': [m['run']],
                      'target_digest': digest(gate.finish_plan([{'pr':10,'manifest':m}], {}, target_only=True))})})
        api.data['_start_proof']=api.proofs[(42,'issue-start-receipts')]
        api.data['_deploy_proof']=api.proofs[(77,'issue-deploy-receipts')]
        self.db.write_text(json.dumps(api.data))
        return m, c['id']

    def test_pr_check_from_remote_body_real_cli(self):
        self.fixture()
        result = self.run_cli('pr-check', '--pr', '10')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['head'], HEAD)
        self.assertNotIn('PATCH ', self.calls.read_text())

    def test_finish_default_dry_run_real_cli(self):
        m, approval = self.fixture()
        payload = self.root / 'finish.json'
        payload.write_text(json.dumps({'prs': [{'pr': 10, 'manifest': m}], 'approvals': {'1': approval}}))
        journal = self.root / 'journal.json'
        result = self.run_cli('finish', '--input', str(payload), '--journal', str(journal))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['dry_run'])
        self.assertFalse(journal.exists())
        self.assertNotIn('PATCH ', self.calls.read_text())
        # Only GraphQL read uses POST, not Issue mutations.
        self.assertTrue(all(line.startswith('GET ') or line == 'POST graphql' for line in self.calls.read_text().splitlines()))

    def test_dirty_start_blocks_before_any_gh_call(self):
        repo = self.root / 'repo'; repo.mkdir()
        subprocess.run(['git', 'init', '-b', 'work', str(repo)], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/' + REPO + '.git'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--allow-empty', '-m', 'fixture baseline'], capture_output=True, check=True)
        (repo / 'implementation.py').write_text('already changed')
        result = self.run_cli('start', '--issue', '1', '--output', str(self.root / 'manifest.json'),
                              '--journal', str(self.root / 'start.json'), cwd=repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('clean before start', result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root / 'start.json').exists())

    def test_invalid_draft_policy_fails_closed(self):
        policy = dict(POLICY, required_checks=[{'name': 'not-configured', 'app_id': None}])
        self.policy.write_text(json.dumps(policy))
        result = self.run_cli('pr-check', '--pr', '10')
        self.assertEqual(result.returncode, 1)
        self.assertIn('workflow binding missing', result.stderr)
        self.assertFalse(self.calls.exists())

if __name__ == '__main__': unittest.main()
