#!/usr/bin/env python3
"""Issue lifecycle checks. No dependencies, tokens, hooks or settings installed."""
import argparse
import datetime as dt
import hashlib
import fnmatch
import json
import re
import subprocess
import sys
import time
import uuid
import io
import zipfile
from pathlib import Path

START = 'issue-gate-start:v1'
APPROVAL = 'issue-gate-approval:v1'
FINISH = 'issue-gate-finish:v1'
MANIFEST = 'issue-gate-manifest:v1'
TRANSITION = 'issue-gate-transition:v1'
AUTO_CLOSE = re.compile(r'\b(close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s+(?:#|https://github\.com/|[\w.-]+/[\w.-]+#)', re.I)


def pr_manifest(body):
    matches = re.findall(r'<!-- ' + MANIFEST + r' -->\n```json\n(.*?)\n```', body, re.S)
    require(len(matches) == 1, 'exactly one PR manifest block required')
    return json.loads(matches[0])

class GateError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise GateError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def stamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'timestamp must have timezone')
    return result


def packet(kind, data):
    return '<!-- ' + kind + ' -->\n```json\n' + json.dumps(data, sort_keys=True) + '\n```'


def unpack(body, kind):
    match = re.fullmatch(r'<!-- ' + re.escape(kind) + r' -->\n```json\n(.*)\n```', body, re.S)
    require(match is not None, 'invalid ' + kind + ' packet')
    return json.loads(match[1])


class GitHub:
    """Use existing gh auth; no credential extraction, no implicit mutation retry."""
    def call(self, path, method='GET', data=None):
        args = ['gh', 'api', '--method', method, path]
        require(len(path) < 4096, 'API path too long')
        if data is not None:
            args += ['--input', '-']
        result = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=30)
        require(result.returncode == 0, 'GitHub request failed/unknown; reconcile before retry: ' + path)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def artifact_payload(self, run_id, name):
        artifacts = self.pages(f'repos/{self.repository}/actions/runs/{run_id}/artifacts')
        matches = [a for a in artifacts if a['name'] == name and not a['expired']]
        require(len(matches) == 1, 'trusted receipt artifact missing/expired/ambiguous')
        result = subprocess.run(['gh', 'api', f'repos/{self.repository}/actions/artifacts/{matches[0]["id"]}/zip'],
                                capture_output=True, timeout=30)
        require(result.returncode == 0 and len(result.stdout) <= 1048576, 'receipt artifact download failed/oversize')
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            require(archive.namelist() == ['receipts.json'] and archive.getinfo('receipts.json').file_size <= 1048576,
                    'invalid trusted receipt archive')
            return json.loads(archive.read('receipts.json'))

    def closing_issues(self, repo, pr):
        owner, name = repo.split('/')
        query = '''query($owner:String!,$name:String!,$pr:Int!) {
          repository(owner:$owner,name:$name) { pullRequest(number:$pr) {
            closingIssuesReferences(first:100) { totalCount nodes { number } }
          } }
        }'''
        response = self.call('graphql', 'POST', {'query': query, 'variables': {'owner': owner, 'name': name, 'pr': pr}})
        require(not response.get('errors'), 'closing references lookup failed')
        refs = response['data']['repository']['pullRequest']['closingIssuesReferences']
        require(refs['totalCount'] == len(refs['nodes']), 'closing references truncated')
        return refs['nodes']

    def pages(self, path):
        result = []
        for page in range(1, 101):
            separator = '&' if '?' in path else '?'
            value = self.call(f'{path}{separator}per_page=100&page={page}')
            # check-runs API wraps the array.
            if isinstance(value, dict):
                value = next((value[k] for k in ('check_runs','artifacts','workflow_runs','jobs') if k in value), None)
            require(isinstance(value, list), 'invalid paginated response')
            result.extend(value)
            if len(value) < 100:
                return result
        raise GateError('pagination bound reached; fail closed')


