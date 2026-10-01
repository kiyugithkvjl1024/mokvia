# GTD Local Data Schema

## Storage contract

Each Purpose, Vision, Goal, Roadmap Outcome, Cycle, Area, Project, Task, Progress, or Review is stored in one `.md` file. The file begins with constrained YAML frontmatter delimited by `---`, followed by a human-readable Markdown body. IDs are non-empty and unique across every entity type in the repository, including archived records. Dates and timestamps use ISO 8601; timestamps include a timezone offset.

The frontmatter parser supports top-level `key: value` pairs only. Avoid nested YAML. Inline lists such as `[home, phone]` are permitted for list-like values.

## Time Allocation Plan

`time-allocation-plans/` stores one Markdown file per revision. The kind is
`time_allocation_plans`, the type is exactly `time_allocation_plan`, and IDs use
`time-allocation-plan-YYYYMMDD-NNN`. Required frontmatter keys are `id`, `type`,
`title`, `axis`, `period_kind`, `period_start`, `period_end`, `revision`,
`status`, `input_mode`, `total_minutes`, `created_at`, and `updated_at`.

`axis` is exactly `area`, `goal`, or `roadmap_outcome`; `period_kind` is exactly
`day`, `week`, `month`, or `year`; `status` is exactly `active`, `superseded`, or
`withdrawn`; and `input_mode` is exactly `ratio` or `minutes`. `revision` is a
positive integer. `period_start` and server-calculated `period_end` are ISO dates
whose boundaries use Asia/Tokyo; `period_end` is never accepted as user authority.
Revisions after 1 require `supersedes_id`.

The body contains `## Allocations` and one deterministic line per allocation in
the form `- <target-token>: <whole-number>`. A target token is an Entity ID
matching the axis or the literal `none`. Ratio values are basis points summing to
10000; minute values sum to `total_minutes`. Missing tokens mean zero and never an
inferred allocation.

Only `resource_allocation_create`, `resource_allocation_revise`,
`resource_allocation_withdraw`, and `resource_allocation_expand` may mutate this
Entity. Expansion accepts only an explicit active source plan, its current source
hash, and selected target period kinds; the server derives canonical targets.
Its compact preview and token-only apply are expansion-only compatibility
contracts. Generic create, update, archive, and delete reject
`time_allocation_plan`. Revision atomically supersedes the former active revision
and activates the new one; there is at most one active revision for an `(axis,
period_kind, period_start)` key.

## Task

Required keys:

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `task-20260716-001` |
| `type` | Exactly `task` |
| `title` | Non-empty action or captured input |
| `status` | One of `inbox`, `planned`, `next`, `doing`, `waiting`, `scheduled`, `someday`, `done` |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

Optional keys:

| Key | Contract |
| --- | --- |
| `project_id` | ID of one confirmed Project; empty when unlinked |
| `project_position` | Optional positive integer ordering a Task inside its exact Project/status/primary-parent sibling group |
| `area_id` | ID of one confirmed Area for a standalone Task; empty when unlinked or when `project_id` is set |
| `action_date` | Optional real ISO-8601 date (`YYYY-MM-DD`) for the intended day of action; Japanese label `対応予定日` |
| `due` | ISO-8601 date or timestamp for a deadline |
| `scheduled_start` | ISO-8601 timestamp with timezone |
| `scheduled_end` | ISO-8601 timestamp with timezone; not earlier than start |
| `available_from` | Real ISO-8601 date or timezone-aware timestamp on or after which the Task may start; a date means 00:00 in Asia/Tokyo; retained after release |
| `contexts` | Inline list of Context names |
| `estimated_minutes` | Positive whole-number estimate |
| `depends_on` | Ordered inline list of Task IDs that must all be `done` before this Task can start |
| `waiting_for` | Person, event, or condition being awaited |
| `work_started_at` | Work-session start as an ISO-8601 timestamp with timezone and second precision |
| `work_ended_at` | Work-session end as an ISO-8601 timestamp with timezone and second precision; not earlier than `work_started_at` |
| `resume_status` | Return status for an interrupted active Task; exactly `next` or `scheduled` |
| `continuation_of` | ID of the completed Task from which this continuation was created; must resolve to one unique `status: done` Task in active or archived repository data |
| `timer_kind` | `break` only; requires `timer_ends_at` and is allowed only for a break Task in `doing` or `done` |
| `timer_ends_at` | ISO-8601 timestamp with timezone and second precision, exactly 300 seconds after `work_started_at` for `timer_kind: break` |
| `calendar_id` | Exactly `example@group.calendar.google.com` |
| `calendar_event_id` | Non-empty printable ASCII event ID without whitespace or path separators |
| `calendar_event_url` | HTTPS Google Calendar event URL without embedded credentials |
| `calendar_event_kind` | Exactly `all_day` or `timed` |
| `calendar_sync_version` | `2` only; set with the complete Calendar identity for a Task-origin projection |
| `started_at` | ISO-8601 timestamp with timezone; allowed only for `doing` or `done` |
| `completed_at` | ISO-8601 timestamp with timezone; allowed only for `done` and strictly later than `started_at` when a start exists |

