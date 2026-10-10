#!/usr/bin/env python3
"""Trusted workflow_dispatch entry point. Never execute PR branch/code."""
import json
import os
from pathlib import Path
from gate import Gate, GitHub, require, digest, START


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    inputs = event['inputs']
    policy = json.loads(Path(__file__).with_name('policy.json').read_text())
    require(os.environ['GITHUB_REPOSITORY'] == policy['repository'], 'workflow repository mismatch')
    require(os.environ['GITHUB_SHA'] == inputs['base_sha'], 'dispatch SHA differs from requested base')
    gate = Gate(GitHub(), policy)
    manifest = gate.start(json.loads(inputs['issues']), inputs['base_sha'], True, inputs['branch'], policy['repository'],
               actor='github-actions[bot]', workflow_run_id=int(os.environ['GITHUB_RUN_ID']), run=inputs['run'])
    records = []
    for entry in manifest['issues']:
        c, payload = gate.comment(entry['number'],entry['start_comment'],START)
        records.append({'issue':entry['number'],'comment_id':c['id'],'payload_digest':digest(payload)})
    Path('/tmp/receipts.json').write_text(json.dumps({'receipts':records},sort_keys=True))

if __name__ == '__main__':
    main()
