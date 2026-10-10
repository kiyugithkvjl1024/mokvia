import copy
import json
import tempfile
import unittest
from pathlib import Path
from gate import Gate, GateError, START, APPROVAL, FINISH, MANIFEST, TRANSITION, packet, digest

REPO = 'example/private'
ROOT = 'repos/' + REPO
BASE, HEAD, MERGE = 'a' * 40, 'b' * 40, 'c' * 40
POLICY = {'repository': REPO, 'base_branch': 'main', 'approvers': ['owner'],
          'required_checks': [{'name': 'test', 'app_id': 123, 'workflow': '.github/workflows/test.yml'}], 'deployment_environments': ['production'],
          'deployment_issuers': ['deploy-bot'], 'deployment_workflows': ['.github/workflows/deploy.yml'], 'start_issuers': ['start-bot'],
          'start_workflow': '.github/workflows/issue-start.yml'}

class Fake:
    def __init__(self):
        self.data, self.writes = {}, []
        self.next_id = 100
        self.closing = []
        self.proofs = {(77,'issue-deploy-receipts'):{'receipts':[{'deployment_id':1,'status_id':1,'sha':MERGE,'environment':'production','state':'success'}]}}
        self.fail_close = None
        self.fail_comment = False
        self.data[ROOT] = {'full_name': REPO, 'archived': False}
        self.data['user'] = {'login': 'start-bot'}
        self.data[ROOT + '/actions/runs/42'] = {'head_sha': BASE, 'event': 'workflow_dispatch',
          'path': POLICY['start_workflow'], 'head_branch': 'main', 'status': 'completed', 'conclusion': 'success'}
        self.data[ROOT + '/actions/runs/77'] = {'head_sha': MERGE, 'event': 'push', 'head_branch': 'main',
           'path': POLICY['deployment_workflows'][0], 'status': 'completed', 'conclusion': 'success'}
        self.data[ROOT + '/collaborators/owner/permission'] = {'permission': 'admin'}
        self.data[ROOT + '/commits/' + BASE] = {'sha': BASE}
        self.data[ROOT + '/git/ref/heads/main'] = {'object': {'sha': BASE}}
        self.data[ROOT + '/compare/' + BASE + '...' + HEAD] = {'status': 'ahead', 'total_commits': 1, 'commits': [{'commit': {'committer': {'date': '2026-01-02T12:00:00Z'}}}]}
        self.data[ROOT + '/commits/' + MERGE + '/check-runs'] = [
            {'id': 1, 'name': 'test', 'app': {'id': 123}, 'check_suite': {'id': 10}, 'head_sha': MERGE, 'status': 'completed', 'conclusion': 'success'}]
        self.data[ROOT + '/deployments?sha=' + MERGE] = [
            {'id': 1, 'sha': MERGE, 'environment': 'production', 'transient_environment': False, 'creator': {'login': 'deploy-bot'}, 'payload': {'workflow_run_id': 77}}]
        self.data[ROOT + '/deployments/1/statuses'] = [{'id': 1, 'state': 'success', 'creator': {'login': 'deploy-bot'}}]
        for n in (1, 2):
            self.data[f'{ROOT}/issues/{n}'] = {'number': n, 'html_url': f'https://github.com/{REPO}/issues/{n}',
                'state': 'open', 'state_reason': None, 'body': '## 要求\nDo work\n## 受入条件\n- AC1: works\n- AC2: tested',
                'created_at': '2026-01-01T00:00:00Z'}
            self.data[f'{ROOT}/issues/{n}/events'] = []
            self.data[f'{ROOT}/issues/{n}/timeline'] = []
            self.data[f'{ROOT}/issues/{n}/comments'] = []
        self.data[ROOT + '/pulls/10'] = {'number': 10, 'state': 'open', 'merged_at': '2026-01-04T00:00:00Z',
            'merge_commit_sha': MERGE, 'created_at': '2026-01-03T00:00:00Z',
            'head': {'sha': HEAD}, 'base': {'ref': 'main', 'repo': {'full_name': REPO}},
            'commits': 1, 'changed_files': 1, 'body': f'Refs https://github.com/{REPO}/issues/1 https://github.com/{REPO}/issues/2'}

        run={'id':78,'path':'.github/workflows/test.yml','head_sha':MERGE,'head_branch':'main','event':'push','status':'completed','conclusion':'success'}
        self.data[ROOT+'/actions/runs?check_suite_id=10']=[run]
        self.data[ROOT+'/actions/runs/78/jobs']=[{'check_run_url':f'https://api.github.com/{ROOT}/check-runs/1'}]
        self.data[ROOT+'/actions/runs?check_suite_id=11']=[dict(run,id=79,head_sha='d'*40)]
        self.data[ROOT+'/actions/runs/79/jobs']=[{'check_run_url':f'https://api.github.com/{ROOT}/check-runs/2'}]
        self.data[ROOT+'/pulls?state=open']=[self.data[ROOT+'/pulls/10']]
        self.data[ROOT + '/pulls/10/files'] = [{'filename':'src/application.py'}]
        self.data[ROOT + '/pulls/10/commits'] = self.data[ROOT + '/compare/' + BASE + '...' + HEAD]['commits']

    def artifact_payload(self,run_id,name):
        return copy.deepcopy(self.proofs[(run_id,name)])

    def publish_start_receipts(self,m):
        records=[]
        for entry in m['issues']:
            c=self.data[f'{ROOT}/issues/comments/{entry["start_comment"]}']
            value=json.loads(c['body'].split('```json\n')[1].split('\n```')[0])
            records.append({'issue':entry['number'],'comment_id':c['id'],'payload_digest':digest(value)})
        self.proofs[(42,'issue-start-receipts')]={'receipts':records}

    def closing_issues(self, repo, pr):
        return self.closing

    def pages(self, path):
        return copy.deepcopy(self.data[path])

    def call(self, path, method='GET', data=None):
        if method == 'GET':
            return copy.deepcopy(self.data[path])
        self.writes.append((path, method, data))
        if method == 'POST':
            if self.fail_comment:
                raise GateError('unknown POST result')
            n = int(path.split('/')[-2])
            self.next_id += 1
            c = {'id': self.next_id, 'body': data['body'], 'user': {'login': 'start-bot' if data['body'].startswith('<!-- '+START) else 'owner'},
                 'created_at': '2026-01-02T00:00:00Z', 'updated_at': '2026-01-02T00:00:00Z',
                 'issue_url': f'https://api.github.com/{ROOT}/issues/{n}'}
            self.data[f'{ROOT}/issues/comments/{c["id"]}'] = c
            self.data[f'{ROOT}/issues/{n}/comments'].append(c)
            return copy.deepcopy(c)
        if method == 'PATCH':
            if self.fail_close == path:
                raise GateError('unknown PATCH result')
            self.data[path].update(data)
            return copy.deepcopy(self.data[path])
        raise AssertionError(method)