`status: inbox` Task files belong under `inbox/`; clarified Task files belong under `tasks/`. A `waiting_for` value is relevant to `waiting`; a scheduled window is relevant to `scheduled`. A due date does not itself make a Task Scheduled.

The three date concepts are distinct: `action_date` is the intended action day,
`due` remains the deadline, and `scheduled_start` and `scheduled_end` remain a fixed timed appointment. `action_date` is retained as history when a Task starts
or completes; an interrupted Task's continuation uses the interruption day's JST date. It has no priority or ordering effect, and is forbidden on both
inbox and planned Tasks. A transition into either status removes it in the same
atomic workflow.

`planned` means a saved Project-linked commitment that is not yet actionable;
it may be a dependency descendant, an explicitly planned dependency-free tree root,
or a standalone Task outside the planning tree, and it is not Waiting. Status and
planning-tree membership are independent: membership is derived from
`project_position`, a primary `depends_on`, or use as another same-Project Task's
primary parent, never from `status: planned` alone. A standalone planned Task has no
such structural evidence. A planned Task requires `project_id` and forbids
`waiting_for`, `action_date`, `scheduled_start`, `scheduled_end`, Calendar identity,
`calendar_sync_version`, `started_at`, and `completed_at`. It is
excluded from Focus Today/availability results and from Calendar projection.
`project_position` may be absent on legacy tree Tasks and is normally absent on
standalone Tasks; the first ordered sibling mutation deterministically normalizes
only the exact tree sibling group to contiguous 1-based positions.

`project_id` and direct `area_id` are mutually exclusive. A Task linked to a
Project derives its effective Area from that Project. Removing a Project link
does not infer or copy an Area onto the Task. Interrupt and continuation flows
preserve a direct `area_id` only when the Task has no Project.

`depends_on`は自己参照、重複、存在しないID、非Task、固定`5分休憩` Task、未完了の
archive Taskを拒否する。完了済みarchive Taskへの参照は履歴として有効であり、依存
解除後もリストを保持する。未完了依存を追加した通常Taskは同じmutationで`waiting`へ、
`planned` Taskは`planned`を維持する。残りが全件`done`になった`waiting`または
`planned` Taskは同じatomic workflowで`next`へ移る。依存先を
再オープンしても、既に解除されたTaskを`waiting`へ戻さない。`waiting` Task同士の
依存循環はrepository validationで拒否する。