class Gate:
    def __init__(self, api, policy):
        self.api, self.policy = api, policy
        self.finished = set()
        self.repo = policy['repository']
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repo), 'invalid repository')
        require(policy['base_branch'] and policy['approvers'], 'base/approvers missing')
        require(policy['start_issuers'] and policy['start_workflow'], 'trusted start issuer/workflow missing')
        require(policy['deployment_issuers'] and policy['deployment_workflows'], 'trusted deployment issuer/workflow missing')
        require(isinstance(policy['deployment_environments'], list), 'deployment policy missing')
        require(policy['required_checks'], 'required checks must not be empty')
        for check in policy['required_checks'] + policy.get('tool_checks', []):
            require(check['name'] and type(check['app_id']) is int and check['app_id'] > 0 and check.get('workflow'), 'check name/app/workflow binding missing')
        self.root = 'repos/' + self.repo
        self.api.repository = self.repo

    def issue(self, number, contract=None):
        require(type(number) is int and number > 0, 'invalid issue number')
        issue = self.api.call(f'{self.root}/issues/{number}')
        require('pull_request' not in issue, 'PR cannot be an Issue')
        require(issue['state'] == 'open' or (number in self.finished and issue['state'] == 'closed'
                and issue.get('state_reason') == 'completed'), 'Issue must be open')
        require(issue['html_url'] == f'https://github.com/{self.repo}/issues/{number}', 'wrong repository')
        body = issue.get('body') or ''
        if contract is None:
            require(re.search(r'^## (要求|Requirement)\s*\n\S', body, re.M), 'requirement section missing')
            criteria = re.findall(r'^- (AC[1-9][0-9]*):\s*(\S.*)$', body, re.M)
        else:
            require(body.strip() and isinstance(contract, dict) and contract, 'transition needs existing request and explicit AC contract')
            require(all(re.fullmatch('AC[1-9][0-9]*', k) and isinstance(v, str) and v.strip() for k,v in contract.items()), 'invalid transitional AC')
            criteria = list(contract.items())
        require(criteria and len(dict(criteria)) == len(criteria), 'unique acceptance criteria missing')
        events = self.api.pages(f'{self.root}/issues/{number}/events')
        reopened = [x for x in events if x['event'] == 'reopened']
        epoch = max(reopened, key=lambda x: x['id']) if reopened else None
        return issue, {'body_digest': digest(body), 'criteria': dict(criteria),
                       'epoch': epoch['id'] if epoch else 0,
                       'epoch_at': epoch['created_at'] if epoch else issue['created_at']}

    def comment(self, number, comment_id, kind):
        require(type(comment_id) is int and comment_id > 0, 'invalid comment id')
        c = self.api.call(f'{self.root}/issues/comments/{comment_id}')
        require(c['issue_url'] == f'https://api.github.com/{self.root}/issues/{number}', 'comment belongs to other Issue')
        require(c['created_at'] == c['updated_at'], 'edited receipt/approval rejected')
        return c, unpack(c['body'], kind)

    def writer(self, login):
        permission = self.api.call(f'{self.root}/collaborators/{login}/permission')['permission']
        require(permission in ('admin', 'maintain', 'write'), 'start author lacks repository write permission')

    def request_start(self, numbers, base_sha, clean, branch, remote, journal_path, timeout=120):
        require(clean and branch and branch != self.policy['base_branch'], 'working tree must be clean before start on a work branch')
        require(remote == self.repo, 'origin does not match policy repository')
        require(re.fullmatch('[0-9a-f]{40}', base_sha), 'invalid base SHA')
        require(numbers and len(numbers) == len(set(numbers)), 'duplicate/empty Issues')
        current = self.api.call(f'{self.root}/git/ref/heads/{self.policy["base_branch"]}')
        require(current['object']['sha'] == base_sha, 'local HEAD must match latest server base before start')
        for n in numbers:
            self.issue(n)
        run = uuid.uuid4().hex
        journal = Path(journal_path)
        with journal.open('x') as f:
            json.dump({'status': 'dispatch_intent', 'run': run, 'issues': numbers, 'repo': self.repo, 'base_sha': base_sha}, f)
        self.api.call(f'{self.root}/actions/workflows/{self.policy["start_workflow"].split("/")[-1]}/dispatches', 'POST',
                      {'ref': self.policy['base_branch'], 'inputs': {'issues': json.dumps(numbers), 'base_sha': base_sha,
                                                                  'branch': branch, 'run': run}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            receipts = []
            for n in numbers:
                matches = []
                for c in self.api.pages(f'{self.root}/issues/{n}/comments'):
                    if c['user']['login'] not in self.policy['start_issuers'] or not c['body'].startswith('<!-- ' + START + ' -->'):
                        continue
                    payload = unpack(c['body'], START)
                    if payload.get('run') == run:
                        matches.append(c)
                require(len(matches) <= 1, 'duplicate start receipt; reconcile')
                if not matches:
                    break
                c, receipt = self.comment(n, matches[0]['id'], START)
                require(receipt['base_sha'] == base_sha and receipt['policy_digest'] == digest(self.policy)
                        and receipt['repo'] == self.repo and receipt['issue'] == n, 'start broker receipt mismatch')
                workflow = self.api.call(f'{self.root}/actions/runs/{receipt["workflow_run_id"]}')
                require(workflow['head_sha'] == base_sha and workflow['path'] == self.policy['start_workflow']
                        and workflow['event'] == 'workflow_dispatch' and workflow['head_branch'] == self.policy['base_branch'], 'start broker provenance mismatch')
                if workflow['status'] != 'completed':
                    break
                require(workflow['conclusion'] == 'success', 'start broker failed')
                proof = self.api.artifact_payload(receipt['workflow_run_id'], 'issue-start-receipts')
                require({'issue': n, 'comment_id': c['id'], 'payload_digest': digest(receipt)} in proof['receipts'], 'start receipt not emitted by trusted workflow')
                receipts.append({'number': n, 'start_comment': c['id'], 'acceptance': {}})
            if len(receipts) == len(numbers):
                result = {'repo': self.repo, 'run': run, 'head': None, 'issues': receipts}
                journal.write_text(json.dumps({'status': 'started', 'manifest': result}, indent=2))
                return result
            time.sleep(1)
        raise GateError('start broker timeout/unknown; inspect dispatch journal and workflow before retry')

    def start(self, numbers, base_sha, clean, branch, remote, journal_path=None, *, actor=None, workflow_run_id=None, run=None):
        require(clean, 'working tree must be clean before start (including untracked files)')
        require(branch and branch != self.policy['base_branch'], 'use a separate work branch')
        require(remote == self.repo, 'origin does not match policy repository')
        require(re.fullmatch('[0-9a-f]{40}', base_sha), 'invalid base SHA')
        require(numbers and len(numbers) == len(set(numbers)), 'duplicate/empty Issues')
        repo = self.api.call(self.root)
        require(repo['full_name'] == self.repo and not repo['archived'], 'wrong/archived repository')
        self.api.call(f'{self.root}/commits/{base_sha}')  # baseline must exist on server
        branch_ref = self.api.call(f'{self.root}/git/ref/heads/{self.policy["base_branch"]}')
        require(branch_ref['object']['sha'] == base_sha, 'start must be at current server base branch SHA; pre-start implementation cannot be adopted')
        actor = actor or self.api.call('user')['login']
        require(actor in self.policy['start_issuers'], 'only trusted start issuer may post receipt; use start broker')
        require(type(workflow_run_id) is int and workflow_run_id > 0, 'trusted workflow run required')
        workflow = self.api.call(f'{self.root}/actions/runs/{workflow_run_id}')
        require(workflow['head_sha'] == base_sha and workflow['event'] == 'workflow_dispatch' and workflow['head_branch'] == self.policy['base_branch']
                and workflow['path'] == self.policy['start_workflow'], 'untrusted start workflow provenance')
        # Validate all first; no first mutation on a later invalid Issue.
        snapshots = [(n, *self.issue(n)) for n in numbers]
        run = run or uuid.uuid4().hex
        require(re.fullmatch('[0-9a-f]{32}', run), 'invalid start run identity')
        receipts = []
        if journal_path:
            with Path(journal_path).open('x') as f:
                json.dump({'status': 'start_intent', 'run': run, 'repo': self.repo, 'issues': numbers}, f)
        for n, issue, snapshot in snapshots:
            payload = dict(snapshot, run=run, repo=self.repo, issue=n, base_sha=base_sha,
                           policy_digest=digest(self.policy), actor=actor, workflow_run_id=workflow_run_id)
            posted = self.api.call(f'{self.root}/issues/{n}/comments', 'POST', {'body': packet(START, payload)})
            c, actual = self.comment(n, posted['id'], START)
            require(actual == payload and c['user']['login'] == actor, 'start receipt mismatch')
            require(stamp(c['created_at']) >= stamp(snapshot['epoch_at']), 'start precedes Issue epoch')
            # issue may change while posting.
            require(self.issue(n)[1] == snapshot, 'Issue changed while starting; discard receipt')
            receipts.append({'number': n, 'start_comment': c['id'], 'acceptance': {}})
            if journal_path:
                Path(journal_path).write_text(json.dumps({'status': 'start_pending', 'run': run,
                    'repo': self.repo, 'issues': numbers, 'verified': receipts}, indent=2))
        result = {'repo': self.repo, 'run': run, 'head': None, 'issues': receipts}
        if journal_path:
            Path(journal_path).write_text(json.dumps({'status': 'started', 'manifest': result}, indent=2))
        return result

    def pr_check(self, number, manifest):
        require(type(number) is int and number > 0, 'invalid PR number')
        pr = self.api.call(f'{self.root}/pulls/{number}')
        require(manifest == pr_manifest(pr.get('body') or ''), 'manifest must exactly match PR body')
        require(manifest['repo'] == self.repo, 'manifest repository mismatch')
        require(pr['base']['repo']['full_name'] == self.repo, 'PR repository mismatch')
        require(pr['base']['ref'] == self.policy['base_branch'], 'PR base branch mismatch')
        require(manifest['head'] == pr['head']['sha'], 'stale manifest/head SHA')
        require(pr['state'] == 'open' or pr.get('merged_at'), 'unmerged closed PR')
        entries = manifest['issues']
        require(entries and len({x['number'] for x in entries}) == len(entries), 'duplicate/empty Issues')
        body = pr.get('body') or ''
        require(not self.api.closing_issues(self.repo, number), 'GitHub closing Issue links forbidden; remove automatic-close linkage')
        # Blanket rejection avoids accidental auto-close via alternate syntax/URL/cross-repo refs.
        require(not AUTO_CLOSE.search(body),
                'auto-close keyword forbidden; use Refs and finish after release')
        validated = []
        for entry in entries:
            n = entry['number']
            if manifest.get('mode') == 'transition':
                require(number in self.policy.get('transitional_prs', []), 'PR is not grandfathered by trusted policy')
                c, receipt = self.comment(n, entry['transition_comment'], TRANSITION)
                require(c['user']['login'] in self.policy['approvers'] and receipt.get('accepted') is True
                        and receipt.get('kind') == 'retrospective' and receipt.get('reason'), 'transition must be explicitly approved as retrospective')
                require(receipt['repo'] == self.repo and receipt['pr'] == number and receipt['issue'] == n
                        and receipt['head'] == manifest['head'] and receipt['run'] == manifest['run'], 'transition exact target mismatch')
                _, snapshot = self.issue(n, receipt['criteria'])
                require(receipt['body_digest'] == snapshot['body_digest'] and receipt['epoch'] == snapshot['epoch'], 'transitional Issue changed/reopened')
                decisions = []
                for item in self.api.pages(f'{self.root}/issues/{n}/comments'):
                    if item['user']['login'] in self.policy['approvers'] and item['body'].startswith('<!-- '+TRANSITION+' -->'):
                        value = unpack(item['body'], TRANSITION)
                        if value.get('repo') == self.repo and value.get('pr') == number:
                            decisions.append(item)
                require(decisions and max(decisions,key=lambda x:x['id'])['id'] == c['id'], 'transition superseded/revoked')
            else:
                _, snapshot = self.issue(n)
                c, receipt = self.comment(n, entry['start_comment'], START)
                require(receipt['repo'] == self.repo and receipt['issue'] == n, 'start target mismatch')
                require(receipt['run'] == manifest['run'], 'run mismatch')
                require(receipt['actor'] == c['user']['login'], 'start author mismatch')
                require(receipt['actor'] in self.policy['start_issuers'], 'untrusted start issuer')
                workflow = self.api.call(f'{self.root}/actions/runs/{receipt["workflow_run_id"]}')
                require(workflow['head_sha'] == receipt['base_sha'] and workflow['event'] == 'workflow_dispatch' and workflow['head_branch'] == self.policy['base_branch']
                        and workflow['path'] == self.policy['start_workflow']
                        and workflow['status'] == 'completed' and workflow['conclusion'] == 'success',
                        'start workflow not verified successful')
                proof = self.api.artifact_payload(receipt['workflow_run_id'], 'issue-start-receipts')
                require({'issue': n, 'comment_id': c['id'], 'payload_digest': digest(receipt)} in proof['receipts'], 'start receipt not emitted by trusted workflow')
                require(receipt['policy_digest'] == digest(self.policy), 'policy changed; new start required')
                require(all(receipt[k] == snapshot[k] for k in snapshot), 'Issue changed/reopened; new start required')
                require(stamp(c['created_at']) >= stamp(snapshot['epoch_at']), 'receipt before current epoch')
                require(stamp(c['created_at']) <= stamp(pr['created_at']), 'start receipt is after PR creation')
            if manifest.get('mode') != 'transition':
                comparison = self.api.call(f'{self.root}/compare/{receipt["base_sha"]}...{manifest["head"]}')
                require(comparison['status'] == 'ahead', 'start base is not ancestor of changed PR head')
                require(comparison.get('commits') and comparison.get('total_commits') == len(comparison['commits']),
                        'PR commit evidence missing/truncated (split large work)')
            pr_commits = self.api.pages(f'{self.root}/pulls/{number}/commits')
            require(pr['commits'] == len(pr_commits) and pr_commits, 'PR commit list missing/truncated')
            require(not any(AUTO_CLOSE.search(commit['commit'].get('message', '')) for commit in pr_commits),
                    'auto-close keyword in PR commit forbidden')
            require(manifest.get('mode') == 'transition' or all(stamp(commit['commit']['committer']['date']) >= stamp(c['created_at'])
                        for commit in pr_commits), 'PR commit predates start; classify as retrospective work')
            require(f'https://github.com/{self.repo}/issues/{n}' in body, 'explicit Issue URL missing from PR body')
            acceptance = entry['acceptance']
            require(acceptance and set(acceptance) <= set(snapshot['criteria']), 'unknown/empty AC mapping')
            require(all(isinstance(v, dict) and isinstance(v.get('implementation'), str)
                        and v['implementation'].strip() and isinstance(v.get('verification'), str)
                        and v['verification'].strip() for v in acceptance.values()), 'AC implementation/verification missing')
            validated.append({'number': n, 'criteria': snapshot['criteria'], 'acceptance': acceptance,
                              'run': receipt['run'], 'start_comment': c['id'] if manifest.get('mode') != 'transition' else None,
                              'transition_comment': c['id'] if manifest.get('mode') == 'transition' else None})
        files = self.api.pages(f'{self.root}/pulls/{number}/files')
        require(len(files) == pr['changed_files'], 'PR file list truncated')
        paths = sorted({path for f in files for path in (f['filename'], f.get('previous_filename')) if path})
        no_runtime = bool(paths) and all(any(fnmatch.fnmatchcase(path, rule) for rule in self.policy.get('no_runtime_paths', [])) for path in paths)
        return {'pr': number, 'paths': paths, 'requires_deployment': not no_runtime, 'head': pr['head']['sha'], 'merge_sha': pr.get('merge_commit_sha') if pr.get('merged_at') else None,
                'issues': validated}

    def checks(self, sha, specs=None):
        require(sha and re.fullmatch('[0-9a-f]{40}', sha), 'merged SHA required')
        checks = self.api.pages(f'{self.root}/commits/{sha}/check-runs')
        evidence = {'check_sha': sha, 'checks': [], 'deployments': []}
        for spec in (specs or self.policy['required_checks']):
            matching = [c for c in checks if c['name'] == spec['name'] and c['app']['id'] == spec['app_id']]
            require(matching, 'required check missing on release SHA: ' + spec['name'])
            latest = max(matching, key=lambda x: x['id'])
            require(latest['head_sha'] == sha and latest['status'] == 'completed' and latest['conclusion'] == 'success',
                    'required check not successful: ' + spec['name'])
            suite_id = latest['check_suite']['id']
            runs = self.api.pages(f'{self.root}/actions/runs?check_suite_id={suite_id}')
            matches = [r for r in runs if r['path'] == spec['workflow'] and r['head_sha'] == sha
                       and r['head_branch'] == self.policy['base_branch'] and r['event'] in ('push','workflow_dispatch')
                       and r['status'] == 'completed' and r['conclusion'] == 'success']
            require(matches, 'required check has no trusted release workflow run')
            verified_run = None
            for run in matches:
                jobs = self.api.pages(f'{self.root}/actions/runs/{run["id"]}/jobs')
                if any(job['check_run_url'] == f'https://api.github.com/{self.root}/check-runs/{latest["id"]}' for job in jobs):
                    verified_run = run['id']; break
            require(verified_run is not None, 'check was not emitted by required workflow job')
            evidence['checks'].append({'workflow_run_id': verified_run, 'id': latest['id'], 'name': latest['name'], 'app_id': latest['app']['id'],
                                       'conclusion': latest['conclusion'], 'url': latest.get('html_url')})
        return evidence

    def release(self, sha, deployment_sha=None, *, requires_deployment=True):
        specs = self.policy.get('tool_checks', self.policy['required_checks']) if not requires_deployment else self.policy['required_checks']
        evidence = self.checks(sha, specs)
        deployment_sha = deployment_sha or sha
        require(re.fullmatch('[0-9a-f]{40}', deployment_sha), 'invalid deployment SHA')
        if deployment_sha != sha:
            comparison = self.api.call(f'{self.root}/compare/{sha}...{deployment_sha}')
            require(comparison['status'] == 'ahead', 'deployment does not contain PR merge')
            evidence['deployment_checks'] = self.checks(deployment_sha, specs)
        for environment in (self.policy['deployment_environments'] if requires_deployment else []):
            deployments = self.api.pages(f'{self.root}/deployments?sha={deployment_sha}')
            matching = [d for d in deployments if d['sha'] == deployment_sha and d['environment'] == environment
                        and not d.get('transient_environment', False)]
            require(matching, 'deployment missing: ' + environment)
            deployment = max(matching, key=lambda x: x['id'])
            require(deployment['creator']['login'] in self.policy['deployment_issuers'], 'untrusted deployment issuer')
            payload = deployment.get('payload') or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            workflow = self.api.call(f'{self.root}/actions/runs/{payload["workflow_run_id"]}')
            require(workflow['path'] in self.policy['deployment_workflows'] and workflow['head_branch'] == self.policy['base_branch']
                    and workflow['head_sha'] == deployment_sha and workflow['event'] in ('push', 'workflow_dispatch')
                    and workflow['status'] == 'completed' and workflow['conclusion'] == 'success', 'deployment verifier workflow not trusted/successful')
            statuses = self.api.pages(f'{self.root}/deployments/{deployment['id']}/statuses')
            require(statuses, 'deployment statuses missing')
            latest = max(statuses, key=lambda x: x['id'])
            require(latest['creator']['login'] in self.policy['deployment_issuers'], 'untrusted deployment status issuer')
            require(latest['state'] == 'success', 'deployment pending/failed: ' + environment)
            record = {'deployment_id': deployment['id'], 'status_id': latest['id'], 'sha': deployment_sha,
                      'environment': environment, 'state': latest['state']}
            proof = self.api.artifact_payload(payload['workflow_run_id'], 'issue-deploy-receipts')
            require(record in proof['receipts'], 'deployment/status not emitted by trusted verifier')
            evidence['deployments'].append({'deployment_id': deployment['id'], 'status_id': latest['id'],
                'sha': deployment_sha, 'environment': environment, 'state': latest['state'],
                'workflow_run_id': payload['workflow_run_id'], 'issuer': deployment['creator']['login'], 'status_issuer': latest['creator']['login'],
                'url': latest.get('environment_url')})
        return evidence

    def finish_plan(self, manifests, approvals, *, target_only=False):
        require(manifests and len({x['pr'] for x in manifests}) == len(manifests), 'duplicate/empty PRs')
        checked = [self.pr_check(x['pr'], x['manifest']) for x in manifests]
        for pr, source in zip(checked, manifests):
            pr['deployment_sha'] = source.get('deployment_sha') or pr['merge_sha']
            pr['release_evidence'] = self.release(pr['merge_sha'], pr['deployment_sha'], requires_deployment=pr['requires_deployment'])
        grouped = {}
        for pr in checked:
            for entry in pr['issues']:
                group = grouped.setdefault(entry['number'], {'criteria': set(entry['criteria']), 'covered': set(),
                                                            'runs': set(), 'heads': set(), 'starts': set(), 'prs': set(), 'transitions': set()})
                group['covered'].update(entry['acceptance'])
                group['runs'].add(entry['run'])
                group['heads'].add(pr['head'])
                if entry['start_comment'] is not None:
                    group['starts'].add(entry['start_comment'])
                if entry['transition_comment'] is not None:
                    group['transitions'].add(entry['transition_comment'])
                group['prs'].add(pr['pr'])
        target = {'repo': self.repo, 'policy_digest': digest(self.policy), 'prs': checked, 'issues': sorted(grouped)}
        if target_only:
            return target
        require(set(approvals) == {str(n) for n in grouped}, 'approval set must exactly match Issue set')
        for n, group in grouped.items():
            require(group['covered'] == group['criteria'], 'not all ACs covered across PRs')
            starts = [self.comment(n, cid, START)[0] for cid in group['starts']]
            starts.extend(self.comment(n,cid,TRANSITION)[0] for cid in group['transitions'])
            started_at = min(stamp(c['created_at']) for c in starts)
            if group['transitions']:
                started_at = min(started_at, *(stamp(self.api.call(f'{self.root}/pulls/{number}')['created_at']) for number in group['prs']))
            for event in self.api.pages(f'{self.root}/issues/{n}/timeline'):
                if event['event'] != 'cross-referenced' or stamp(event['created_at']) < started_at:
                    continue
                linked = event.get('source', {}).get('issue', {})
                if 'pull_request' not in linked:
                    continue
                url = linked['html_url']
                match = re.fullmatch(r'https://github.com/' + re.escape(self.repo) + r'/pull/([0-9]+)', url)
                require(match is not None, 'cross-repository PR requires separate coordinated finish')
                linked_pr = self.api.call(f'{self.root}/pulls/{int(match[1])}')
                if linked_pr['state'] == 'open' or linked_pr.get('merged_at'):
                    require(int(match[1]) in group['prs'], 'related PR omitted; cannot close Issue')
            decisions = []
            for comment in self.api.pages(f'{self.root}/issues/{n}/comments'):
                if comment['user']['login'] not in self.policy['approvers'] or not comment['body'].startswith('<!-- ' + APPROVAL + ' -->'):
                    continue
                decision = unpack(comment['body'], APPROVAL)
                if decision.get('repo') == self.repo and decision.get('issue') == n and set(decision.get('runs', [])) & group['runs']:
                    decisions.append(comment)
            require(decisions and max(decisions, key=lambda x: x['id'])['id'] == approvals[str(n)],
                    'approval is not latest formal decision (revoked/superseded)')
            c, approval = self.comment(n, approvals[str(n)], APPROVAL)
            require(c['user']['login'] in self.policy['approvers'], 'approval author not authorized')
            require(approval.get('approved') is True and approval.get('issue') == n and approval.get('repo') == self.repo,
                    'approval pending/rejected/wrong target')
            require(approval.get('target_digest') == digest(target), 'approval target changed; reapproval required')
            require(set(approval['runs']) == group['runs'] and set(approval['heads']) == group['heads'], 'stale approval')
        return dict(target, approvals=approvals)

    def finish(self, manifests, approvals, journal_path, apply=False):
        self.finished = set()
        plan = self.finish_plan(manifests, approvals)
        if not apply:
            return {'status': 'ready', 'dry_run': True, 'plan': plan}
        # Exclusive creation prevents blind reapply after any partial/unknown mutation.
        journal = Path(journal_path)
        with journal.open('x', encoding='utf-8') as f:
            json.dump({'status': 'pending', 'plan_digest': digest(plan), 'plan': plan, 'events': []}, f)
        state = json.loads(journal.read_text())
        def record(event):
            state['events'].append(event)
            journal.write_text(json.dumps(state, indent=2), encoding='utf-8')
        try:
            for n in plan['issues']:
                # Revalidate entire plan before each Issue's mutation.
                require(self.finish_plan(manifests, approvals) == plan, 'finish inputs changed')
                evidence = {'repo': self.repo, 'issue': n, 'plan_digest': digest(plan), 'plan': plan}
                record({'issue': n, 'operation': 'comment', 'status': 'intent'})
                c = self.api.call(f'{self.root}/issues/{n}/comments', 'POST', {'body': packet(FINISH, evidence)})
                actual, payload = self.comment(n, c['id'], FINISH)
                require(payload == evidence, 'finish evidence readback mismatch')
                record({'issue': n, 'operation': 'comment', 'status': 'verified', 'comment': actual['id']})
                # Release and approval may have changed during comment write.
                require(self.finish_plan(manifests, approvals) == plan, 'finish inputs changed after evidence')
                record({'issue': n, 'operation': 'close', 'status': 'intent'})
                self.api.call(f'{self.root}/issues/{n}', 'PATCH', {'state': 'closed', 'state_reason': 'completed'})
                readback = self.api.call(f'{self.root}/issues/{n}')
                require(readback['state'] == 'closed' and readback['state_reason'] == 'completed', 'close readback failed')
                record({'issue': n, 'operation': 'close', 'status': 'verified'})
                self.finished.add(n)
            for n in plan['issues']:
                latest = self.api.call(f'{self.root}/issues/{n}')
                require(latest['state'] == 'closed' and latest['state_reason'] == 'completed', 'Issue reopened during finish')
            state['status'] = 'completed'
        except Exception:
            state['status'] = 'partial_or_unknown'
            raise
        finally:
            journal.write_text(json.dumps(state, indent=2), encoding='utf-8')
        return state


def git(*args):
    result = subprocess.run(['git', *args], text=True, capture_output=True)
    require(result.returncode == 0, 'git operation failed')
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True, help='trusted repository policy JSON')
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('--issue', type=int, action='append', required=True)
    start.add_argument('--journal', required=True, help='new mutation journal outside worktree')
    start.add_argument('--output', required=True, help='new manifest outside worktree')
    check = commands.add_parser('pr-check')
    check.add_argument('--pr', type=int, required=True)
    check.add_argument('--manifest', help='optional exact copy of PR manifest; otherwise read PR body')
    finish = commands.add_parser('finish')
    finish.add_argument('--input', required=True, help='JSON {prs:[{pr,manifest}], approvals:{issue:comment_id}}')
    finish.add_argument('--journal', required=True)
    finish.add_argument('--target', action='store_true', help='read-only exact approval target and digest')
    finish.add_argument('--apply', action='store_true', help='post evidence and close after all checks (default dry-run)')
    args = parser.parse_args()
    policy = json.loads(Path(args.policy).read_text())
    gate = Gate(GitHub(), policy)
    if args.command == 'start':
        require(Path(args.output).resolve() != Path(args.journal).resolve(), 'manifest and journal must be separate')
        require(not Path(args.output).exists(), 'output exists; do not overwrite start')
        remote = git('remote', 'get-url', 'origin')
        match = re.fullmatch(r'(?:https://github.com/|git@github.com:)([^/]+/[^/]+?)(?:\.git)?', remote)
        require(match is not None, 'only exact github.com origin supported')
        result = gate.request_start(args.issue, git('rev-parse', 'HEAD'), not git('status', '--porcelain', '--untracked-files=all'),
                            git('branch', '--show-current'), match[1], args.journal)
        with Path(args.output).open('x') as f:
            json.dump(result, f, indent=2)
    elif args.command == 'pr-check':
        manifest = json.loads(Path(args.manifest).read_text()) if args.manifest else pr_manifest(
            gate.api.call(f'{gate.root}/pulls/{args.pr}').get('body') or '')
        result = gate.pr_check(args.pr, manifest)
    else:
        payload = json.loads(Path(args.input).read_text())
        require(not (args.target and args.apply), 'target preview cannot apply')
        if args.target:
            target = gate.finish_plan(payload['prs'], {}, target_only=True)
            result = {'target': target, 'target_digest': digest(target)}
        else:
            result = gate.finish(payload['prs'], payload['approvals'], args.journal, args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try:
        main()
    except (GateError, KeyError, ValueError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