class GateTests(unittest.TestCase):
    def setUp(self):
        self.api = Fake()
        self.gate = Gate(self.api, copy.deepcopy(POLICY))

    def started(self, numbers=(1,)):
        manifest = self.gate.start(list(numbers), BASE, True, 'work', REPO, workflow_run_id=42)
        self.api.publish_start_receipts(manifest)
        manifest['head'] = HEAD
        for x in manifest['issues']:
            x['acceptance'] = {ac: {'implementation': 'src/file.py', 'verification': 'test case'} for ac in ('AC1', 'AC2')}
        self.api.data[ROOT + '/pulls/10']['body'] += '\n' + packet(MANIFEST, manifest)
        self.api.writes.clear()
        return manifest

    def completion(self, numbers=(1,)):
        manifest = self.started(numbers)
        approvals = {}
        for n in numbers:
            target = self.gate.finish_plan([{'pr': 10, 'manifest': manifest}], {}, target_only=True)
            payload = {'repo': REPO, 'issue': n, 'approved': True, 'runs': [manifest['run']], 'heads': [HEAD], 'target_digest': digest(target)}
            c = self.api.call(f'{ROOT}/issues/{n}/comments', 'POST', {'body': packet(APPROVAL, payload)})
            approvals[str(n)] = c['id']
        self.api.writes.clear()
        return [{'pr': 10, 'manifest': manifest}], approvals

    def blocked(self, fn):
        with self.assertRaises((GateError, KeyError, TypeError)):
            fn()
        self.assertEqual(self.api.writes, [])

    def test_start_and_pr_pass(self):
        m = self.started((1, 2))
        self.assertEqual(len(self.gate.pr_check(10, m)['issues']), 2)

    def test_start_rejects_wrong_repo_dirty_main_empty_duplicate(self):
        for args in [([1], BASE, False, 'work', REPO), ([1], BASE, True, 'main', REPO),
                     ([1], BASE, True, 'work', 'other/repo'), ([], BASE, True, 'work', REPO),
                     ([1, 1], BASE, True, 'work', REPO)]:
            self.blocked(lambda: self.gate.start(*args, workflow_run_id=42))

    def test_start_validates_all_before_post(self):
        self.api.data[ROOT + '/issues/2']['state'] = 'closed'
        self.blocked(lambda: self.gate.start([1, 2], BASE, True, 'work', REPO, workflow_run_id=42))

    def test_missing_requirement_acceptance_and_duplicate_ac(self):
        for body in ['none', '## 要求\nwork', '## 要求\nwork\n- AC1: a\n- AC1: b']:
            self.api.data[ROOT + '/issues/1']['body'] = body
            self.blocked(lambda: self.gate.start([1], BASE, True, 'work', REPO, workflow_run_id=42))

    def test_pr_issue_not_allowed(self):
        self.api.data[ROOT + '/issues/1']['pull_request'] = {}
        self.blocked(lambda: self.gate.start([1], BASE, True, 'work', REPO, workflow_run_id=42))

    def test_require_server_baseline_and_writer(self):
        del self.api.data[ROOT + '/commits/' + BASE]
        self.blocked(lambda: self.gate.start([1], BASE, True, 'work', REPO, workflow_run_id=42))
        self.api.data[ROOT + '/commits/' + BASE] = {}
        self.api.data['user']['login'] = 'untrusted-writer'
        self.blocked(lambda: self.gate.start([1], BASE, True, 'work', REPO, workflow_run_id=42))

    def test_after_pr_receipt_is_not_prestart(self):
        m = self.started()
        c = self.api.data[f'{ROOT}/issues/comments/{m["issues"][0]["start_comment"]}']
        c['created_at'] = c['updated_at'] = '2026-01-05T00:00:00Z'
        self.blocked(lambda: self.gate.pr_check(10, m))

    def test_edited_receipt_rejected(self):
        m = self.started()
        self.api.data[f'{ROOT}/issues/comments/{m["issues"][0]["start_comment"]}']['updated_at'] = '2026-01-05T00:00:00Z'
        self.blocked(lambda: self.gate.pr_check(10, m))

    def test_changed_requirement_and_reopen_require_new_start(self):
        m = self.started()
        self.api.data[ROOT + '/issues/1']['body'] += '\nNew requirement'
        self.blocked(lambda: self.gate.pr_check(10, m))
        self.setUp()
        m = self.started()
        self.api.data[ROOT + '/issues/1/events'] = [{'id': 2, 'event': 'reopened', 'created_at': '2026-01-05T00:00:00Z'}]
        self.blocked(lambda: self.gate.pr_check(10, m))

    def test_new_start_after_reopen_passes(self):
        self.api.data[ROOT + '/issues/1/events'] = [{'id': 2, 'event': 'reopened', 'created_at': '2026-01-01T12:00:00Z'}]
        m = self.started()
        self.gate.pr_check(10, m)

    def test_stale_head_policy_ancestry_and_issue_url(self):
        for mutation in ('head', 'policy', 'ancestry', 'url', 'run', 'target'):
            self.setUp()
            m = self.started()
            if mutation == 'head': m['head'] = 'd' * 40
            if mutation == 'policy': self.gate.policy['required_checks'][0]['name'] = 'other-check'
            if mutation == 'ancestry': self.api.data[ROOT + '/compare/' + BASE + '...' + HEAD]['status'] = 'diverged'
            if mutation == 'url': self.api.data[ROOT + '/pulls/10']['body'] = 'Refs #1'
            if mutation == 'run': m['run'] = 'other'
            if mutation == 'target': m['issues'][0]['start_comment'] = 999
            if mutation != 'url':
                self.api.data[ROOT + '/pulls/10']['body'] = f'Refs https://github.com/{REPO}/issues/1\n' + packet(MANIFEST, m)
            self.blocked(lambda: self.gate.pr_check(10, m))

    def test_autoclose_keyword_variants(self):
        for keyword in ['Closes #1', 'CLOSES: #1', 'Fixes example/private#1', 'Resolved https://github.com/example/private/issues/1']:
            self.setUp()
            m = self.started()
            self.api.data[ROOT + '/pulls/10']['body'] += '\n' + keyword
            self.blocked(lambda: self.gate.pr_check(10, m))

    def test_ac_mapping_not_empty_or_unknown(self):
        m = self.started()
        for mapping in ({}, {'AC3': {'implementation': 'x', 'verification': 'y'}}, {'AC1': {'implementation': '', 'verification': 'y'}}):
            m['issues'][0]['acceptance'] = mapping
            self.api.data[ROOT + '/pulls/10']['body'] = f'Refs https://github.com/{REPO}/issues/1\n' + packet(MANIFEST, m)
            self.blocked(lambda: self.gate.pr_check(10, m))

    def test_finish_dry_run_has_zero_writes(self):
        prs, approvals = self.completion()
        self.assertEqual(self.gate.finish(prs, approvals, '/unused')['status'], 'ready')
        self.assertEqual(self.api.writes, [])

    def test_finish_two_issues_close_only_after_evidence(self):
        prs, approvals = self.completion((1, 2))
        with tempfile.TemporaryDirectory() as d:
            result = self.gate.finish(prs, approvals, Path(d) / 'journal.json', True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([x[1] for x in self.api.writes], ['POST', 'PATCH', 'POST', 'PATCH'])
        self.assertTrue(all(self.api.data[f'{ROOT}/issues/{n}']['state'] == 'closed' for n in (1, 2)))

    def test_multiple_prs_union_acceptance(self):
        prs, approvals = self.completion()
        second = copy.deepcopy(prs[0]); second['pr'] = 11
        self.api.data[ROOT + '/pulls/11'] = copy.deepcopy(self.api.data[ROOT + '/pulls/10'])
        self.api.data[ROOT + '/pulls/11/files'] = copy.deepcopy(self.api.data[ROOT + '/pulls/10/files'])
        self.api.data[ROOT + '/pulls/11/commits'] = copy.deepcopy(self.api.data[ROOT + '/pulls/10/commits'])
        del prs[0]['manifest']['issues'][0]['acceptance']['AC2']
        del second['manifest']['issues'][0]['acceptance']['AC1']
        for item in prs + [second]:
            self.api.data[f'{ROOT}/pulls/{item["pr"]}']['body'] = f'Refs https://github.com/{REPO}/issues/1\n' + packet(MANIFEST, item['manifest'])
        target = self.gate.finish_plan(prs + [second], {}, target_only=True)
        c = self.api.data[f'{ROOT}/issues/comments/{approvals["1"]}']
        value = json.loads(c['body'].split('```json\n')[1].split('\n```')[0]); value['target_digest'] = digest(target)
        c['body'] = packet(APPROVAL, value)
        self.gate.finish_plan(prs + [second], approvals)
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_failed_pending_skipped_spoofed_or_missing_ci(self):
        for mode in ('failure', 'pending', 'skipped', 'spoof', 'missing', 'wrong_sha', 'later_failure'):
            self.setUp(); prs, approvals = self.completion()
            checks = self.api.data[ROOT + '/commits/' + MERGE + '/check-runs']
            if mode in ('failure', 'skipped'): checks[0]['conclusion'] = mode
            if mode == 'pending': checks[0]['status'] = 'in_progress'
            if mode == 'spoof': checks[0]['app']['id'] = 456
            if mode == 'missing': checks.clear()
            if mode == 'wrong_sha': checks[0]['head_sha'] = HEAD
            if mode == 'later_failure': checks.append(dict(checks[0], id=2, conclusion='failure'))
            self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_deployment_wait_failure_inactive_unknown_wrong_sha(self):
        for mode in ('pending', 'failure', 'inactive', 'unknown', 'missing', 'wrong_sha', 'transient'):
            self.setUp(); prs, approvals = self.completion()
            if mode == 'missing': self.api.data[ROOT + '/deployments?sha=' + MERGE] = []
            elif mode == 'wrong_sha': self.api.data[ROOT + '/deployments?sha=' + MERGE][0]['sha'] = HEAD
            elif mode == 'transient': self.api.data[ROOT + '/deployments?sha=' + MERGE][0]['transient_environment'] = True
            else: self.api.data[ROOT + '/deployments/1/statuses'][0]['state'] = mode
            self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_merge_not_enough_and_unmerged_cannot_finish(self):
        prs, approvals = self.completion()
        self.api.data[ROOT + '/pulls/10']['merged_at'] = None
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_pending_stale_edited_wrong_author_approval(self):
        for mode in ('pending', 'stale', 'author', 'edited', 'missing', 'target'):
            self.setUp(); prs, approvals = self.completion()
            c = self.api.data[f'{ROOT}/issues/comments/{approvals["1"]}']
            data = json.loads(c['body'].split('```json\n')[1].split('\n```')[0])
            if mode == 'pending': data['approved'] = False
            if mode == 'stale': data['heads'] = [BASE]
            if mode == 'target': data['issue'] = 2
            c['body'] = packet(APPROVAL, data)
            if mode == 'author': c['user']['login'] = 'other'
            if mode == 'edited': c['updated_at'] = '2026-01-05T00:00:00Z'
            if mode == 'missing': approvals.clear()
            self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_partial_unknown_close_journal_and_no_retry(self):
        prs, approvals = self.completion((1, 2))
        self.api.fail_close = ROOT + '/issues/2'
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'journal.json'
            with self.assertRaises(GateError): self.gate.finish(prs, approvals, path, True)
            state = json.loads(path.read_text())
            self.assertEqual(state['status'], 'partial_or_unknown')
            self.assertEqual(state['events'][-1], {'issue': 2, 'operation': 'close', 'status': 'intent'})
            self.assertEqual(self.api.data[ROOT + '/issues/1']['state'], 'closed')
            self.assertEqual(self.api.data[ROOT + '/issues/2']['state'], 'open')
            writes = len(self.api.writes)
            with self.assertRaises((GateError, FileExistsError)): self.gate.finish(prs, approvals, path, True)
            self.assertEqual(len(self.api.writes), writes)

    def test_unknown_start_journal_prevents_retry(self):
        self.api.fail_comment = True
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'start.json'
            with self.assertRaises(GateError): self.gate.start([1], BASE, True, 'work', REPO, path, workflow_run_id=42)
            self.assertEqual(json.loads(path.read_text())['status'], 'start_intent')
            writes = len(self.api.writes)
            with self.assertRaises(FileExistsError): self.gate.start([1], BASE, True, 'work', REPO, path, workflow_run_id=42)
            self.assertEqual(len(self.api.writes), writes)

    def test_unknown_evidence_never_closes(self):
        prs, approvals = self.completion()
        self.api.fail_comment = True
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(GateError): self.gate.finish(prs, approvals, Path(d) / 'journal.json', True)
        self.assertEqual([x[1] for x in self.api.writes], ['POST'])


    def test_committed_work_before_start_rejected(self):
        self.api.data[ROOT + '/git/ref/heads/main']['object']['sha'] = HEAD
        self.blocked(lambda: self.gate.start([1], BASE, True, 'work', REPO, workflow_run_id=42))
        self.setUp(); m = self.started()
        self.api.data[ROOT + '/compare/' + BASE + '...' + HEAD]['commits'][0]['commit']['committer']['date'] = '2026-01-01T00:00:00Z'
        self.blocked(lambda: self.gate.pr_check(10, m))

    def test_omitted_related_open_or_merged_pr_blocks_close(self):
        prs, approvals = self.completion()
        self.api.data[ROOT + '/pulls/11'] = copy.deepcopy(self.api.data[ROOT + '/pulls/10'])
        self.api.data[ROOT + '/pulls/11/files'] = copy.deepcopy(self.api.data[ROOT + '/pulls/10/files'])
        self.api.data[ROOT + '/pulls/11/commits'] = copy.deepcopy(self.api.data[ROOT + '/pulls/10/commits'])
        self.api.data[ROOT + '/issues/1/timeline'] = [{'event': 'cross-referenced', 'created_at': '2026-01-03T00:00:00Z',
            'source': {'issue': {'html_url': f'https://github.com/{REPO}/pull/11', 'pull_request': {}}}}]
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))
        # Explicitly cancelled, unmerged related PR does not prevent completion.
        self.api.data[ROOT + '/pulls/11'].update(state='closed', merged_at=None)
        self.gate.finish_plan(prs, approvals)

    def test_final_deployment_sha_can_cover_multiple_merges(self):
        prs, approvals = self.completion()
        final = 'd' * 40
        prs[0]['deployment_sha'] = final
        self.api.data[ROOT + '/compare/' + MERGE + '...' + final] = {'status': 'ahead'}
        self.api.data[ROOT + '/deployments?sha=' + final] = [{'id': 1, 'sha': final, 'environment': 'production', 'creator': {'login': 'deploy-bot'}, 'payload': {'workflow_run_id': 77}}]
        self.api.data[ROOT+'/actions/runs/77']['head_sha']=final
        self.api.data[ROOT + '/commits/' + final + '/check-runs'] = [dict(self.api.data[ROOT + '/commits/' + MERGE + '/check-runs'][0], head_sha=final,id=2,check_suite={'id':11})]
        self.api.proofs[(77,'issue-deploy-receipts')]={'receipts':[{'deployment_id':1,'status_id':1,'sha':final,'environment':'production','state':'success'}]}
        target = self.gate.finish_plan(prs, {}, target_only=True)
        c = self.api.data[f'{ROOT}/issues/comments/{approvals["1"]}']; value = json.loads(c['body'].split('```json\n')[1].split('\n```')[0]); value['target_digest'] = digest(target); c['body'] = packet(APPROVAL, value)
        self.gate.finish_plan(prs, approvals)
        self.api.data[ROOT + '/compare/' + MERGE + '...' + final]['status'] = 'diverged'
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))


    def test_commit_autoclose_is_blocked(self):
        m = self.started()
        self.api.data[ROOT + '/compare/' + BASE + '...' + HEAD]['commits'][0]['commit']['message'] = 'Closes #1'
        self.blocked(lambda: self.gate.pr_check(10, m))

    def test_manifest_cannot_be_changed_only_at_finish(self):
        m = self.started()
        m['issues'][0]['acceptance']['AC1']['verification'] = 'invented after merge'
        self.blocked(lambda: self.gate.pr_check(10, m))


    def test_manual_closing_link_and_truncated_commits_blocked(self):
        m = self.started()
        self.api.closing = [{'number': 1}]
        self.blocked(lambda: self.gate.pr_check(10, m))
        self.api.closing = []
        self.api.data[ROOT + '/compare/' + BASE + '...' + HEAD]['total_commits'] = 251
        self.blocked(lambda: self.gate.pr_check(10, m))


    def test_review_manifest_and_deployment_target_changes_require_new_approval(self):
        prs, approvals = self.completion()
        prs[0]['manifest']['issues'][0]['acceptance']['AC1']['verification'] = 'changed together'
        self.api.data[ROOT+'/pulls/10']['body'] = f'Refs https://github.com/{REPO}/issues/1\n' + packet(MANIFEST, prs[0]['manifest'])
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_review_latest_refusal_invalidates_old_approval(self):
        prs, approvals = self.completion()
        old = self.api.data[f'{ROOT}/issues/comments/{approvals["1"]}']
        value = json.loads(old['body'].split('```json\n')[1].split('\n```')[0]); value['approved'] = False
        self.api.call(ROOT+'/issues/1/comments', 'POST', {'body': packet(APPROVAL, value)})
        self.api.writes.clear()
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_review_untrusted_start_issuer_and_workflow_rejected(self):
        m = self.started()
        c = self.api.data[f'{ROOT}/issues/comments/{m["issues"][0]["start_comment"]}']
        value = json.loads(c['body'].split('```json\n')[1].split('\n```')[0]); value['actor'] = 'owner'
        c['user']['login'] = 'owner'; c['body'] = packet(START, value)
        self.blocked(lambda: self.gate.pr_check(10, m))
        self.setUp(); m = self.started()
        self.api.data[ROOT+'/actions/runs/42']['head_branch'] = 'attacker-branch'
        self.blocked(lambda: self.gate.pr_check(10, m))

    def test_review_untrusted_deployment_and_status_issuer(self):
        for field in ('deployment', 'status'):
            self.setUp(); prs, approvals = self.completion()
            data = self.api.data[ROOT+'/deployments?sha='+MERGE] if field == 'deployment' else self.api.data[ROOT+'/deployments/1/statuses']
            data[0]['creator']['login'] = 'untrusted-writer'
            self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_review_failed_final_deployment_sha_tests_blocks_finish(self):
        prs, approvals = self.completion(); final = 'd'*40
        prs[0]['deployment_sha'] = final
        self.api.data[ROOT+'/compare/'+MERGE+'...'+final] = {'status':'ahead'}
        self.api.data[ROOT+'/commits/'+final+'/check-runs'] = [dict(self.api.data[ROOT+'/commits/'+MERGE+'/check-runs'][0],head_sha=final,conclusion='failure')]
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))

    def test_review_main_incorporation_old_commit_is_not_pr_implementation(self):
        m = self.started()
        old = {'commit': {'committer': {'date':'2026-01-01T00:00:00Z'}, 'message':'main-only change'}}
        comparison = self.api.data[ROOT+'/compare/'+BASE+'...'+HEAD]
        comparison['commits'] = list(comparison['commits']) + [old]; comparison['total_commits']=2
        self.gate.pr_check(10,m)


    def test_tool_only_release_does_not_require_app_rollout(self):
        self.gate.policy['no_runtime_paths'] = ['tools/issue-lifecycle/*']
        self.api.data[ROOT+'/pulls/10/files'] = [{'filename':'tools/issue-lifecycle/gate.py'}]
        self.api.data[ROOT+'/deployments?sha='+MERGE] = []
        prs, approvals = self.completion()
        self.gate.finish_plan(prs, approvals)
        # Renaming production code into the safe prefix still needs deployment.
        self.api.data[ROOT+'/pulls/10/files'][0]['previous_filename'] = 'src/application.py'
        self.blocked(lambda: self.gate.finish_plan(prs, approvals))


    def transition_fixture(self):
        self.gate.policy['transitional_prs'] = [10]
        criteria = {'AC1':'works','AC2':'tested'}
        issue,snapshot = self.gate.issue(1,criteria)
        value = {'repo':REPO,'pr':10,'issue':1,'head':HEAD,'run':'transition-10','kind':'retrospective',
                 'accepted':True,'reason':'Gate introduced after this work began; no prestart claim',
                 'body_digest':snapshot['body_digest'],'epoch':snapshot['epoch'],'criteria':criteria}
        c=self.api.call(ROOT+'/issues/1/comments','POST',{'body':packet(TRANSITION,value)})
        m={'repo':REPO,'head':HEAD,'run':'transition-10','mode':'transition','issues':[{'number':1,
           'transition_comment':c['id'],'acceptance':{ac:{'implementation':'code','verification':'test'} for ac in criteria}}]}
        self.api.data[ROOT+'/pulls/10']['body']=f'Refs https://github.com/{REPO}/issues/1\n'+packet(MANIFEST,m)
        self.api.writes.clear();return m,c,value

    def test_transition_is_explicit_retrospective_not_prestart(self):
        m,c,value=self.transition_fixture()
        self.api.data[ROOT+'/pulls/10']['created_at']='2026-01-01T01:00:00Z'
        result=self.gate.pr_check(10,m)
        self.assertIsNone(result['issues'][0]['start_comment'])
        self.assertEqual(result['issues'][0]['transition_comment'],c['id'])

    def test_transition_rejects_unlisted_pr_wrong_head_untrusted_author(self):
        for mode in ('pr','head','author','revoked','issue_changed'):
            self.setUp();m,c,value=self.transition_fixture()
            if mode=='pr':self.gate.policy['transitional_prs']=[]
            if mode=='head':value['head']=BASE;c['body']=packet(TRANSITION,value)
            if mode=='author':c['user']['login']='untrusted-writer';self.api.data[f'{ROOT}/issues/comments/{c["id"]}']['user']['login']='untrusted-writer'
            if mode=='revoked':
                value['accepted']=False;self.api.call(ROOT+'/issues/1/comments','POST',{'body':packet(TRANSITION,value)});self.api.writes.clear()
            if mode=='issue_changed':self.api.data[ROOT+'/issues/1']['body']+='\nnew request'
            if mode=='head':self.api.data[f'{ROOT}/issues/comments/{c["id"]}']['body']=c['body']
            self.blocked(lambda:self.gate.pr_check(10,m))


    def test_deployment_from_wrong_workflow_is_not_formal_evidence(self):
        prs, approvals=self.completion()
        self.api.data[ROOT+'/actions/runs/77']['path']='.github/workflows/untrusted.yml'
        self.blocked(lambda:self.gate.finish_plan(prs,approvals))

    def test_start_comment_copy_of_trusted_run_is_not_output_proof(self):
        m=self.started();self.api.proofs[(42,'issue-start-receipts')]['receipts']=[]
        self.blocked(lambda:self.gate.pr_check(10,m))

    def test_deployment_same_bot_and_run_but_not_actual_output_is_blocked(self):
        prs, approvals=self.completion();self.api.proofs[(77,'issue-deploy-receipts')]['receipts']=[]
        self.blocked(lambda:self.gate.finish_plan(prs,approvals))

    def test_same_app_name_sha_check_not_from_workflow_job_is_blocked(self):
        prs, approvals=self.completion()
        check=copy.deepcopy(self.api.data[ROOT+'/commits/'+MERGE+'/check-runs'][0]);check['id']=999
        self.api.data[ROOT+'/commits/'+MERGE+'/check-runs'].append(check)
        self.blocked(lambda:self.gate.finish_plan(prs,approvals))

if __name__ == '__main__': unittest.main()