`available_from`、`depends_on`、`waiting_for`は独立した開始gateである。Taskを
`waiting`から`next`へ自動解除できるのは、`available_from`が未設定または
timezone-awareな判定時刻に到達済み、全`depends_on`が`done`、かつ`waiting_for`が空の場合だけ
である。自動解除の対象は非空の`available_from`または非空の`depends_on`を持つ
Taskに限り、`depends_on: []`や自由記述だけの既存`waiting`は変えない。
`available_from`は実在ISO日付またはtimezone付きISO-8601 timestampとし、日付値は
Asia/Tokyoの当日00:00として比較する。timestampは表すinstantで比較し、timezoneなし
timestampは拒否する。解除後も履歴として保持し、空値は未設定として扱う。未来の
`available_from`を`next`へ保存すると同じmutationで`waiting`へ移り、日時の到達
または削除後に残るgateがなければ同じmutationで`next`へ移る。

At most one non-archived Task may have `status: doing` across the repository. A
legacy Task with all four work-session fields unset remains valid. A Task with
`continuation_of` is not legacy; when it is `doing`, `work_started_at` and
`resume_status` are required. Once a work session is recorded, these
combinations are authoritative:

- An active session is `status: doing` with `work_started_at` and
  `resume_status`, and without `work_ended_at`.
- A closed session is `status: done` with `work_started_at` and
  `work_ended_at`, and without `resume_status`.
- A continuation may carry `continuation_of` without work timestamps until it
  is started. Empty optional values are treated as unset.
- A break session has `timer_kind: break` and `timer_ends_at` while `doing` or
  `done`; its normal timed completion records `work_ended_at` exactly equal to
  `timer_ends_at`.

Starting a Task requires every availability gate to be satisfied, records `work_started_at`, changes its status to `doing`, and
sets `resume_status` to `scheduled` only when its previous status was
`scheduled`; every other incomplete status resumes to `next`. Starting a
non-scheduled Task removes `scheduled_start` and `scheduled_end`; a non-empty
`waiting_for` blocks Start and is never cleared by Start. Completing an active Task changes it to `done`, records
`work_ended_at`, and removes `resume_status`.

`correct_work_session`は、非アーカイブの通常`doing`または`done` Taskについて、
既に有効な実績を記録した作業区間だけを変更できる。`work_started_at`を必須とし、
`done`は`work_ended_at`も必須、`doing`では禁止する。送信時刻はタイムゾーン付き
ISO 8601文字列の秒精度のまま保存し、未来時刻と開始より前の終了を拒否する。欠けた
作業区間の作成、不整合な既存記録の修復、statusまたは`resume_status`の変更、休憩、
archive、非Task entityの対象化はできない。本文、リンク、配置は維持する。非Calendar
Taskでの他のfrontmatter変更はサーバー生成の`updated_at`だけである。完全なCalendar
identityを持つTaskでは、代わりに`started_at`を`work_started_at`へ、`done`の
`completed_at`を`work_ended_at`へmirrorし、`calendar_event_kind`を`timed`にする。
部分identityは拒否し、これはTask frontmatterだけのmutationでGoogle Calendarへ直接
書き込まない。

For a Calendar-linked Task, Start also records `started_at` at exactly the
same instant as `work_started_at` and changes `calendar_event_kind` to
`timed`. Complete, Interrupt, and Switch close an active interval with
`completed_at` equal to `work_ended_at`; an older work-only session is
backfilled so `started_at` equals `work_started_at`. The closing instant must
be strictly later than the start. Direct completion without either start is
valid and keeps `calendar_event_kind: all_day`.

Interrupting an active Task closes it as `done` and creates a new Task with the
same title, body, Project, due value, Contexts, estimate, and its own dependency
references. The new Task has
no work timestamps, points `continuation_of` to the closed Task ID, and uses
the recorded return status. It also receives no Calendar identity or Calendar
lifecycle timestamps. A scheduled continuation alone retains its
scheduled window. Elapsed work is derived from the two timestamps and is never
stored.

完了によって全依存が充足した非アーカイブ`waiting` Taskは、完了Taskと同じ
`WorkflowPlan`内で、他のgateもすべて解消している場合だけID順に`next`へ移る。
`waiting_for`は独立gateとして保持し、非空なら解除しない。それ以外のfrontmatter、
本文、`depends_on`は保持する。中断では後続Taskの旧ID参照を続きTask
IDへ同じworkflow内で付け替え、解除しない。最後の依存Taskを完了して後続Taskへ
切り替える場合は、後続を重複effectにせず直接`doing`へ移す。

