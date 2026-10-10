# Development Issue lifecycle

Use an open Issue in this repository before changing code or development configuration. Describe the requirement under `## 要求` or `## Requirement`, and give stable acceptance IDs such as `- AC1: observable result`.

From a clean, separate work branch at the latest server base SHA:

```bash
python3 tools/issue-lifecycle/gate.py --policy tools/issue-lifecycle/policy.json start \
  --issue 123 --output /tmp/start-manifest.json --journal /tmp/start-journal.json
```

The local command checks the worktree and requests a trusted `issue-start.yml` workflow on the base branch. The workflow independently verifies the base SHA, Issue and acceptance contract, then posts a GitHub-timestamped start receipt. Begin editing only after the command returns successfully. The receipt's comment ID and payload digest must appear in the trusted workflow's `issue-start-receipts` artifact. A copied workflow run ID or a normal writer's manually authored comment is rejected. Artifacts expire after 90 days; expired proof blocks the operation and requires an honest renewed/transitioned contract. The workflow uses the existing Actions token; no new token, system service or local file watcher is needed.

The start result is a manifest template. Fill `head` with the current commit SHA and give each acceptance ID an `implementation` and `verification` explanation. Include complete Issue URLs and exactly one manifest block in the PR body:

````markdown
Refs https://github.com/example/project/issues/123
<!-- issue-gate-manifest:v1 -->
```json
{"repo":"example/project","run":"START_RUN","head":"CURRENT_40_HEX_SHA","issues":[{"number":123,"start_comment":456,"acceptance":{"AC1":{"implementation":"changed behavior and file","verification":"test name and result artifact"}}}]}
```
````

Run `pr-check --pr NUMBER` with the same policy before publishing a PR update. The trusted CI validator runs base code, parses PR data, and publishes `issue-lifecycle` directly on the checked PR head. It rejects closing keywords in descriptions/commits and GitHub automatic-closing links. Use references rather than `Closes`, `Fixes` or `Resolves`. Issue edits/reopens/closes recheck every open PR head. Multiple PRs sharing one SHA must all pass before that SHA receives success.

After all related PRs merge, prepare `finish.json` with `prs:[{pr,manifest,deployment_sha}]` and an `approvals` map from Issue number to final approval comment ID. Multiple PRs may cover different acceptance IDs; their union must cover the full contract. Exact manifest copies must match PR bodies.

```bash
python3 tools/issue-lifecycle/gate.py --policy tools/issue-lifecycle/policy.json finish \
  --input /tmp/finish.json --journal /tmp/finish-journal.json --target
```

`--target` returns the exact approval target and digest after read-only checks. The approver posts a formal decision on each Issue, binding repository, Issue, run set, head set and the complete target digest:

````markdown
<!-- issue-gate-approval:v1 -->
```json
{"repo":"example/project","issue":123,"approved":true,"runs":["START_RUN"],"heads":["APPROVED_HEAD_SHA"],"target_digest":"TARGET_DIGEST"}
```
````

A newer `approved:false` decision on the same run revokes approval. Edited, unauthorized, stale or superseded decisions are rejected. Changing acceptance explanations, PR set, policy, release SHA or release evidence requires a new target approval.

`finish` is read-only by default. `finish --apply` posts verification evidence, reads it back, rechecks the plan, closes as completed, then reads back the closed state. It blocks unmerged PRs, missing/failed/pending/skipped checks, omitted related PRs, incomplete acceptance coverage, approval waits and missing production evidence. Merge alone never closes the Issue.

Production changes require checks on every merge SHA and the final deployed SHA. Deployment and status publishers must match policy. Each required test must belong to an actual job of its policy-pinned workflow run on the release SHA. The deployment payload must name a successful, trusted base-branch verifier workflow run for that SHA, whose `issue-deploy-receipts` artifact contains the exact deployment/status IDs, SHA, environment and successful state. Preview deployments and unrelated provider projects do not qualify. A provider/local rollout adapter must actually verify the approved runtime target before publishing this evidence; an adapter not yet connected leaves finish blocked.

Changes confined to the policy's exact operational-tool path allowlist use tool checks and do not require restarting an application that they do not change. Renames from production paths still require production evidence. Repository data edits are not automatically classified as software development.

Reopen or contract changes require a fresh start from the new base. Retrospective work must be labeled honestly. A temporary, owner-approved transition for a PR already in progress is allowed only for PR numbers pinned in trusted policy, an exact head/Issue/body digest/epoch and explicit acceptance contract. The transition packet uses `issue-gate-transition:v1`, `kind:retrospective`, `accepted:true` and a concrete reason. It does not prove pre-start checks, does not waive release/acceptance/finish checks, and does not extend to another PR or changed head. New work uses the normal start broker.

Journals record mutation intent before external writes. After unknown/partial results, reconcile GitHub comments and Issue states before any retry. The same journal cannot be overwritten; do not bypass this with another name. Multi-Issue close is not atomic.

Required-status enforcement must be configured separately after checking real head-bound success/failure results. Keep all existing protection rules. If the repository plan or permissions cannot enforce required checks, report the limit; do not present successful Actions as enforced merge protection.

Limits: local edits and hooks can be bypassed; ignored/other local files are not monitored. Commit timestamps can be manipulated. Acceptance descriptions and test sufficiency still require review. A repository writer with `checks:write` can publish the same check name through another Actions workflow: standard required-status rules pin the app, not the workflow. CLI finish rejects such forged evidence; fully preventing merge that way requires a distinct trusted app or a required-workflow rule, which this installation does not create. Owner/admin changes to trusted workflows/policy, manual close and dynamic changes between checks remain authority/operation boundaries.

Tests: `cd tools/issue-lifecycle && python3 -m unittest discover -s tests -v`.

Live verification probe: validate the trusted start receipt and head-bound CI before enabling required status.
