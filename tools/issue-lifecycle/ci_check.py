#!/usr/bin/env python3
"""Publish trusted gate result on exact PR head, not pull_request_target base SHA."""
import datetime as dt
import json
import os
from pathlib import Path
from gate import Gate, GitHub, pr_manifest, require


def run(api, policy, number):
    gate = Gate(api, policy)
    pr = api.call(f'{gate.root}/pulls/{number}')
    head = pr['head']['sha']
    check = api.call(f'{gate.root}/check-runs', 'POST', {'name': 'issue-lifecycle', 'head_sha': head,
                    'status': 'in_progress', 'started_at': dt.datetime.now(dt.timezone.utc).isoformat()})
    error = None
    try:
        all_open = api.pages(f'{gate.root}/pulls?state=open')
        same_head = [p['number'] for p in all_open if p['head']['sha'] == head]
        require(number in same_head, 'PR no longer open')
        snapshots = {}
        failures = []
        for target in same_head:
            current = api.call(f'{gate.root}/pulls/{target}')
            snapshots[target] = current
            try:
                gate.pr_check(target, pr_manifest(current.get('body') or ''))
            except Exception as exc:
                failures.append(f'PR {target}: {exc}')
        for target, before in snapshots.items():
            latest = api.call(f'{gate.root}/pulls/{target}')
            require(latest['head']['sha'] == head and latest.get('body') == before.get('body'), 'PR changed during check')
        require(not failures, '; '.join(failures))
        conclusion = 'success'
    except Exception as exc:
        conclusion, error = 'failure', str(exc)
    api.call(f'{gate.root}/check-runs/{check["id"]}', 'PATCH', {'status': 'completed', 'conclusion': conclusion,
             'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
             'output': {'title': 'Issue lifecycle', 'summary': 'Verified Issue start and AC mapping' if error is None else 'BLOCKED: ' + error[:1000]}})
    if error:
        raise RuntimeError(error)
    return {'head': head, 'check_id': check['id'], 'conclusion': conclusion}


def recheck_open(api, policy):
    # Recheck every open head: queued events may replace one another, and
    # one Issue may affect multiple PRs. Publish all heads before failing job.
    numbers = [p['number'] for p in api.pages('repos/' + policy['repository'] + '/pulls?state=open')]
    results, failures, seen = [], [], set()
    for number in numbers:
        head = api.call('repos/' + policy['repository'] + '/pulls/' + str(number))['head']['sha']
        if head in seen:
            continue
        seen.add(head)
        try:
            results.append(run(api, policy, number))
        except Exception as exc:
            failures.append(f'PR {number}: {exc}')
    return {'results':results,'failures':failures}


def main():
    policy = json.loads(Path(__file__).with_name('policy.json').read_text())
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    require(os.environ['GITHUB_REPOSITORY'] == policy['repository'], 'repository mismatch')
    api = GitHub()
    result = recheck_open(api, policy)
    print(json.dumps(result))
    require(not result['failures'], '; '.join(result['failures']))

if __name__ == '__main__':
    main()