The fixed `5分休憩` workflow creates a break Task with `work_started_at` and
`timer_ends_at` exactly 300 seconds apart. Its dedicated local scheduler may
complete that Task only; it does not start its continuation. Break metadata is
not Calendar metadata.

Calendar metadata belongs only to Tasks. The four identity fields
`calendar_id`, `calendar_event_id`, `calendar_event_url`, and
`calendar_event_kind` are classified before synchronization as either all unset
(`null` or the empty string) or one complete non-empty set; any partial set is
invalid and fails closed. If `calendar_sync_version` has a value,
the same complete identity is required. `calendar_sync_version: 2` marks a
Task-origin projection and the Task is canonical for it. `started_at` and
`completed_at` require that complete identity. Direct `done`
with `completed_at` and no `started_at` is valid. Existing Tasks without Calendar
metadata remain valid unchanged.

外部Calendarフィールドは互換性のため残るだけです。本配布に同期サービスや認証情報はなく、新規データでは使用しません。ローカルカレンダーはaction_date、due、scheduled/workの日時だけを投影します。

## Purpose

Purpose files belong under `purposes/`. Required keys are `id`, `type`,
`title`, `created_at`, and `updated_at`; `type` is exactly `purpose`. Purpose
has no lifecycle `status`. Its body contains the exact Markdown lines
`## Purpose` and `## Principles`; empty section content is valid. It is
recommended to keep each heading once and in Purpose-then-Principles order. At
most one non-archived Purpose may exist; archived historical Purpose records do
not count toward that singleton. Archived Purpose bodies follow the same
heading contract.

## Vision

Vision files belong under `visions/`.

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `vision-20260814-001` |
| `type` | Exactly `vision` |
| `title` | Non-empty description of the desired future |
| `status` | One of `active`, `on_hold`, `realized`, `dropped` |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

The intended time horizon is body guidance, not a validator-enforced year
range.

## Goal

Required keys:

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `goal-20260716-001` |
| `type` | Exactly `goal` |
| `title` | Non-empty desired medium- or long-term outcome |
| `status` | One of `active`, `on_hold`, `achieved`, `dropped` |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

Optional keys:

| Key | Contract |
| --- | --- |
| `vision_id` | ID of one confirmed Vision |
| `target_date` | Real ISO-8601 date |

## Area

Area files belong under `areas/`. Required keys are `id`, `type`, `title`,
`created_at`, and `updated_at`; `type` is exactly `area`. Area has no lifecycle
`status` and is archived when no longer relevant.

Optional keys:

| Key | Contract |
| --- | --- |
| `health` | Exactly `maintained` or `needs_attention`; unset means unchecked |
| `last_reviewed_on` | Real ISO-8601 date of an explicit health review; unset means unchecked |

An explicit health-review operation updates `health` and `last_reviewed_on`
together. Ordinary edits do not advance the review date. The body contains
`## 維持基準` and `## メモ`.

## Roadmap Outcome

Roadmap Outcome files belong under `roadmap-outcomes/`.

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `roadmap-outcome-20260825-001` |
| `type` | Exactly `roadmap_outcome` |
| `title` | Non-empty description of the result |
| `status` | One of `active`, `achieved`, `dropped` |
| `goal_id` | ID of exactly one confirmed non-archived Goal |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

Optional keys:

| Key | Contract |
| --- | --- |
| `target_date` | Optional real ISO-8601 date; arrival estimate only, with no ordering or priority side effect |
| `roadmap_lane` | Exactly `next` or `later` |
| `roadmap_position` | Positive whole-number position within its lane |

An `active` Outcome outside the active Cycle has both lane fields. An Outcome
in the active Cycle has neither. `achieved` and `dropped` Outcomes also have
neither. Within each lane, positions are unique and contiguous from 1. The body
contains `## 成功条件` and `## メモ`; activation requires non-whitespace content
under `## 成功条件`, while planned CRUD may retain an empty section.

## Cycle

Cycle files belong under `cycles/`.

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `cycle-20260825-001` |
| `type` | Exactly `cycle` |
| `title` | Non-empty Cycle name |
| `status` | One of `planned`, `active`, `completed`, `cancelled` |
| `start_date` | Real ISO-8601 date |
| `end_date` | Real ISO-8601 date on or after `start_date` |
| `outcome_ids` | Inline list of zero to two unique Roadmap Outcome IDs |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

Closing-result keys are forbidden for `planned` and `active`, and are all
server-managed by `cycle_close`:

| Key | Contract |
| --- | --- |
| `achieved_outcome_ids` | Members closed as achieved |
| `carried_outcome_ids` | Members added to `carryover_cycle_id` |
| `next_outcome_ids` | Members restored at the end of Next |
| `later_outcome_ids` | Members restored at the end of Later |
| `dropped_outcome_ids` | Members closed as dropped |
| `carryover_cycle_id` | Existing non-archived `planned` Cycle; required exactly when `carried_outcome_ids` is non-empty |
| `cancellation_reason` | Non-empty text required exactly for `status: cancelled` |

For `completed` and `cancelled`, all five result-list keys are present. Their
members are pairwise disjoint and their union equals `outcome_ids`; no other ID
is allowed. The carryover target must differ from the closing Cycle, must not
already contain a carried member, and must remain within the two-Outcome cap.
Each carried Outcome is restored to the end of Next while it waits in the
planned Cycle.
The body contains `## 振り返り` and `## メモ`; closing requires non-whitespace
content under `## 振り返り`.

The initial UI end date is `start_date + 41 days`, producing 42 inclusive days;
both dates remain stored facts. Across non-archived `planned` and `active`
Cycles, inclusive date ranges do not overlap, and at most one Cycle is
`active`. Planned members remain ordered in Next/Later. Active members have no
lane fields, and the `outcome_ids`, `start_date`, and `end_date` of an active
Cycle cannot be changed except by closing it as cancelled and creating a new
Cycle.

## Project

Required keys:

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `project-20260716-001` |
| `type` | Exactly `project` |
| `title` | Non-empty desired outcome |
| `status` | One of `not_started`, `doing`, `on_hold`, `completed`, `dropped`; legacy `active` remains readable only for migration |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

Optional keys:

| Key | Contract |
| --- | --- |
| `goal_id` | ID of one confirmed Goal |
| `roadmap_outcome_id` | ID of one confirmed Roadmap Outcome |
| `area_id` | ID of one confirmed primary Area |
| `planned_start_date` | Optional real `YYYY-MM-DD` date for the Project's estimated period start; must be paired with `planned_end_date` |
| `planned_end_date` | Optional real `YYYY-MM-DD` date for the Project's estimated period end; must be paired with `planned_start_date` and be on or after it |
| `kanban_position` | Optional positive whole-number display position within the effective Project status lane; never priority |

`goal_id` and `roadmap_outcome_id` are mutually exclusive. Area is parallel to
that choice: a Project may hold one of the two direction links and also one
`area_id`. A Project linked to a Roadmap Outcome derives its Goal from the
Outcome. Existing Projects retain their current Goal link and are never
auto-migrated.

`planned_start_date` and `planned_end_date` are either both absent or both
present. They are real calendar dates in `YYYY-MM-DD` form; the end date may
equal the start date but may not precede it. The pair is the Project's
「目安期間（見込み期間）」 only. It is not a deadline, schedule, priority,
automatic ordering, Cycle membership, or status transition trigger, and has no
effect on Focus, Calendar, or Resource Allocation. Existing Projects remain
without these fields unless explicitly edited. Ordinary create/update and the
Roadmap bundle update accept the pair. Sending both fields as empty strings on
update clears both keys; a one-sided value or one-sided clear is rejected.

New Projects default to `not_started`. `not_started` and `doing` are the
active-equivalent states used by factual counts and Project execution views.
Starting a linked Task changes a `not_started` Project to `doing` in the same
atomic workflow; Project completion is never inferred. A legacy `active`
Project may only be updated to `not_started` or `doing`, and no new create or
update may write `active`.

Non-archived Projects are displayed inside the effective `not_started`, `doing`,
`on_hold`, `completed`, or `dropped` lane. A legacy `active` Project reads as
`not_started`. Positioned Projects sort by `kanban_position`; legacy Projects
without it remain valid and follow positioned Projects in repository-path order.
When every Project in a lane is positioned, values are unique and contiguous
from 1. The first ordering mutation that affects a legacy lane normalizes that
lane. `project_move` atomically removes the target from its source sequence,
inserts it at the requested one-based destination position, and rewrites every
changed neighbour in the same rollback unit. New Projects and non-drag status
changes append to the destination; linked Task start appends its Project to
`doing`; archive compacts the source while preserving the archived Project's
original frontmatter. This field has no effect on Focus selection, Roadmap,
review facts, priority, or automatic ranking.

## Review

Daily and Weekly Reviews share one schema.

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `review-daily-20260716-001` |
| `type` | Exactly `review` |
| `title` | Non-empty review title |
| `review_kind` | Exactly `daily` or `weekly` |
| `period_start` | ISO-8601 date for the period reviewed |
| `created_at` | ISO-8601 timestamp with timezone |

Daily Reviews belong under `reviews/daily/`; Weekly Reviews belong under `reviews/weekly/`.

## Progress

Progress is the canonical record of a daily fact. It is stored under `progress/` and is never copied into a separate weekly or monthly canonical file.

| Key | Contract |
| --- | --- |
| `id` | Unique identifier, for example `progress-20260830-001` |
| `type` | Exactly `progress` |
| `title` | Non-empty description of the change or fact |
| `occurred_on` | ISO-8601 date (`YYYY-MM-DD`) |
| `visibility` | Exactly `private`; the server fixes this value |
| `created_at` | ISO-8601 timestamp with timezone |
| `updated_at` | ISO-8601 timestamp with timezone |

At most one of `project_id`, `goal_id`, or `area_id` may be set. The optional body may contain the exact H2 sections `## メリット・学び` and `## 証拠`. Progress can retain an archived origin and does not block archiving its origin. Weekly and monthly views read these files through projections; they do not persist report copies.

## Mutation rules

- Preserve `id`, `type`, and `created_at` during ordinary edits and archiving.
- Update `updated_at` whenever a Purpose, Vision, Goal, Roadmap Outcome, Cycle, Area, Project, or Task materially changes.
- Non-archived Goal, Roadmap Outcome, Cycle, Project, and Task links must resolve to an existing, correctly typed, non-archived target.
- Archived records retain their links; every retained link must resolve to one correctly typed target, whether active or archived.
- Never cascade, unlink children implicitly, or rewrite children during archive. Reject archiving a Vision, Area, Goal, Roadmap Outcome, or Project while a non-archived child still references it. In particular, a Roadmap Outcome cannot be archived while a non-archived Cycle or Project references it. An active Cycle cannot be archived.
- Reject moving a Project to `completed` or `dropped` while any non-archived linked Task is unfinished (`unfinished_project_tasks`). Reject moving it to `not_started` or `on_hold` while a linked Task is `doing` (`project_has_doing_task`).
- Reject starting a linked Task when its Project is `on_hold`, `completed`, or `dropped` (`project_not_executable`).
- Reject changing a Roadmap Outcome to `achieved` or `dropped` while any non-archived Project linked to it has `status: not_started`, `doing`, or `on_hold` (legacy `active` is treated as unfinished until migrated).
- Do not add a priority field or gamification state.
- AI Proposal values are not canonical until explicitly confirmed by the user.
- Calendar identity and lifecycle metadata must satisfy the complete-set and status rules above; archiving preserves them with all original frontmatter.
