import json
import gc
import datetime
import hashlib
import pathlib
import tempfile
import threading
import unittest
import weakref
from unittest import mock

from scripts.validate_frontmatter import (
    FORBIDDEN_KEYS,
    validate_document_map,
    validate_repository,
)
from webapp import api
from webapp.store import (
    ConflictError,
    DestinationConflict,
    Entity,
    InputError,
    MutationPlanConflict,
    MutationRecoveryRequired,
    SchemaError,
    Store,
    StoreLockTimeout,
    WorkflowPostCommitCleanupError,
    WorkflowRollbackError,
    serialize_frontmatter,
)


TASK_STATUSES = (
    "inbox",
    "planned",
    "next",
    "doing",
    "waiting",
    "scheduled",
    "someday",
    "done",
)
CALENDAR_FIELDS = {
    "calendar_id": "example@group.calendar.google.com",
    "calendar_event_id": "event-api-workflow",
    "calendar_event_url": "https://calendar.google.com/calendar/event?eid=YXBp",
    "calendar_event_kind": "all_day",
}
PURPOSE_BODY = "## Purpose\n\n## Principles\n"

handle = api.handle
ApiState = getattr(api, "ApiState", None)


class ReadApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = pathlib.Path(self.temporary_directory.name)
        for directory in (
            "inbox",
            "tasks",
            "purposes",
            "visions",
            "areas",
            "projects",
            "goals",
            "reviews/daily",
            "reviews/weekly",
            "archive",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root)

    def write_entity(
        self,
        relative_path: str,
        entity_type: str,
        entity_id: str,
        *,
        body: str = "Body\n",
        **fields: str,
    ) -> None:
        timestamp = "2026-07-17T09:00:00+09:00"
        if entity_type == "review":
            frontmatter = {
                "id": entity_id,
                "type": "review",
                "title": fields.pop("title", "Review"),
                "review_kind": fields.pop("review_kind", "daily"),
                "period_start": fields.pop("period_start", "2026-07-17"),
                "created_at": timestamp,
                **fields,
            }
        elif entity_type == "progress":
            frontmatter = {
                "id": entity_id,
                "type": "progress",
                "title": fields.pop("title", entity_id),
                "occurred_on": fields.pop("occurred_on"),
                "visibility": fields.pop("visibility", "private"),
                "created_at": timestamp,
                "updated_at": timestamp,
                **fields,
            }
        elif entity_type in {"purpose", "area"}:
            frontmatter = {
                "id": entity_id,
                "type": entity_type,
                "title": fields.pop("title", entity_id),
                "created_at": timestamp,
                "updated_at": timestamp,
                **fields,
            }
        else:
            frontmatter = {
                "id": entity_id,
                "type": entity_type,
                "title": fields.pop("title", entity_id),
                "status": fields.pop("status", "active"),
                "created_at": timestamp,
                "updated_at": timestamp,
                **fields,
            }
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            serialize_frontmatter(frontmatter, entity_type) + body,
            encoding="utf-8",
        )

    def assert_error(self, result: tuple[int, dict[str, object]], status: int, code: str) -> None:
        actual_status, payload = result
        self.assertEqual(actual_status, status)
        self.assertEqual(payload["error"]["code"], code)  # type: ignore[index]
        self.assertEqual(payload["error"]["details"], [])  # type: ignore[index]

    def test_progress_route_returns_an_inclusive_read_only_period(self) -> None:
        """Catches the period endpoint being absent or omitting its boundary day."""
        self.write_entity(
            "progress/progress-20260803-001.md",
            "progress",
            "progress-20260803-001",
            title="変化",
            occurred_on="2026-08-03",
            body="## メリット・学び\n\n学び\n",
        )

        status, payload = handle(
            self.store,
            "GET",
            "/api/v1/progress",
            {"from": "2026-08-03", "to": "2026-08-03"},
        )

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["items"]], ["progress-20260803-001"])  # type: ignore[index]
        snapshot_status, snapshot = handle(self.store, "GET", "/api/v1/snapshot")
        self.assertEqual(snapshot_status, 200)
        self.assertNotIn(
            "progress-20260803-001",
            [item["id"] for item in snapshot["entities"]],  # type: ignore[index]
        )

    def test_progress_routes_reject_invalid_range_and_month(self) -> None:
        """Catches unbounded period scans or malformed monthly report queries."""
        self.assert_error(
            handle(
                self.store,
                "GET",
                "/api/v1/progress",
                {"from": "2026-08-01", "to": "2026-09-02"},
            ),
            400,
            "invalid_query",
        )
        self.assert_error(
            handle(self.store, "GET", "/api/v1/reports/progress", {"month": "2026-13"}),
            400,
            "invalid_query",
        )

    def test_health_is_factual_and_does_not_scan_repository(self) -> None:
        class NoScanStore:
            def list_entities(self) -> list[Entity]:
                raise AssertionError("health scanned entities")

            def repository_errors(self) -> list[str]:
                raise AssertionError("health ran validator")

        status, payload = handle(NoScanStore(), "get", "/api/v1/health")  # type: ignore[arg-type]

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "status": "ok",
                "bind_origin": "http://127.0.0.1:24873",
                "validator_available": True,
            },
        )

    def test_quick_start_suggestions_validates_query_caps_response_and_only_reads_snapshot(self) -> None:
        self.write_entity("areas/home.md", "area", "area-home", title="Home")
        self.write_entity(
            "tasks/one.md", "task", "task-one", title="報告を書く", status="done",
            work_started_at="2026-08-31T09:00:00+09:00", area_id="area-home",
        )
        self.write_entity(
            "tasks/two.md", "task", "task-two", title="報告を書く", status="done",
            work_started_at="2026-08-24T09:00:00+09:00", area_id="area-home",
        )
        before = sorted(path.read_bytes() for path in self.root.rglob("*.md"))
        with mock.patch.object(
            api, "_gtd_now", return_value=datetime.datetime(2026, 8, 31, 9, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        ):
            status, payload = handle(
                self.store, "GET", "/api/v1/quick-start-suggestions", {"q": "報"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"suggestions": [{"title": "報告を書く", "area_id": "area-home", "reason_code": "query_match"}]},
        )
        self.assertEqual(before, sorted(path.read_bytes() for path in self.root.rglob("*.md")))
        for query in ({"q": ["a", "b"]}, {"unknown": "x"}, {"q": "x" * 301}):
            with self.subTest(query=query):
                self.assert_error(
                    handle(self.store, "GET", "/api/v1/quick-start-suggestions", query),
                    400,
                    "invalid_query",
                )

    def test_production_health_exposes_exact_mutation_origins(self) -> None:
        origins = (
            "http://127.0.0.1:24873",
            "http://localhost:24873",
            "http://localhost:24873",
        )
        status, payload = handle(
            self.store,
            "GET",
            "/api/v1/health",
            mutation_origins=origins,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mutation_origins"], list(origins))

    def test_snapshot_omits_bodies_and_archives_and_returns_all_facts(self) -> None:
        self.write_entity("inbox/z.md", "task", "task-inbox", status="inbox")
        self.write_entity(
            "tasks/b.md", "task", "task-next", status="next", project_id="project-covered"
        )
        self.write_entity("tasks/c.md", "task", "task-doing", status="doing")
        self.write_entity("projects/b.md", "project", "project-missing", status="not_started")
        self.write_entity("projects/a.md", "project", "project-covered", status="doing")
        self.write_entity("projects/c.md", "project", "project-hold", status="on_hold")
        self.write_entity("goals/g.md", "goal", "goal-one", status="active")
        self.write_entity(
            "reviews/daily/r.md",
            "review",
            "review-one",
            review_kind="daily",
        )
        self.write_entity("archive/old.md", "task", "task-old", status="done")

        with mock.patch.object(
            api, "_gtd_today", return_value=datetime.date(2026, 8, 29)
        ):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot")

        self.assertEqual(status, 200)
        entities = payload["entities"]
        self.assertEqual(
            [entity["path"] for entity in entities],  # type: ignore[index]
            [
                "goals/g.md",
                "inbox/z.md",
                "projects/a.md",
                "projects/b.md",
                "projects/c.md",
                "reviews/daily/r.md",
                "tasks/b.md",
                "tasks/c.md",
            ],
        )

        self.assertTrue(all("body" not in entity for entity in entities))  # type: ignore[arg-type]
        self.assertTrue(all(entity["archived"] is False for entity in entities))  # type: ignore[index]
        self.assertEqual(
            payload["facts"],
            {
                "gtd_today": "2026-08-29",
                "inbox_count": 1,
                "task_status_counts": {
                    "inbox": 1,
                    "planned": 0,
                    "next": 1,
                    "doing": 1,
                    "waiting": 0,
                    "scheduled": 0,
                    "someday": 0,
                    "done": 0,
                },
                "active_projects_without_next_action": [],
                "focus_actionable_next_ids": ["task-next"],
                "focus_done_task_ids": ["task-old"],
                "archive_count": 1,
                "focus_notification_pause": None,
                "direction": {
                    "purpose": None,
                    "vision_status_counts": {
                        "active": 0,
                        "on_hold": 0,
                        "realized": 0,
                        "dropped": 0,
                    },
                    "active_goal_count": 1,
                    "next_goal_target_date": None,
                    "area_health_counts": {
                        "maintained": 0,
                        "needs_attention": 0,
                        "unreviewed": 0,
                    },
                    "active_project_count": 2,
                },
                "roadmap": {
                    "active_cycle_id": None,
                    "now_outcome_ids": [],
                    "next_outcome_ids": [],
                    "later_outcome_ids": [],
                    "planned_cycle_ids": [],
                },
            },
        )
        self.assertEqual(payload["mutation_state"], {"recovery_required": False})

    def test_focus_actionable_next_ids_respect_release_gates_and_archived_done_dependencies(self) -> None:
        """Catches blocked or future tasks being offered as executable Next Actions."""
        self.write_entity("archive/done.md", "task", "task-done", status="done")
        self.write_entity("tasks/open.md", "task", "task-open", status="next")
        self.write_entity("tasks/ready.md", "task", "task-ready", status="next", depends_on="[task-done]", available_from="2026-08-29")
        self.write_entity("tasks/future.md", "task", "task-future", status="next", available_from="2026-08-30")
        self.write_entity("tasks/later-today.md", "task", "task-later-today", status="next", available_from="2026-08-29T20:00:00+09:00")
        self.write_entity("tasks/blocked.md", "task", "task-blocked", status="next", depends_on="[task-open]")
        self.write_entity("tasks/waiting.md", "task", "task-waiting", status="next", waiting_for="返信")
        self.write_entity("tasks/action-future.md", "task", "task-action-future", status="next", action_date="2026-08-30")
        with mock.patch.object(api, "_gtd_now", return_value=datetime.datetime(2026, 8, 29, 12, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))), mock.patch.object(api, "_gtd_today", return_value=datetime.date(2026, 8, 29)):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(payload["facts"]["focus_actionable_next_ids"], ["task-open", "task-ready"])
        self.assertEqual(payload["facts"]["focus_done_task_ids"], ["task-done"])

    def test_snapshot_exposes_the_canonical_jst_gtd_today(self) -> None:
        """Catches clients deriving the Focus day from their own clock."""
        with mock.patch.object(
            api, "_gtd_today", return_value=datetime.date(2026, 8, 29)
        ):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot", {})

        self.assertEqual(status, 200)
        self.assertEqual(payload["facts"].get("gtd_today"), "2026-08-29")  # type: ignore[union-attr]

    def test_snapshot_reports_persistent_recovery_gate_without_path_details(self) -> None:
        (self.root / ".webapp-mutation-state").write_bytes(b"opaque")
        status, payload = handle(self.store, "GET", "/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["mutation_state"], {"recovery_required": True}
        )
        serialized = json.dumps(payload)
        self.assertNotIn(".webapp-mutation-state", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_snapshot_direction_facts_are_unfiltered_factual_counts(self) -> None:
        self.write_entity(
            "purposes/p.md",
            "purpose",
            "purpose-one",
            title="Choose deliberately",
            body="\n## Purpose\n\nAct with care.\n\n## Principles\n\nBe honest.\n",
        )
        for status in ("active", "active", "on_hold", "realized", "dropped"):
            index = len(list((self.root / "visions").glob("*.md")))
            self.write_entity(
                f"visions/{index}.md",
                "vision",
                f"vision-{index}",
                status=status,
            )
        self.write_entity(
            "goals/active-later.md",
            "goal",
            "goal-active-later",
            status="active",
            target_date="2027-05-01",
        )
        self.write_entity(
            "goals/active-next.md",
            "goal",
            "goal-active-next",
            status="active",
            target_date="2026-12-01",
        )
        self.write_entity(
            "goals/hold.md",
            "goal",
            "goal-hold",
            status="on_hold",
            target_date="2026-01-01",
        )
        self.write_entity(
            "areas/maintained.md",
            "area",
            "area-maintained",
            health="maintained",
        )
        self.write_entity(
            "areas/attention.md",
            "area",
            "area-attention",
            health="needs_attention",
        )
        self.write_entity("areas/unreviewed.md", "area", "area-unreviewed")
        self.write_entity("projects/a.md", "project", "project-active", status="not_started")
        self.write_entity("projects/b.md", "project", "project-hold", status="on_hold")

        status, payload = handle(
            self.store, "GET", "/api/v1/snapshot", {"status": "waiting"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["facts"]["direction"],  # type: ignore[index]
            {
                "purpose": {
                    "id": "purpose-one",
                    "title": "Choose deliberately",
                    "excerpt": "Act with care.",
                },
                "vision_status_counts": {
                    "active": 2,
                    "on_hold": 1,
                    "realized": 1,
                    "dropped": 1,
                },
                "active_goal_count": 2,
                "next_goal_target_date": "2026-12-01",
                "area_health_counts": {
                    "maintained": 1,
                    "needs_attention": 1,
                    "unreviewed": 1,
                },
                "active_project_count": 1,
            },
        )

    def test_snapshot_reports_latest_future_strict_focus_pause_for_doing_task(self) -> None:
        self.write_entity(
            "tasks/doing.md",
            "task",
            "task-doing",
            status="doing",
            body=(
                "Body\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-08-19T08:59:59+09:00\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-08-19T09:30:00+09:00\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-02-30T12:00:00+09:00\n"
                "prefix [gtd-focus-monitor] 通知停止期限: 2026-08-19T12:00:00+09:00\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-08-19T10:15:00+09:00\n"
            ),
        )
        self.write_entity(
            "tasks/not-current.md",
            "task",
            "task-next",
            status="next",
            body="[gtd-focus-monitor] 通知停止期限: 2026-08-19T12:00:00+09:00\n",
        )
        now = datetime.datetime(
            2026, 8, 19, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        )

        with mock.patch("webapp.api._gtd_now", return_value=now):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["facts"]["focus_notification_pause"],  # type: ignore[index]
            {"task_id": "task-doing", "deadline": "2026-08-19T10:15:00+09:00"},
        )

    def test_snapshot_omits_focus_pause_when_current_doing_task_is_ambiguous(self) -> None:
        now = datetime.datetime(
            2026, 8, 19, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        )
        with mock.patch("webapp.api._gtd_now", return_value=now):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["facts"]["focus_notification_pause"])  # type: ignore[index]

        for index in range(2):
            self.write_entity(
                f"tasks/doing-{index}.md",
                "task",
                f"task-doing-{index}",
                status="doing",
                body="[gtd-focus-monitor] 通知停止期限: 2026-08-19T10:00:00+09:00\n",
            )
        with mock.patch("webapp.api._gtd_now", return_value=now):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["facts"]["focus_notification_pause"])  # type: ignore[index]

    def test_area_filter_selects_tasks_by_effective_project_or_direct_area(self) -> None:
        self.write_entity("areas/a.md", "area", "area-one")
        self.write_entity("areas/b.md", "area", "area-two")
        self.write_entity(
            "projects/a.md", "project", "project-one", area_id="area-one"
        )
        self.write_entity(
            "projects/b.md", "project", "project-two", area_id="area-two"
        )
        self.write_entity(
            "tasks/inherited.md",
            "task",
            "task-inherited",
            status="next",
            project_id="project-one",
        )
        self.write_entity(
            "tasks/direct.md",
            "task",
            "task-direct",
            status="next",
            area_id="area-one",
        )
        self.write_entity(
            "tasks/other.md",
            "task",
            "task-other",
            status="next",
            project_id="project-two",
        )
        self.write_entity("tasks/unlinked.md", "task", "task-unlinked", status="next")

        status, payload = handle(
            self.store, "GET", "/api/v1/snapshot", {"area_id": "area-one"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [entity["id"] for entity in payload["entities"]],  # type: ignore[index]
            ["task-direct", "task-inherited"],
        )

    def test_snapshot_lock_timeout_is_safe_busy_without_path_details(self) -> None:
        with mock.patch.object(
            self.store,
            "read_snapshot",
            side_effect=StoreLockTimeout("SECRET /tmp/private"),
            create=True,
        ):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot")

        self.assertEqual(status, 503)
        self.assertEqual(
            payload["error"],
            {
                "code": "busy",
                "message": "repository is busy; retry snapshot",
                "details": [],
            },
        )
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertNotIn("/tmp/private", json.dumps(payload))

    def test_facts_are_unfiltered_and_doing_also_covers_project(self) -> None:
        self.write_entity(
            "tasks/a.md", "task", "task-waiting", status="waiting", contexts="[phone]"
        )
        self.write_entity(
            "tasks/b.md", "task", "task-doing", status="doing", project_id="project-covered"
        )
        self.write_entity("projects/a.md", "project", "project-covered", status="doing")

        status, payload = handle(
            self.store, "GET", "/api/v1/snapshot", {"status": "waiting"}
        )

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["entities"]], ["task-waiting"])  # type: ignore[index]
        self.assertEqual(payload["facts"]["task_status_counts"]["doing"], 1)  # type: ignore[index]
        self.assertEqual(payload["facts"]["active_projects_without_next_action"], [])  # type: ignore[index]

    def test_missing_next_action_ignores_not_started_projects_but_keeps_doing_projects(self) -> None:
        """Catches Project Support plans incorrectly treated as missing Next Actions."""
        self.write_entity("projects/not-started.md", "project", "project-not-started", status="not_started")
        self.write_entity("projects/doing.md", "project", "project-doing", status="doing")

        status, payload = handle(self.store, "GET", "/api/v1/snapshot")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["facts"]["active_projects_without_next_action"],  # type: ignore[index]
            ["project-doing"],
        )

    def test_status_project_goal_context_and_max_minutes_filters(self) -> None:
        self.write_entity(
            "tasks/a.md",
            "task",
            "task-match",
            status="next",
            project_id="project-one",
            contexts="[home, Phone]",
            estimated_minutes="15",
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "task-other",
            status="next",
            project_id="project-two",
            contexts="[home]",
            estimated_minutes="30",
        )
        self.write_entity("projects/a.md", "project", "project-one", goal_id="goal-one")
        self.write_entity("projects/b.md", "project", "project-two", goal_id="goal-two")

        cases = (
            ({"status": "next"}, ["task-match", "task-other"]),
            ({"project_id": "project-one"}, ["task-match"]),
            ({"goal_id": "goal-one"}, ["project-one"]),
            ({"context": "Phone"}, ["task-match"]),
            ({"context": "phone"}, []),
            ({"max_minutes": "15"}, ["task-match"]),
            ({"max_minutes": "29"}, ["task-match"]),
        )
        for query, expected_ids in cases:
            with self.subTest(query=query):
                status, payload = handle(self.store, "GET", "/api/v1/snapshot", query)
                self.assertEqual(status, 200)
                self.assertEqual(
                    [item["id"] for item in payload["entities"]],  # type: ignore[index]
                    expected_ids,
                )

    def test_unassigned_filter_selects_only_tasks_without_project_and_conflicts_with_project_id(self) -> None:
        self.write_entity("tasks/a.md", "task", "task-unassigned", status="next")
        self.write_entity(
            "tasks/b.md",
            "task",
            "task-assigned",
            status="next",
            project_id="project-one",
        )
        self.write_entity(
            "projects/a.md", "project", "project-one", status="not_started"
        )

        status, payload = handle(
            self.store, "GET", "/api/v1/snapshot", {"unassigned": "1"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in payload["entities"]],  # type: ignore[index]
            ["task-unassigned"],
        )
        for invalid in (
            {"unassigned": "0"},
            {"unassigned": "true"},
            {"unassigned": "1", "project_id": "project-one"},
        ):
            with self.subTest(query=invalid):
                self.assert_error(
                    handle(self.store, "GET", "/api/v1/snapshot", invalid),
                    400,
                    "invalid_query",
                )

    def test_all_filters_combine_with_and(self) -> None:
        self.write_entity(
            "tasks/a.md",
            "task",
            "task-match",
            status="next",
            project_id="project-one",
            contexts="[phone]",
            estimated_minutes="10",
            due="2026-07-20",
            scheduled_start="2026-07-19T08:00:00+09:00",
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "task-too-long",
            status="next",
            project_id="project-one",
            contexts="[phone]",
            estimated_minutes="20",
            due="2026-07-20",
            scheduled_start="2026-07-19T08:00:00+09:00",
        )

        status, payload = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {
                "status": "next",
                "project_id": "project-one",
                "context": "phone",
                "max_minutes": "10",
                "due_before": "2026-07-20",
                "scheduled_before": "2026-07-18T23:00:00Z",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["entities"]], ["task-match"])  # type: ignore[index]

    def test_availability_uses_action_date(self) -> None:
        calendar_identity = {
            "calendar_id": "example@group.calendar.google.com",
            "calendar_event_url": "https://calendar.google.com/calendar/event?eid=YXBp",
            "calendar_event_kind": "all_day",
        }
        self.write_entity("tasks/a.md", "task", "dateless-next", status="next")
        self.write_entity(
            "tasks/b.md", "task", "future-due-next", status="next", due="2026-07-21"
        )
        self.write_entity(
            "tasks/c.md", "task", "past-action-next", status="next", action_date="2026-07-19",
            calendar_event_id="past-must-event", **calendar_identity,
        )
        self.write_entity(
            "tasks/d.md", "task", "today-action-next", status="next", action_date="2026-07-20",
            calendar_event_id="today-must-event", **calendar_identity,
        )
        self.write_entity(
            "tasks/e.md", "task", "future-action-next", status="next", action_date="2026-07-21",
            calendar_event_id="future-must-event", **calendar_identity,
        )
        self.write_entity("tasks/f.md", "task", "doing", status="doing")
        self.write_entity("tasks/g.md", "task", "waiting", status="waiting")
        with mock.patch.object(api, "_gtd_today", return_value=datetime.date(2026, 7, 20)):
            now_status, now_payload = handle(
                self.store, "GET", "/api/v1/snapshot", {"availability": "now"}
            )
            later_status, later_payload = handle(
                self.store, "GET", "/api/v1/snapshot", {"availability": "later"}
            )

        self.assertEqual(now_status, 200)
        self.assertEqual(
            [item["id"] for item in now_payload["entities"]],  # type: ignore[index]
            ["dateless-next", "future-due-next", "past-action-next", "today-action-next"],
        )
        self.assertEqual(later_status, 200)
        self.assertEqual(
            [item["id"] for item in later_payload["entities"]],  # type: ignore[index]
            ["future-action-next"],
        )

    def test_planned_tasks_are_queryable_for_projects_but_excluded_from_focus_today(self) -> None:
        """Catches Project Support commitments leaking into Focus while hiding from Project reads."""
        self.write_entity(
            "tasks/planned.md",
            "task",
            "task-planned",
            status="planned",
            project_id="project-one",
        )

        status, project_tasks = handle(
            self.store, "GET", "/api/v1/snapshot", {"project_id": "project-one"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [entity["id"] for entity in project_tasks["entities"]],  # type: ignore[index]
            ["task-planned"],
        )
        for query in ({"availability": "now"}, {"today": "1"}):
            with self.subTest(query=query):
                status, focus = handle(self.store, "GET", "/api/v1/snapshot", query)
                self.assertEqual(status, 200)
                self.assertEqual(focus["entities"], [])

    def test_availability_rejects_invalid_values_and_combines_with_filters(self) -> None:
        calendar_identity = {
            "calendar_id": "example@group.calendar.google.com",
            "calendar_event_url": "https://calendar.google.com/calendar/event?eid=YXBp",
            "calendar_event_kind": "all_day",
        }
        self.write_entity(
            "tasks/a.md", "task", "available-match", status="next", project_id="project-one"
        )
        self.write_entity(
            "tasks/b.md", "task", "available-other-project", status="next", project_id="project-two"
        )
        self.write_entity(
            "tasks/c.md", "task", "later-match-project", status="next", project_id="project-one",
            action_date="2026-07-21", calendar_event_id="later-match-event", **calendar_identity,
        )

        with mock.patch.object(api, "_gtd_today", return_value=datetime.date(2026, 7, 20)):
            status, payload = handle(
                self.store,
                "GET",
                "/api/v1/snapshot",
                {"availability": "now", "project_id": "project-one", "status": "next"},
            )

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["entities"]], ["available-match"])  # type: ignore[index]
        for value in ("", "soon", "NOW", "0"):
            with self.subTest(value=value):
                self.assert_error(
                    handle(
                        self.store,
                        "GET",
                        "/api/v1/snapshot",
                        {"availability": value},
                    ),
                    400,
                    "invalid_query",
                )

    def test_due_filter_uses_inclusive_date_and_timestamp_semantics(self) -> None:
        self.write_entity("tasks/a.md", "task", "date-boundary", status="next", due="2026-07-20")
        self.write_entity(
            "tasks/b.md",
            "task",
            "instant-boundary",
            status="next",
            due="2026-07-20T10:00:00+09:00",
        )
        self.write_entity(
            "tasks/c.md",
            "task",
            "same-instant",
            status="next",
            due="2026-07-20T01:00:00Z",
        )
        self.write_entity(
            "tasks/d.md",
            "task",
            "later-instant",
            status="next",
            due="2026-07-20T01:00:01Z",
        )

        _, date_payload = handle(
            self.store, "GET", "/api/v1/snapshot", {"due_before": "2026-07-20"}
        )
        _, instant_payload = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"due_before": "2026-07-20T10:00:00+09:00"},
        )

        self.assertEqual(
            [item["id"] for item in date_payload["entities"]],  # type: ignore[index]
            ["date-boundary", "instant-boundary", "same-instant", "later-instant"],
        )
        self.assertEqual(
            [item["id"] for item in instant_payload["entities"]],  # type: ignore[index]
            ["instant-boundary", "same-instant"],
        )

    def test_date_due_cutoff_uses_end_of_day_in_gtd_local_timezone(self) -> None:
        self.write_entity(
            "tasks/a.md",
            "task",
            "next-local-date-but-before-cutoff",
            status="next",
            due="2026-07-21T00:30:00+10:00",
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "same-local-date-but-after-cutoff",
            status="next",
            due="2026-07-20T23:30:00+08:00",
        )

        status, payload = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"due_before": "2026-07-20"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in payload["entities"]],  # type: ignore[index]
            ["next-local-date-but-before-cutoff"],
        )

    def test_date_due_is_gtd_local_end_of_day_against_timestamp_cutoff(self) -> None:
        self.write_entity(
            "tasks/a.md", "task", "date-due", status="next", due="2026-07-20"
        )

        _, inclusive = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"due_before": "2026-07-20T14:59:59.999999Z"},
        )
        _, just_before = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"due_before": "2026-07-20T14:59:59.999998Z"},
        )

        self.assertEqual([item["id"] for item in inclusive["entities"]], ["date-due"])  # type: ignore[index]
        self.assertEqual(just_before["entities"], [])

    def test_scheduled_filter_compares_aware_instants_inclusively(self) -> None:
        self.write_entity(
            "tasks/a.md",
            "task",
            "same-instant",
            status="scheduled",
            scheduled_start="2026-07-20T10:00:00+09:00",
            scheduled_end="2026-07-20T11:00:00+09:00",
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "later",
            status="scheduled",
            scheduled_start="2026-07-20T01:00:01Z",
            scheduled_end="2026-07-20T02:00:00Z",
        )

        status, payload = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"scheduled_before": "2026-07-20T01:00:00Z"},
        )

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["entities"]], ["same-instant"])  # type: ignore[index]

    def test_today_filter_uses_tokyo_dates_for_incomplete_and_completed_tasks(self) -> None:
        calendar_identity = {
            "calendar_id": "example@group.calendar.google.com",
            "calendar_event_url": "https://calendar.google.com/calendar/event?eid=YXBp",
            "calendar_event_kind": "all_day",
        }
        self.write_entity(
            "tasks/a.md", "task", "scheduled-today", status="scheduled",
            scheduled_start="2026-07-20T00:30:00+09:00",
        )
        self.write_entity(
            "tasks/b.md", "task", "due-today", status="next", due="2026-07-20"
        )
        self.write_entity(
            "tasks/c.md", "task", "due-tomorrow", status="next", due="2026-07-21"
        )
        self.write_entity(
            "tasks/d.md", "task", "action-today", status="next",
            action_date="2026-07-20", calendar_event_id="event-must-today", **calendar_identity,
        )
        self.write_entity(
            "tasks/e.md", "task", "action-yesterday", status="next",
            action_date="2026-07-19", calendar_event_id="event-must-yesterday", **calendar_identity,
        )
        self.write_entity(
            "tasks/f.md", "task", "action-tomorrow", status="next",
            action_date="2026-07-21", calendar_event_id="event-must-tomorrow", **calendar_identity,
        )
        self.write_entity(
            "tasks/g.md", "task", "action-yesterday-due-today", status="next",
            action_date="2026-07-19", due="2026-07-20",
            calendar_event_id="event-must-yesterday-due-today", **calendar_identity,
        )
        self.write_entity(
            "tasks/h.md", "task", "action-tomorrow-scheduled-today", status="scheduled",
            action_date="2026-07-21", scheduled_start="2026-07-20T00:30:00+09:00",
            calendar_event_id="event-must-tomorrow-scheduled-today", **calendar_identity,
        )
        self.write_entity(
            "tasks/k.md", "task", "completed-today", status="done",
            action_date="2026-07-19", completed_at="2026-07-19T15:30:00Z",
            calendar_event_id="event-completed-today", **calendar_identity,
        )
        self.write_entity(
            "tasks/l.md", "task", "completed-yesterday", status="done",
            action_date="2026-07-19", completed_at="2026-07-19T14:59:59Z",
            calendar_event_id="event-completed-yesterday", **calendar_identity,
        )
        self.write_entity("tasks/m.md", "task", "legacy-done", status="done")
        self.write_entity("tasks/n.md", "task", "doing", status="doing")

        self.assertEqual(validate_repository(self.root), [])

        with mock.patch.object(api, "_gtd_today", return_value=datetime.date(2026, 7, 20)):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot", {"today": "1"})

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in payload["entities"]],  # type: ignore[index]
            [
                "scheduled-today",
                "due-today",
                "action-today",
                "action-yesterday-due-today",
                "action-tomorrow-scheduled-today",
                "completed-today",
            ],
        )
        for value in ("0", "true", "01"):
            with self.subTest(value=value):
                self.assert_error(
                    handle(self.store, "GET", "/api/v1/snapshot", {"today": value}),
                    400,
                    "invalid_query",
                )

    def test_today_filter_includes_only_incomplete_continuations_created_today(self) -> None:
        self.write_entity("tasks/a.md", "task", "completed-today-source", status="done")
        self.write_entity("tasks/b.md", "task", "completed-yesterday-source", status="done")
        self.write_entity(
            "tasks/c.md",
            "task",
            "continuation-created-today",
            status="next",
            continuation_of="completed-today-source",
            created_at="2026-07-19T15:00:00Z",
        )
        self.write_entity(
            "tasks/d.md",
            "task",
            "continuation-created-yesterday",
            status="next",
            continuation_of="completed-yesterday-source",
            created_at="2026-07-19T14:59:59Z",
        )
        self.write_entity("tasks/e.md", "task", "dateless-next", status="next")
        self.write_entity("tasks/f.md", "task", "dateless-done", status="done")

        with mock.patch.object(api, "_gtd_today", return_value=datetime.date(2026, 7, 20)):
            status, payload = handle(self.store, "GET", "/api/v1/snapshot", {"today": "1"})

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in payload["entities"]],  # type: ignore[index]
            ["continuation-created-today"],
        )

    def test_today_filter_ignores_extreme_aware_timestamps_without_overflow(self) -> None:
        self.write_entity(
            "tasks/a.md",
            "task",
            "minimum-scheduled-start",
            status="scheduled",
            scheduled_start="0001-01-01T00:00:00+14:00",
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "maximum-completed-at",
            status="done",
            completed_at="9999-12-31T23:59:59-12:00",
        )
        self.write_entity(
            "tasks/c.md", "task", "due-today", status="next", due="2026-07-20"
        )

        with mock.patch.object(api, "_gtd_today", return_value=datetime.date(2026, 7, 20)):
            status, payload = handle(
                self.store, "GET", "/api/v1/snapshot", {"today": "1"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in payload["entities"]],  # type: ignore[index]
            ["due-today"],
        )

    def test_datetime_query_endpoints_are_deterministic_without_overflow(self) -> None:
        valid_queries = (
            {"due_before": "0001-01-01T00:00:00+14:00"},
            {"due_before": "9999-12-31T23:59:59-12:00"},
            {"scheduled_before": "0001-01-01T00:00:00+14:00"},
            {"scheduled_before": "9999-12-31T23:59:59-12:00"},
        )
        for query in valid_queries:
            with self.subTest(query=query):
                status, payload = handle(
                    self.store, "GET", "/api/v1/snapshot", query
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["entities"], [])

        invalid_queries = (
            {"due_before": "0000-01-01T00:00:00+00:00"},
            {"due_before": "9999-13-31T23:59:59+00:00"},
            {"scheduled_before": "0000-01-01T00:00:00+00:00"},
            {"scheduled_before": "9999-12-32T23:59:59+00:00"},
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                self.assert_error(
                    handle(self.store, "GET", "/api/v1/snapshot", query),
                    400,
                    "invalid_query",
                )

    def test_canonical_datetime_endpoints_compare_without_overflow(self) -> None:
        self.write_entity(
            "tasks/a.md",
            "task",
            "minimum-endpoint",
            status="scheduled",
            due="0001-01-01T00:00:00+14:00",
            scheduled_start="0001-01-01T00:00:00+14:00",
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "maximum-endpoint",
            status="scheduled",
            due="9999-12-31T23:59:59-12:00",
            scheduled_start="9999-12-31T23:59:59-12:00",
        )

        for key, cutoff, expected_ids in (
            (
                "due_before",
                "0001-01-01T00:00:00+14:00",
                ["minimum-endpoint"],
            ),
            (
                "due_before",
                "9999-12-31T23:59:59-12:00",
                ["minimum-endpoint", "maximum-endpoint"],
            ),
            (
                "scheduled_before",
                "0001-01-01T00:00:00+14:00",
                ["minimum-endpoint"],
            ),
            (
                "scheduled_before",
                "9999-12-31T23:59:59-12:00",
                ["minimum-endpoint", "maximum-endpoint"],
            ),
        ):
            with self.subTest(key=key, cutoff=cutoff):
                status, payload = handle(
                    self.store,
                    "GET",
                    "/api/v1/snapshot",
                    {key: cutoff},
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    [item["id"] for item in payload["entities"]],  # type: ignore[index]
                    expected_ids,
                )

    def test_max_minutes_compares_arbitrarily_long_ascii_integers_safely(self) -> None:
        self.write_entity(
            "tasks/a.md", "task", "ordinary", status="next", estimated_minutes="15"
        )
        self.write_entity(
            "tasks/b.md",
            "task",
            "huge-estimate",
            status="next",
            estimated_minutes="9" * 5000,
        )

        status, huge_cutoff = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"max_minutes": "9" * 5000},
        )
        _, larger_cutoff = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            {"max_minutes": "1" + "0" * 5000},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in huge_cutoff["entities"]],  # type: ignore[index]
            ["ordinary", "huge-estimate"],
        )
        self.assertEqual(
            [item["id"] for item in larger_cutoff["entities"]],  # type: ignore[index]
            ["ordinary", "huge-estimate"],
        )

    def test_invalid_duplicate_and_unknown_queries_are_rejected(self) -> None:
        invalid_queries: tuple[object, ...] = (
            {"unknown": "x"},
            {"status": ["next", "doing"]},
            {"status": "   "},
            {"project_id": "../project"},
            {"goal_id": "goal/id"},
            {"area_id": "area/id"},
            {"context": "home,phone"},
            {"context": "home phone"},
            {"context": "[home]"},
            {"max_minutes": "0"},
            {"max_minutes": "01"},
            {"max_minutes": "１"},
            {"due_before": "2026-02-30"},
            {"due_before": "2026-07-20T10:00:00"},
            {"scheduled_before": "2026-07-20"},
            {"scheduled_before": "2026-07-20T10:00:00"},
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                self.assert_error(
                    handle(self.store, "GET", "/api/v1/snapshot", query),  # type: ignore[arg-type]
                    400,
                    "invalid_query",
                )

    def test_entity_detail_supports_all_kinds_and_archived_entities(self) -> None:
        self.write_entity("tasks/t.md", "task", "task-one", status="next", body="Task body\n")
        self.write_entity(
            "purposes/u.md", "purpose", "purpose-one", body=PURPOSE_BODY
        )
        self.write_entity("visions/v.md", "vision", "vision-one", body="Vision body\n")
        self.write_entity("areas/a.md", "area", "area-one", body="Area body\n")
        self.write_entity("projects/p.md", "project", "project-one", body="Project body\n")
        self.write_entity("goals/g.md", "goal", "goal-one", body="Goal body\n")
        self.write_entity(
            "reviews/weekly/r.md",
            "review",
            "review-one",
            review_kind="weekly",
            body="Review body\n",
        )
        self.write_entity("archive/a.md", "task", "task-old", status="done", body="Old body\n")

        cases = (
            ("tasks", "task-one", False),
            ("purposes", "purpose-one", False),
            ("visions", "vision-one", False),
            ("areas", "area-one", False),
            ("projects", "project-one", False),
            ("goals", "goal-one", False),
            ("reviews", "review-one", False),
            ("tasks", "task-old", True),
        )
        for kind, entity_id, archived in cases:
            with self.subTest(kind=kind, entity_id=entity_id):
                status, payload = handle(
                    self.store, "GET", f"/api/v1/entities/{kind}/{entity_id}"
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["id"], entity_id)
                self.assertEqual(payload["kind"], kind)
                self.assertEqual(payload["archived"], archived)
                self.assertIn("body", payload)
                self.assertRegex(payload["content_hash"], r"^[0-9a-f]{64}$")  # type: ignore[arg-type]

    def test_entity_kind_mismatch_and_unknown_id_are_generic_not_found(self) -> None:
        self.write_entity("projects/p.md", "project", "shared-id")

        mismatch = handle(self.store, "GET", "/api/v1/entities/tasks/shared-id")
        unknown = handle(self.store, "GET", "/api/v1/entities/tasks/unknown-id")

        self.assertEqual(mismatch, unknown)
        self.assert_error(mismatch, 404, "not_found")
        self.assertNotIn("project", json.dumps(mismatch))

    def test_invalid_entity_routes_and_unknown_paths_are_distinguished(self) -> None:
        invalid_paths = (
            "/api/v1/entities/tasks/..",
            "/api/v1/entities/tasks/%2e%2e%2fx",
            "/api/v1/entities/tasks/task-one/extra",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                self.assert_error(handle(self.store, "GET", path), 400, "invalid_request")

        for path in ("/api/v1/nope", "/api/v1/entities", "/not-api"):
            with self.subTest(path=path):
                self.assert_error(handle(self.store, "GET", path), 404, "not_found")

    def test_method_and_non_snapshot_query_rules_are_enforced(self) -> None:
        self.write_entity("tasks/t.md", "task", "task-one", status="next")
        for path in (
            "/api/v1/health",
            "/api/v1/snapshot",
            "/api/v1/entities/tasks/task-one",
        ):
            with self.subTest(path=path):
                self.assert_error(handle(self.store, "POST", path), 405, "method_not_allowed")
        self.assert_error(
            handle(self.store, "GET", "/api/v1/health", {"x": "1"}),
            400,
            "invalid_query",
        )
        self.assert_error(
            handle(
                self.store,
                "GET",
                "/api/v1/entities/tasks/task-one",
                {"status": "next"},
            ),
            400,
            "invalid_query",
        )

    def test_get_ignores_headers_and_body_without_decoding_them(self) -> None:
        expected = handle(self.store, "GET", "/api/v1/health")
        actual = handle(
            self.store,
            "GET",
            "/api/v1/health",
            headers={"Origin": object()},
            body=b"\xffnot-json/private-data",
        )
        self.assertEqual(actual, expected)

    def test_returned_frontmatter_is_copied(self) -> None:
        frontmatter = {
            "id": "task-one",
            "type": "task",
            "title": "Original",
            "status": "next",
        }
        entity = Entity(
            entity_id="task-one",
            entity_type="task",
            relative_path="tasks/t.md",
            frontmatter=frontmatter,
            body="Body\n",
            content_hash="0" * 64,
        )

        class FixtureStore:
            def get_entity(self, entity_id: str) -> Entity:
                return entity

        _, payload = handle(
            FixtureStore(), "GET", "/api/v1/entities/tasks/task-one"  # type: ignore[arg-type]
        )
        payload["frontmatter"]["title"] = "Changed"  # type: ignore[index]

        self.assertEqual(entity.frontmatter["title"], "Original")

    def test_all_success_and_error_payloads_are_json_primitives(self) -> None:
        self.write_entity("tasks/t.md", "task", "task-one", status="next")
        payloads = (
            handle(self.store, "GET", "/api/v1/health")[1],
            handle(self.store, "GET", "/api/v1/snapshot")[1],
            handle(self.store, "GET", "/api/v1/entities/tasks/task-one")[1],
            handle(self.store, "GET", "/api/v1/nope")[1],
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                encoded = json.dumps(payload)
                self.assertEqual(json.loads(encoded), payload)
                self.assertNotIn("PosixPath", repr(payload))
                self.assertNotIn("Entity(", repr(payload))


class MutationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = pathlib.Path(self.temporary_directory.name)
        for directory in (
            "inbox",
            "tasks",
            "purposes",
            "visions",
            "areas",
            "projects",
            "goals",
            "reviews/daily",
            "reviews/weekly",
            "archive",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root)
        self.headers = {
            "Origin": "http://127.0.0.1:24873",
            "X-GTD-Web": "1",
            "Content-Type": "application/json; charset=utf-8",
        }

    def test_api_state_is_available_for_long_lived_server(self) -> None:
        self.assertIsNotNone(ApiState)

    def test_expand_token_registry_enforces_action_ttl_capacity_and_store_binding(self) -> None:
        now = [100.0]
        state = ApiState(
            self.store, clock=lambda: now[0], ttl_seconds=10, max_previews=1
        )
        first = "a" * 64
        second = "b" * 64
        legacy = "c" * 64
        state.register(
            self.store, mock.sentinel.first_plan, b"first",
            {"action": "resource_allocation_expand"}, first,
        )

        with tempfile.TemporaryDirectory() as other_directory:
            other_root = pathlib.Path(other_directory)
            for directory in (
                "inbox", "tasks", "projects", "goals",
                "reviews/daily", "reviews/weekly", "archive",
            ):
                (other_root / directory).mkdir(parents=True, exist_ok=True)
            self.assertIsNone(
                state.consume_token(Store(other_root), first)
            )
        self.assertEqual(
            state.token_action(self.store, first), "resource_allocation_expand"
        )

        state.register(
            self.store, mock.sentinel.second_plan, b"second",
            {"action": "resource_allocation_expand"}, second,
        )
        self.assertIsNone(state.consume_token(self.store, first))
        self.assertIs(
            state.consume_token(self.store, second).plan,
            mock.sentinel.second_plan,
        )
        self.assertIsNone(state.consume_token(self.store, second))

        state.register(
            self.store, mock.sentinel.legacy_plan, b"legacy",
            {"action": "create"}, legacy,
        )
        self.assertEqual(state.token_action(self.store, legacy), "create")
        self.assertIsNone(state.consume_token(self.store, legacy))
        self.assertEqual(state.token_action(self.store, legacy), "create")
        now[0] = 111.0
        self.assertIsNone(state.token_action(self.store, legacy))

    def body(self, payload: object) -> bytes:
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def preview(
        self,
        payload: object,
        *,
        store: Store | None = None,
        state: object | None = None,
    ) -> tuple[int, dict[str, object]]:
        return handle(
            store or self.store,
            "POST",
            "/api/v1/mutations/preview",
            headers=self.headers,
            body=self.body(payload),
            state=state,
        )

    def apply(
        self,
        preview: dict[str, object],
        *,
        store: Store | None = None,
        state: object | None = None,
    ) -> tuple[int, dict[str, object]]:
        return handle(
            store or self.store,
            "POST",
            "/api/v1/mutations/apply",
            headers=self.headers,
            body=self.body(
                {
                    "preview": {
                        key: value
                        for key, value in preview.items()
                        if key != "preview_hash"
                    },
                    "preview_hash": preview["preview_hash"],
                }
            ),
            state=state,
        )

    def assert_error(
        self, result: tuple[int, dict[str, object]], status: int, code: str
    ) -> dict[str, object]:
        actual_status, payload = result
        self.assertEqual(actual_status, status)
        self.assertEqual(payload["error"]["code"], code)  # type: ignore[index]
        self.assertIsInstance(payload["error"]["details"], list)  # type: ignore[index]
        return payload

    def write_task(
        self,
        *,
        entity_id: str = "task-20260718-001",
        status: str = "next",
        body: str = "Before\n",
        title: str = "Before",
        **fields: str,
    ) -> Entity:
        timestamp = "2026-07-18T09:00:00+09:00"
        frontmatter = {
            "id": entity_id,
            "type": "task",
            "title": title,
            "status": status,
            "created_at": timestamp,
            "updated_at": timestamp,
            **fields,
        }
        directory = "inbox" if status == "inbox" else "tasks"
        path = self.root / directory / f"{entity_id}.md"
        path.write_text(
            serialize_frontmatter(frontmatter, "task") + body,
            encoding="utf-8",
        )
        return self.store.get_entity(entity_id)

    def write_project(
        self,
        *,
        entity_id: str = "project-20260718-001",
        status: str = "not_started",
        title: str = "Project",
        kanban_position: str | None = None,
    ) -> Entity:
        timestamp = "2026-07-18T09:00:00+09:00"
        frontmatter = {
            "id": entity_id,
            "type": "project",
            "title": title,
            "status": status,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        path = self.root / "projects" / f"{entity_id}.md"
        document = serialize_frontmatter(frontmatter, "project")
        if kanban_position is not None:
            document = document.replace(
                "\n---\n", f"\nkanban_position: {kanban_position}\n---\n", 1
            )
        path.write_text(
            document + "Project body\n",
            encoding="utf-8",
        )
        return self.store.get_entity(entity_id)

    def test_project_move_preview_and_apply_expose_exact_atomic_order_effects(self) -> None:
        """Catches the API splitting a canonical Project reorder or losing readback."""
        first = self.write_project(
            entity_id="project-kanban-first", kanban_position="1"
        )
        second = self.write_project(
            entity_id="project-kanban-second", kanban_position="2"
        )
        operation = {
            "action": "project_move",
            "kind": "projects",
            "id": second.entity_id,
            "base_hash": second.content_hash,
            "status": "not_started",
            "position": 1,
        }

        preview_status, preview = self.preview(operation)

        self.assertEqual(preview_status, 200, preview)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["project_moved", "project_reordered"],
        )
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200, applied)
        self.assertEqual(
            [effect["frontmatter"]["kanban_position"] for effect in applied["effects"]],  # type: ignore[index]
            ["1", "2"],
        )
        self.assertEqual(
            self.store.get_entity(second.entity_id).frontmatter["kanban_position"],
            "1",
        )
        self.assertEqual(
            self.store.get_entity(first.entity_id).frontmatter["kanban_position"],
            "2",
        )

    def test_project_create_normalizes_legacy_lane_through_atomic_api_workflow(self) -> None:
        """Catches the API flattening an order-aware Project create."""
        first = self.write_project(entity_id="project-legacy-a")
        second = self.write_project(entity_id="project-legacy-b")
        status, preview = self.preview(
            {
                "action": "create",
                "kind": "projects",
                "fields": {"title": "New project"},
                "body": "Body",
            }
        )

        self.assertEqual(status, 200, preview)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["project_created", "project_reordered", "project_reordered"],
        )
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200, applied)
        self.assertEqual(
            [effect["frontmatter"]["kanban_position"] for effect in applied["effects"]],  # type: ignore[index]
            ["3", "1", "2"],
        )
        self.assertEqual(
            self.store.get_entity(first.entity_id).frontmatter["kanban_position"],
            "1",
        )
        self.assertEqual(
            self.store.get_entity(second.entity_id).frontmatter["kanban_position"],
            "2",
        )

    def test_project_planned_period_uses_existing_preview_apply_and_clear_contract(self) -> None:
        """Catches API CRUD accepting only a complete estimated Project period."""
        created_status, create_preview = self.preview(
            {
                "action": "create",
                "kind": "projects",
                "fields": {
                    "title": "Timed project",
                    "planned_start_date": "2026-09-01",
                    "planned_end_date": "2026-09-30",
                },
                "body": "Body",
            }
        )
        self.assertEqual(created_status, 200, create_preview)
        applied_status, created = self.apply(create_preview)
        self.assertEqual(applied_status, 201, created)
        self.assertEqual(created["frontmatter"]["planned_start_date"], "2026-09-01")  # type: ignore[index]
        self.assertEqual(created["frontmatter"]["planned_end_date"], "2026-09-30")  # type: ignore[index]

        update_status, clear_preview = self.preview(
            {
                "action": "update",
                "kind": "projects",
                "id": created["id"],
                "base_hash": created["content_hash"],
                "fields": {"planned_start_date": "", "planned_end_date": ""},
            }
        )
        self.assertEqual(update_status, 200, clear_preview)
        cleared_status, cleared = self.apply(clear_preview)
        self.assertEqual(cleared_status, 200, cleared)
        self.assertNotIn("planned_start_date", cleared["frontmatter"])  # type: ignore[index]
        self.assertNotIn("planned_end_date", cleared["frontmatter"])  # type: ignore[index]

        invalid_status, invalid = self.preview(
            {
                "action": "create",
                "kind": "projects",
                "fields": {"title": "Incomplete", "planned_start_date": "2026-09-01"},
                "body": "Body",
            }
        )
        self.assertEqual(invalid_status, 400, invalid)
        self.assertEqual(invalid["error"]["code"], "invalid_request")  # type: ignore[index]

    def test_project_move_rejects_inexact_or_invalid_requests(self) -> None:
        """Catches unknown fields, wrong kinds, legacy status writes, and bad positions."""
        project = self.write_project(kanban_position="1")
        valid = {
            "action": "project_move",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "status": "doing",
            "position": 1,
        }
        invalid_requests = (
            {**valid, "extra": True},
            {key: value for key, value in valid.items() if key != "position"},
            {**valid, "kind": "tasks"},
            {**valid, "status": "active"},
            {**valid, "status": "blocked"},
            {**valid, "position": 0},
            {**valid, "position": True},
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                self.assert_error(self.preview(request), 400, "invalid_request")

    def test_project_transition_invariants_return_stable_errors(self) -> None:
        project = self.write_project(status="doing")
        self.write_task(entity_id="task-project-next", project_id=project.entity_id)
        self.assert_error(
            self.preview(
                {
                    "action": "update",
                    "kind": "projects",
                    "id": project.entity_id,
                    "base_hash": project.content_hash,
                    "fields": {"status": "completed"},
                }
            ),
            409,
            "unfinished_project_tasks",
        )

        doing = self.write_task(
            entity_id="task-project-doing",
            status="doing",
            project_id=project.entity_id,
        )
        self.assertEqual(doing.frontmatter["status"], "doing")
        self.assert_error(
            self.preview(
                {
                    "action": "update",
                    "kind": "projects",
                    "id": project.entity_id,
                    "base_hash": project.content_hash,
                    "fields": {"status": "on_hold"},
                }
            ),
            409,
            "project_has_doing_task",
        )

    def test_project_transition_apply_rechecks_concurrent_child(self) -> None:
        project = self.write_project(status="doing")
        status, preview = self.preview(
            {
                "action": "update",
                "kind": "projects",
                "id": project.entity_id,
                "base_hash": project.content_hash,
                "fields": {"status": "completed"},
            }
        )
        self.assertEqual(status, 200)
        self.store.create_entity(
            "task",
            {"title": "Concurrent child", "project_id": project.entity_id},
            "Body",
        )

        self.assert_error(self.apply(preview), 409, "unfinished_project_tasks")
        self.assertEqual(
            self.store.get_entity(project.entity_id).frontmatter["status"], "doing"
        )

    def test_task_link_to_terminal_project_and_regular_start_update_are_rejected(self) -> None:
        terminal = self.write_project(status="completed")
        self.assert_error(
            self.preview(
                {
                    "action": "create",
                    "kind": "tasks",
                    "fields": {"title": "Blocked", "project_id": terminal.entity_id},
                    "body": "",
                }
            ),
            409,
            "project_not_executable",
        )
        task = self.write_task(entity_id="task-regular-start")
        self.assert_error(
            self.preview(
                {
                    "action": "update",
                    "kind": "tasks",
                    "id": task.entity_id,
                    "base_hash": task.content_hash,
                    "fields": {"status": "doing"},
                }
            ),
            400,
            "invalid_request",
        )

        doing = self.write_task(entity_id="task-existing-doing", status="doing")
        status, edit = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": doing.entity_id,
                "base_hash": doing.content_hash,
                "fields": {"title": "Edited while doing"},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.apply(edit)[0], 200)

    def test_generic_task_create_rejects_doing_status(self) -> None:
        self.assert_error(
            self.preview(
                {
                    "action": "create",
                    "kind": "tasks",
                    "fields": {"title": "Bypass", "status": "doing"},
                    "body": "",
                }
            ),
            400,
            "invalid_request",
        )

    def test_task_start_rejects_non_executable_project(self) -> None:
        project = self.write_project(status="on_hold")
        task = self.write_task(entity_id="task-held-project", project_id=project.entity_id)

        self.assert_error(
            self.preview(
                {
                    "action": "start",
                    "kind": "tasks",
                    "id": task.entity_id,
                    "base_hash": task.content_hash,
                }
            ),
            409,
            "project_not_executable",
        )

    def test_linked_task_start_atomically_starts_project_and_stale_project_blocks_all_effects(self) -> None:
        project = self.write_project(status="not_started")
        task = self.write_task(entity_id="task-linked-start", project_id=project.entity_id)
        operation = {
            "action": "start",
            "kind": "tasks",
            "id": task.entity_id,
            "base_hash": task.content_hash,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["project_started", "started"],
        )
        self.assertEqual(
            preview["effects"][0]["proposed"]["frontmatter"]["status"],  # type: ignore[index]
            "doing",
        )
        self.assertEqual(
            preview["effects"][1]["proposed"]["frontmatter"]["status"],  # type: ignore[index]
            "doing",
        )

        update_status, update = self.preview(
            {
                "action": "update",
                "kind": "projects",
                "id": project.entity_id,
                "base_hash": project.content_hash,
                "fields": {"title": "Changed after start preview"},
            }
        )
        self.assertEqual(update_status, 200)
        self.assertEqual(self.apply(update)[0], 200)

        self.assert_error(self.apply(preview), 409, "conflict")
        self.assertEqual(self.store.get_entity(task.entity_id).frontmatter["status"], "next")
        self.assertEqual(
            self.store.get_entity(project.entity_id).frontmatter["status"],
            "not_started",
        )

    def test_linked_task_switch_includes_project_start_in_the_same_effect_order(self) -> None:
        project = self.write_project(status="not_started")
        active = self.write_task(entity_id="task-switch-active", status="doing")
        target = self.write_task(
            entity_id="task-switch-linked", project_id=project.entity_id
        )

        status, preview = self.preview(
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": active.entity_id,
                    "base_hash": active.content_hash,
                    "action": "complete",
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["completed", "project_started", "started"],
        )
        self.assertEqual(self.apply(preview)[0], 200)
        self.assertEqual(
            self.store.get_entity(project.entity_id).frontmatter["status"], "doing"
        )
        self.assertEqual(
            self.store.get_entity(target.entity_id).frontmatter["status"], "doing"
        )

    def test_project_task_plan_create_orders_dag_generates_ids_and_applies_atomically(self) -> None:
        project = self.write_project(status="not_started")
        operation = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "tasks": [
                {"key": "ship", "title": "Ship", "depends_on_keys": ["build"]},
                {"key": "setup", "title": "Setup", "depends_on_keys": []},
                {"key": "build", "title": "Build", "depends_on_keys": ["setup"]},
            ],
        }
        fixed_now = datetime.datetime(
            2026,
            8,
            28,
            9,
            0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )

        with mock.patch("webapp.store.current_time", return_value=fixed_now):
            status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        effects = preview["effects"]
        self.assertEqual(
            [effect["role"] for effect in effects],  # type: ignore[index]
            ["task_created", "task_created", "task_created"],
        )
        self.assertEqual(
            [effect["proposed"]["frontmatter"]["title"] for effect in effects],  # type: ignore[index]
            ["Setup", "Build", "Ship"],
        )
        self.assertEqual(
            [effect["proposed"]["id"] for effect in effects],  # type: ignore[index]
            [
                "task-20260828-001",
                "task-20260828-002",
                "task-20260828-003",
            ],
        )
        self.assertEqual(
            [effect["proposed"]["frontmatter"]["status"] for effect in effects],  # type: ignore[index]
            ["next", "waiting", "waiting"],
        )
        self.assertNotIn("depends_on", effects[0]["proposed"]["frontmatter"])  # type: ignore[index]
        self.assertEqual(
            effects[1]["proposed"]["frontmatter"]["depends_on"],  # type: ignore[index]
            "[task-20260828-001]",
        )
        self.assertEqual(
            effects[2]["proposed"]["frontmatter"]["depends_on"],  # type: ignore[index]
            "[task-20260828-002]",
        )

        apply_status, applied = self.apply(preview)
        self.assertEqual(apply_status, 200)
        self.assertEqual(
            [effect["role"] for effect in applied["effects"]],  # type: ignore[index]
            ["task_created", "task_created", "task_created"],
        )
        self.assertEqual(validate_repository(self.root), [])

    def test_project_task_plan_create_tree_mode_previews_forest_contract(self) -> None:
        """Catches tree mode using Waiting or losing root/child sibling positions."""
        project = self.write_project(status="not_started")
        operation = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "mode": "tree",
            "tasks": [
                {"key": "a", "title": "Root A", "parent_key": None},
                {"key": "child", "title": "Child", "parent_key": "a"},
                {"key": "b", "title": "Root B", "parent_key": None},
            ],
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["operation"], operation)
        proposed = [
            effect["proposed"]["frontmatter"]
            for effect in preview["effects"]  # type: ignore[index]
        ]
        self.assertEqual([item["status"] for item in proposed], ["next", "planned", "next"])
        self.assertEqual([item["project_position"] for item in proposed], ["1", "1", "2"])
        self.assertEqual(proposed[1]["depends_on"], f"[{proposed[0]['id']}]")
        self.assertEqual(self.apply(preview)[0], 200)

    def test_project_task_plan_update_previews_and_applies_one_atomic_effect_set(self) -> None:
        """Catches the editor save bypassing strict preview/apply normalization."""
        project = self.write_project(status="not_started")
        root = self.write_task(
            entity_id="task-plan-update-root",
            title="Root",
            project_id=project.entity_id,
            project_position="1",
        )
        archived = self.write_task(
            entity_id="task-plan-update-archive",
            title="Archive",
            status="planned",
            project_id=project.entity_id,
            project_position="2",
        )
        operation = {
            "action": "project_task_plan_update",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "nodes": [
                {
                    "key": root.entity_id,
                    "id": root.entity_id,
                    "base_hash": root.content_hash,
                    "title": "Root renamed",
                    "parent_key": None,
                    "status": "planned",
                },
                {
                    "key": "client-child",
                    "title": "Child",
                    "parent_key": root.entity_id,
                    "status": "planned",
                },
            ],
            "archives": [
                {"id": archived.entity_id, "base_hash": archived.content_hash}
            ],
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["task_updated", "task_created", "task_archived"],
        )
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200, applied)
        self.assertEqual(
            [effect["role"] for effect in applied["effects"]],  # type: ignore[index]
            ["task_updated", "task_created", "task_archived"],
        )
        self.assertEqual(validate_repository(self.root), [])

    def test_project_task_plan_update_apply_rejects_tree_membership_drift(self) -> None:
        """Catches an apply overwriting a concurrently changed complete tree snapshot."""
        project = self.write_project(status="not_started")
        root = self.write_task(
            entity_id="task-plan-drift-root",
            title="Root",
            project_id=project.entity_id,
            project_position="1",
        )
        status, preview = self.preview(
            {
                "action": "project_task_plan_update",
                "kind": "projects",
                "id": project.entity_id,
                "base_hash": project.content_hash,
                "nodes": [
                    {
                        "key": root.entity_id,
                        "id": root.entity_id,
                        "base_hash": root.content_hash,
                        "title": "Renamed",
                        "parent_key": None,
                        "status": "next",
                    }
                ],
                "archives": [],
            }
        )
        self.assertEqual(status, 200, preview)
        self.write_task(
            entity_id="task-plan-drift-concurrent",
            title="Concurrent",
            status="planned",
            project_id=project.entity_id,
            project_position="2",
        )

        self.assert_error(self.apply(preview), 409, "conflict")
        self.assertEqual(self.store.get_entity(root.entity_id).frontmatter["title"], "Root")

    def test_project_task_plan_update_rejects_invalid_graph_fixed_tasks_and_incomplete_snapshot(self) -> None:
        """Catches full-tree validation accepting ambiguous or mutable fixed nodes."""
        project = self.write_project(status="doing")
        root = self.write_task(
            entity_id="task-plan-invalid-root", title="Root", status="next",
            project_id=project.entity_id, project_position="1",
        )
        running = self.write_task(
            entity_id="task-plan-invalid-running", title="Running", status="doing",
            project_id=project.entity_id, depends_on=f"[{root.entity_id}]",
            project_position="1",
        )
        done = self.write_task(
            entity_id="task-plan-invalid-done", title="Done", status="done",
            project_id=project.entity_id, project_position="2",
        )
        outside_project = self.write_project(
            entity_id="project-plan-invalid-outside", status="not_started"
        )
        outside = self.write_task(
            entity_id="task-plan-invalid-outside", title="Outside", status="next",
            project_id=outside_project.entity_id, project_position="1",
        )
        root_node = {
            "key": root.entity_id, "id": root.entity_id,
            "base_hash": root.content_hash, "title": "Root",
            "parent_key": None, "status": "next",
        }
        running_node = {
            "key": running.entity_id, "id": running.entity_id,
            "base_hash": running.content_hash, "title": "Running",
            "parent_key": root.entity_id, "status": "doing",
        }
        base = {
            "action": "project_task_plan_update", "kind": "projects",
            "id": project.entity_id, "base_hash": project.content_hash,
            "archives": [],
        }
        invalid_400 = (
            {**base, "nodes": [root_node, {**running_node, "key": root.entity_id}]},
            {**base, "nodes": [{**root_node, "parent_key": running.entity_id, "status": "planned"}, running_node]},
            {**base, "nodes": [{**root_node, "status": "doing"}, running_node]},
            {**base, "nodes": [root_node, {**running_node, "title": "Changed", "status": "doing"}]},
            {**base, "nodes": [root_node], "archives": [{"id": running.entity_id, "base_hash": running.content_hash}]},
            {**base, "nodes": [root_node, running_node], "archives": [{"id": done.entity_id, "base_hash": done.content_hash}]},
            {**base, "nodes": [root_node, running_node, {
                "key": outside.entity_id, "id": outside.entity_id,
                "base_hash": outside.content_hash, "title": "Outside",
                "parent_key": None, "status": "next",
            }]},
        )
        for operation in invalid_400:
            with self.subTest(operation=operation):
                self.assert_error(self.preview(operation), 400, "invalid_request")
        self.assert_error(
            self.preview({**base, "nodes": [root_node]}), 409, "conflict"
        )
        self.assert_error(
            self.preview(
                {**base, "nodes": [{**root_node, "base_hash": "0" * 64}, running_node]}
            ),
            409,
            "conflict",
        )

    def test_project_task_plan_create_tree_mode_accepts_a_planned_root(self) -> None:
        """Catches an explicit planned root being coerced to Next or rejected."""
        project = self.write_project(status="not_started")
        operation = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "mode": "tree",
            "tasks": [
                {"key": "root", "title": "Support root", "parent_key": None, "status": "planned"},
                {"key": "child", "title": "Support child", "parent_key": "root"},
            ],
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200, preview)
        proposed = [
            effect["proposed"]["frontmatter"]
            for effect in preview["effects"]  # type: ignore[index]
        ]
        self.assertEqual([item["status"] for item in proposed], ["planned", "planned"])
        self.assertNotIn("depends_on", proposed[0])
        self.assertEqual(proposed[1]["depends_on"], f"[{proposed[0]['id']}]")

    def test_project_task_plan_create_tree_mode_accepts_an_explicit_next_root(self) -> None:
        """Catches a supported explicit Next root being rejected by the API contract."""
        project = self.write_project(status="not_started")
        operation = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "mode": "tree",
            "tasks": [
                {"key": "root", "title": "Support root", "parent_key": None, "status": "next"},
                {"key": "child", "title": "Support child", "parent_key": "root"},
            ],
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200, preview)
        proposed = [
            effect["proposed"]["frontmatter"]
            for effect in preview["effects"]  # type: ignore[index]
        ]
        self.assertEqual([item["status"] for item in proposed], ["next", "planned"])

    def test_project_task_plan_create_tree_mode_rejects_invalid_root_status_shapes(self) -> None:
        """Catches status being accepted on children or outside the supported root contract."""
        project = self.write_project(status="not_started")
        base = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "mode": "tree",
        }
        invalid_tasks = (
            [{"key": "root", "title": "Root", "parent_key": None, "status": "unknown"}],
            [
                {"key": "root", "title": "Root", "parent_key": None},
                {"key": "child", "title": "Child", "parent_key": "root", "status": "planned"},
            ],
            [{"key": "root", "title": "Root", "parent_key": None, "status": None}],
        )
        for tasks in invalid_tasks:
            with self.subTest(tasks=tasks):
                self.assert_error(
                    self.preview({**base, "tasks": tasks}), 400, "invalid_request"
                )

    def test_project_task_plan_create_rejects_invalid_graph_and_request_shape(self) -> None:
        project = self.write_project(status="doing")
        base = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
        }
        invalid_tasks = (
            [],
            [{"key": "one", "title": "One", "depends_on_keys": ["missing"]}],
            [{"key": "one", "title": "One", "depends_on_keys": ["one"]}],
            [
                {"key": "one", "title": "One", "depends_on_keys": ["two"]},
                {"key": "two", "title": "Two", "depends_on_keys": ["one"]},
            ],
            [
                {"key": "one", "title": "One", "depends_on_keys": []},
                {"key": "one", "title": "Duplicate", "depends_on_keys": []},
            ],
            [{"key": "one", "title": " ", "depends_on_keys": []}],
            [{"key": "one", "title": "One", "depends_on_keys": [], "extra": True}],
        )
        for tasks in invalid_tasks:
            with self.subTest(tasks=tasks):
                self.assert_error(
                    self.preview({**base, "tasks": tasks}),
                    400,
                    "invalid_request",
                )
        for invalid in (
            {**base, "tasks": "not-a-list"},
            {**base, "tasks": [], "extra": True},
            {**base, "kind": "tasks", "tasks": []},
        ):
            with self.subTest(request=invalid):
                self.assert_error(self.preview(invalid), 400, "invalid_request")

    def test_project_task_plan_create_collision_publishes_no_planned_task(self) -> None:
        project = self.write_project(status="not_started")
        fixed_now = datetime.datetime(
            2026,
            8,
            28,
            9,
            0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )
        operation = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": project.entity_id,
            "base_hash": project.content_hash,
            "tasks": [
                {"key": "one", "title": "One", "depends_on_keys": []},
                {"key": "two", "title": "Two", "depends_on_keys": ["one"]},
            ],
        }
        with mock.patch("webapp.store.current_time", return_value=fixed_now):
            status, preview = self.preview(operation)
        self.assertEqual(status, 200)
        planned_ids = [
            effect["proposed"]["id"] for effect in preview["effects"]  # type: ignore[index]
        ]
        collision_path = self.root / preview["effects"][1]["path_change"]["to"]  # type: ignore[index]
        collision_bytes = (
            serialize_frontmatter(
                {
                    "id": "task-external-collision",
                    "type": "task",
                    "title": "External",
                    "status": "next",
                    "created_at": fixed_now.isoformat(timespec="seconds"),
                    "updated_at": fixed_now.isoformat(timespec="seconds"),
                },
                "task",
            )
            + "External\n"
        ).encode("utf-8")
        collision_path.write_bytes(collision_bytes)

        self.assert_error(self.apply(preview), 409, "conflict")
        self.assertEqual(collision_path.read_bytes(), collision_bytes)
        actual_ids = {entity.entity_id for entity in self.store.list_entities()}
        self.assertTrue(set(planned_ids).isdisjoint(actual_ids))

    def test_project_task_move_and_bounded_migration_are_exact_atomic_workflows(self) -> None:
        """Catches route widening, non-normalized reorder, or migration of human Waiting Tasks."""
        project = self.write_project(status="not_started")
        first = self.write_task(
            entity_id="task-project-first",
            status="next",
            project_id=project.entity_id,
            project_position="1",
        )
        second = self.write_task(
            entity_id="task-project-second",
            status="next",
            project_id=project.entity_id,
            project_position="2",
        )
        move = {
            "action": "project_task_move",
            "kind": "tasks",
            "id": second.entity_id,
            "base_hash": second.content_hash,
            "project_id": project.entity_id,
            "status": "next",
            "primary_parent_id": None,
            "position": 1,
        }

        status, move_preview = self.preview(move)

        self.assertEqual(status, 200, move_preview)
        self.assertEqual(move_preview["operation"], move)
        self.assertEqual(
            [effect["role"] for effect in move_preview["effects"]],  # type: ignore[index]
            ["task_moved", "task_reordered"],
        )
        self.assertEqual(self.apply(move_preview)[0], 200)
        self.assertEqual(self.store.get_entity(second.entity_id).frontmatter["project_position"], "1")
        self.assertEqual(self.store.get_entity(first.entity_id).frontmatter["project_position"], "2")

        dependency = self.write_task(
            entity_id="task-project-dependency",
            status="next",
            project_id=project.entity_id,
        )
        eligible = self.write_task(
            entity_id="task-project-eligible",
            status="waiting",
            project_id=project.entity_id,
            depends_on=f"[{dependency.entity_id}]",
        )
        human = self.write_task(
            entity_id="task-project-human",
            status="waiting",
            project_id=project.entity_id,
            depends_on=f"[{dependency.entity_id}]",
            waiting_for="reply",
        )
        current_project = self.store.get_entity(project.entity_id)
        migration = {
            "action": "project_task_plan_migrate",
            "kind": "projects",
            "id": current_project.entity_id,
            "base_hash": current_project.content_hash,
        }

        status, migration_preview = self.preview(migration)

        self.assertEqual(status, 200, migration_preview)
        self.assertEqual(migration_preview["operation"], migration)
        self.assertEqual(
            [
                (effect["role"], effect["proposed"]["id"])
                for effect in migration_preview["effects"]  # type: ignore[index]
            ],
            [("planned_migrated", eligible.entity_id)],
        )
        self.assertEqual(self.apply(migration_preview)[0], 200)
        self.assertEqual(self.store.get_entity(eligible.entity_id).frontmatter["status"], "planned")
        self.assertEqual(self.store.get_entity(human.entity_id).frontmatter["status"], "waiting")

    def test_correct_work_session_preview_and_apply_requires_status_exact_keys(self) -> None:
        done = self.write_task(
            entity_id="task-correct-done",
            status="done",
            work_started_at="2026-07-19T08:00:00+09:00",
            work_ended_at="2026-07-19T08:30:00+09:00",
        )
        operation = {
            "action": "correct_work_session",
            "kind": "tasks",
            "id": done.entity_id,
            "base_hash": done.content_hash,
            "work_started_at": "2026-07-19T08:05:00+09:00",
            "work_ended_at": "2026-07-19T08:35:00+09:00",
        }
        with mock.patch(
            "webapp.store.current_time",
            return_value=datetime.datetime(2026, 7, 19, 9, 8, 7, tzinfo=datetime.timezone(datetime.timedelta(hours=9))),
        ):
            status, preview = self.preview(operation)
        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(self.apply(preview)[0], 200)
        self.assertEqual(
            self.store.get_entity(done.entity_id).frontmatter["work_ended_at"],
            operation["work_ended_at"],
        )

        doing = self.write_task(
            entity_id="task-correct-doing",
            status="doing",
            work_started_at="2026-07-19T08:00:00+09:00",
            resume_status="next",
        )
        invalid = {
            "action": "correct_work_session",
            "kind": "tasks",
            "id": doing.entity_id,
            "base_hash": doing.content_hash,
            "work_started_at": "2026-07-19T08:05:00+09:00",
            "work_ended_at": "2026-07-19T08:35:00+09:00",
        }
        self.assert_error(self.preview(invalid), 400, "invalid_request")

        missing_done_end = {key: value for key, value in operation.items() if key != "work_ended_at"}
        self.assert_error(self.preview(missing_done_end), 400, "invalid_request")
        doing_operation = {
            "action": "correct_work_session",
            "kind": "tasks",
            "id": doing.entity_id,
            "base_hash": doing.content_hash,
            "work_started_at": "2026-07-19T08:05:00+09:00",
        }
        with mock.patch(
            "webapp.store.current_time",
            return_value=datetime.datetime(2026, 7, 19, 9, 8, 7, tzinfo=datetime.timezone(datetime.timedelta(hours=9))),
        ):
            status, doing_preview = self.preview(doing_operation)
        self.assertEqual(status, 200)
        self.assertEqual(doing_preview["operation"], doing_operation)

        unrecorded_doing = self.write_task(
            entity_id="task-correct-unrecorded-doing",
            status="doing",
            resume_status="next",
        )
        unrecorded_done = self.write_task(
            entity_id="task-correct-unrecorded-done", status="done"
        )
        for task, payload in (
            (unrecorded_doing, {"work_started_at": "2026-07-19T08:05:00+09:00"}),
            (unrecorded_done, {"work_started_at": "2026-07-19T08:05:00+09:00", "work_ended_at": "2026-07-19T08:05:00+09:00"}),
        ):
            with self.subTest(task=task.entity_id):
                self.assert_error(
                    self.preview(
                        {
                            "action": "correct_work_session",
                            "kind": "tasks",
                            "id": task.entity_id,
                            "base_hash": task.content_hash,
                            **payload,
                        }
                    ),
                    400,
                    "invalid_request",
                )

        zero_duration = self.write_task(
            entity_id="task-correct-zero-duration",
            status="done",
            work_started_at="2026-07-19T08:00:00+09:00",
            work_ended_at="2026-07-19T08:30:00+09:00",
        )
        with mock.patch(
            "webapp.store.current_time",
            return_value=datetime.datetime(2026, 7, 19, 9, 8, 7, tzinfo=datetime.timezone(datetime.timedelta(hours=9))),
        ):
            status, zero_preview = self.preview(
                {
                    "action": "correct_work_session",
                    "kind": "tasks",
                    "id": zero_duration.entity_id,
                    "base_hash": zero_duration.content_hash,
                    "work_started_at": "2026-07-19T08:05:00+09:00",
                    "work_ended_at": "2026-07-19T08:05:00+09:00",
                }
            )
        self.assertEqual(status, 200)
        self.assertEqual(self.apply(zero_preview)[0], 200)

    def test_mutation_headers_are_checked_before_body_access(self) -> None:
        class ExplodingBody:
            def __len__(self) -> int:
                raise AssertionError("body was examined")

        for headers in (
            {},
            {**self.headers, "Origin": "http://evil.invalid"},
            {**self.headers, "X-GTD-Web": "0"},
        ):
            with self.subTest(headers=headers):
                result = handle(
                    self.store,
                    "POST",
                    "/api/v1/mutations/preview",
                    headers=headers,
                    body=ExplodingBody(),  # type: ignore[arg-type]
                )
                self.assert_error(result, 403, "forbidden")

    def test_exact_https_origin_reaches_body_validation_and_near_match_is_forbidden(self) -> None:
        origins = (
            "http://127.0.0.1:24873",
            "http://localhost:24873",
            "https://example.invalid",
        )
        accepted = handle(
            self.store,
            "POST",
            "/api/v1/mutations/preview",
            headers={**self.headers, "Origin": origins[2]},
            body=b"{}",
            mutation_origins=origins,
        )
        self.assert_error(accepted, 400, "invalid_request")
        rejected = handle(
            self.store,
            "POST",
            "/api/v1/mutations/preview",
            headers={**self.headers, "Origin": f"{origins[1]}.evil.example"},
            body=b"{}",
            mutation_origins=origins,
        )
        self.assert_error(rejected, 403, "forbidden")

    def test_strict_content_type_and_json_boundary(self) -> None:
        cases = (
            ({**self.headers, "Content-Type": "text/plain"}, b"{}", 400),
            (self.headers, b"", 400),
            (self.headers, b"\xff", 400),
            (self.headers, b"\xef\xbb\xbf{}", 400),
            (self.headers, b"{} ", 400),
            (self.headers, b'{"action":"x","action":"y"}', 400),
            (self.headers, b'{"x":{"a":1,"a":2}}', 400),
            (self.headers, b'{"x":NaN}', 400),
            (self.headers, b"[]", 400),
            (self.headers, b'{"x":' + b"[" * 70 + b"0" + b"]" * 70 + b"}", 400),
            (self.headers, b"{" + b"x" * 1_048_576 + b"}", 413),
        )
        for headers, body, expected_status in cases:
            with self.subTest(body=body[:30], expected_status=expected_status):
                result = handle(
                    self.store,
                    "POST",
                    "/api/v1/mutations/preview",
                    headers=headers,
                    body=body,
                )
                self.assert_error(
                    result,
                    expected_status,
                    "invalid_request" if expected_status == 400 else "payload_too_large",
                )

    def test_huge_json_integer_is_a_stable_invalid_request(self) -> None:
        body = b'{"action":' + b"9" * 5000 + b'}'

        result = handle(
            self.store,
            "POST",
            "/api/v1/mutations/preview",
            headers=self.headers,
            body=body,
        )

        self.assert_error(result, 400, "invalid_request")

    def test_mutation_method_query_and_unknown_route_rules(self) -> None:
        self.assert_error(
            handle(self.store, "GET", "/api/v1/mutations/preview"),
            405,
            "method_not_allowed",
        )
        self.assert_error(
            handle(
                self.store,
                "POST",
                "/api/v1/mutations/preview",
                {"x": "1"},
                self.headers,
                b"{}",
            ),
            400,
            "invalid_request",
        )
        self.assert_error(
            handle(
                self.store,
                "POST",
                "/api/v1/mutations/unknown",
                headers=self.headers,
                body=b"{}",
            ),
            404,
            "not_found",
        )

    def test_create_preview_is_exact_hashable_non_mutating_and_applies_once(self) -> None:
        status, preview = self.preview(
            {
                "action": "create",
                "kind": "tasks",
                "fields": {"title": "電話する"},
                "body": "メモ\n",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.store.list_entities(), [])
        self.assertEqual(
            preview["operation"],
            {
                "action": "create",
                "kind": "tasks",
                "fields": {"title": "電話する"},
                "body": "メモ\n",
            },
        )
        self.assertIsNone(preview["before"])
        proposed = preview["proposed"]
        self.assertEqual(proposed["path"].split("/")[0], "inbox")  # type: ignore[index]
        self.assertEqual(proposed["body"], "\nメモ\n")  # type: ignore[index]
        self.assertRegex(proposed["content_hash"], r"^[0-9a-f]{64}$")  # type: ignore[index]
        self.assertEqual(preview["source_paths"], [])
        self.assertEqual(preview["destination_paths"], [proposed["path"]])  # type: ignore[index]
        self.assertIn("--- /dev/null", preview["diff"])
        self.assertIn("+++ inbox/", preview["diff"])
        canonical = json.dumps(
            {key: value for key, value in preview.items() if key != "preview_hash"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(preview["preview_hash"], hashlib.sha256(canonical).hexdigest())
        self.assertNotIn("integrity", json.dumps(preview))
        self.assertNotIn(str(self.root), json.dumps(preview))

        apply_status, result = self.apply(preview)
        self.assertEqual(apply_status, 201)
        stored = self.store.get_entity(result["id"])  # type: ignore[arg-type]
        self.assertEqual(result["id"], stored.entity_id)
        self.assertEqual(result["path"], stored.relative_path)
        self.assertEqual(result["frontmatter"], stored.frontmatter)
        self.assertEqual(result["body"], stored.body)
        self.assertEqual(result["content_hash"], stored.content_hash)
        self.assertEqual(result["kind"], "tasks")
        self.assertFalse(result["archived"])
        self.assertEqual(self.apply(preview)[0], 409)

    def test_calendar_import_create_preserves_exact_top_level_task_id(self) -> None:
        requested_id = "task-calendar-0123456789abcdef01234567"
        operation = {
            "action": "create",
            "kind": "tasks",
            "id": requested_id,
            "fields": {"title": "Calendar import"},
            "body": "Managed body",
        }
        fixed_now = datetime.datetime(
            2026, 8, 3, 6, 0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )
        with mock.patch("webapp.store.current_time", return_value=fixed_now):
            status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(preview["proposed"]["id"], requested_id)
        self.assertEqual(preview["proposed"]["frontmatter"]["id"], requested_id)
        self.assertEqual(
            preview["proposed"]["frontmatter"]["created_at"],
            "2026-08-03T06:00:00+09:00",
        )

        applied_status, applied = self.apply(preview)

        self.assertEqual(applied_status, 201)
        self.assertEqual(applied["id"], requested_id)
        self.assertEqual(self.store.get_entity(requested_id).content_hash,
                         applied["content_hash"])

    def test_calendar_import_id_restrictions_and_duplicate_id_json_are_rejected(self) -> None:
        valid = "task-calendar-abcdef0123456789abcdef01"
        invalid = (
            {"action": "create", "kind": "projects", "id": valid,
             "fields": {"title": "Project"}},
            {"action": "create", "kind": "goals", "id": valid,
             "fields": {"title": "Goal"}},
            {"action": "create", "kind": "reviews", "id": valid,
             "review_kind": "daily", "fields": {"period_start": "2026-08-03"}},
            {"action": "create", "kind": "tasks", "id": "task-20260803-001",
             "fields": {"title": "Ordinary"}},
            {"action": "create", "kind": "tasks", "id": None,
             "fields": {"title": "Null"}},
            {"action": "create", "kind": "tasks", "id": 1,
             "fields": {"title": "Integer"}},
            {"action": "create", "kind": "tasks", "id": True,
             "fields": {"title": "Boolean"}},
            {"action": "create", "kind": "tasks", "id": valid,
             "fields": {"title": "Bad", "id": valid}},
            {"action": "create", "kind": "tasks", "id": valid,
             "fields": {"title": "Bad", "created_at": "2026-08-03"}},
            {"action": "create", "kind": "tasks", "id": valid,
             "fields": {"title": "Bad", "updated_at": "2026-08-03"}},
        )
        for operation in invalid:
            with self.subTest(operation=operation):
                self.assert_error(self.preview(operation), 400, "invalid_request")

        duplicate = (
            b'{"action":"create","kind":"tasks","id":"' + valid.encode()
            + b'","id":"' + valid.encode()
            + b'","fields":{"title":"Duplicate"}}'
        )
        self.assert_error(
            handle(
                self.store, "POST", "/api/v1/mutations/preview",
                headers=self.headers, body=duplicate,
            ),
            400, "invalid_request",
        )

        first = {
            "action": "create", "kind": "tasks", "id": valid,
            "fields": {"title": "First"},
        }
        status, preview = self.preview(first)
        self.assertEqual(status, 200)
        self.assertEqual(self.apply(preview)[0], 201)
        collision = self.preview({**first, "fields": {"title": "Collision"}})
        self.assert_error(collision, 409, "conflict")

        secret = "task-calendar-secret-private-value"
        rejected = self.preview(
            {"action": "create", "kind": "tasks", "id": secret,
             "fields": {"title": "Rejected"}}
        )
        self.assert_error(rejected, 400, "invalid_request")
        self.assertNotIn(secret, json.dumps(rejected))

    def test_update_move_and_archive_preview_apply_exact_planned_bytes(self) -> None:
        current = self.write_task(status="inbox")
        update_status, update = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "fields": {"status": "next", "title": "After"},
                "body": None,
            }
        )
        self.assertEqual(update_status, 200)
        self.assertEqual(update["path_change"]["from"], current.relative_path)  # type: ignore[index]
        self.assertTrue(update["path_change"]["to"].startswith("tasks/"))  # type: ignore[index]
        self.assertEqual(update["operation"]["body"], None)  # type: ignore[index]
        self.assertTrue((self.root / current.relative_path).exists())
        applied_status, applied = self.apply(update)
        self.assertEqual(applied_status, 200)
        self.assertEqual(applied["content_hash"], update["proposed"]["content_hash"])  # type: ignore[index]
        self.assertEqual(
            (self.root / applied["path"]).read_bytes(),  # type: ignore[index]
            serialize_frontmatter(applied["frontmatter"], "task").encode("utf-8")  # type: ignore[index]
            + applied["body"].encode("utf-8"),  # type: ignore[index]
        )

        archive_status, archive = self.preview(
            {
                "action": "archive",
                "kind": "tasks",
                "id": applied["id"],
                "base_hash": applied["content_hash"],
            }
        )
        self.assertEqual(archive_status, 200)
        self.assertNotEqual(archive["path_change"]["from"], archive["path_change"]["to"])  # type: ignore[index]
        self.assertIn("--- tasks/", archive["diff"])
        self.assertIn("+++ archive/", archive["diff"])
        archived_status, archived = self.apply(archive)
        self.assertEqual(archived_status, 200)
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["content_hash"], applied["content_hash"])


    def test_task_archive_request_stays_compatible_and_returns_atomic_dependency_effects(self) -> None:
        """Catches referenced Task archive falling back to a partial single-file mutation."""
        project = self.write_project()
        archived = self.write_task(
            entity_id="task-archive-root", status="next",
            project_id=project.entity_id, project_position="1",
        )
        dependent = self.write_task(
            entity_id="task-archive-child", status="waiting",
            project_id=project.entity_id, project_position="1",
            depends_on=f"[{archived.entity_id}]", action_date="2026-07-18",
            body="Preserved body\n",
        )
        operation = {
            "action": "archive",
            "kind": "tasks",
            "id": archived.entity_id,
            "base_hash": archived.content_hash,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [(effect["role"], effect["proposed"]["id"]) for effect in preview["effects"]],  # type: ignore[index]
            [
                ("task_archived", archived.entity_id),
                ("dependency_released", dependent.entity_id),
            ],
        )
        child_preview = preview["effects"][1]["proposed"]  # type: ignore[index]
        self.assertEqual(child_preview["frontmatter"]["status"], "next")
        self.assertNotIn("depends_on", child_preview["frontmatter"])
        self.assertEqual(child_preview["frontmatter"]["action_date"], "2026-07-18")
        self.assertEqual(child_preview["body"], "Preserved body\n")

        applied_status, applied = self.apply(preview)

        self.assertEqual(applied_status, 200)
        self.assertEqual(applied["operation"], operation)
        self.assertEqual(
            [(effect["role"], effect["id"]) for effect in applied["effects"]],  # type: ignore[index]
            [
                ("task_archived", archived.entity_id),
                ("dependency_released", dependent.entity_id),
            ],
        )
        self.assertTrue(applied["effects"][0]["archived"])  # type: ignore[index]
        self.assertEqual(
            self.store.get_entity(dependent.entity_id).frontmatter["status"], "next"
        )

    def test_done_update_returns_effects_only_when_it_releases_dependencies(self) -> None:
        dependency = self.write_task(
            entity_id="task-dependency-direct",
            status="next",
        )
        dependent = self.write_task(
            entity_id="task-dependent-direct",
            status="waiting",
            depends_on=f"[{dependency.entity_id}]",
        )
        operation = {
            "action": "update",
            "kind": "tasks",
            "id": dependency.entity_id,
            "base_hash": dependency.content_hash,
            "fields": {"status": "done"},
            "body": None,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["completed", "dependency_released"],
        )
        apply_status, applied = self.apply(preview)
        self.assertEqual(apply_status, 200)
        self.assertEqual(
            [effect["id"] for effect in applied["effects"]],  # type: ignore[index]
            [dependency.entity_id, dependent.entity_id],
        )
        self.assertEqual(
            [effect["role"] for effect in applied["effects"]],  # type: ignore[index]
            ["completed", "dependency_released"],
        )

        standalone = self.write_task(
            entity_id="task-standalone-direct",
            status="next",
        )
        standalone_status, standalone_preview = self.preview(
            {
                **operation,
                "id": standalone.entity_id,
                "base_hash": standalone.content_hash,
            }
        )
        self.assertEqual(standalone_status, 200)
        self.assertIn("proposed", standalone_preview)
        self.assertNotIn("effects", standalone_preview)

    def test_calendar_completion_update_releases_dependency_in_one_effects_preview(self) -> None:
        calendar_identity = {
            "calendar_id": "example@group.calendar.google.com",
            "calendar_event_id": "event-dependency-calendar",
            "calendar_event_url": "https://calendar.google.com/calendar/event?eid=ZXZlbnQ",
            "calendar_event_kind": "all_day",
            "calendar_sync_version": "2",
        }
        dependency = self.write_task(
            entity_id="task-dependency-calendar",
            status="next",
            **calendar_identity,
        )
        dependent = self.write_task(
            entity_id="task-dependent-calendar",
            status="waiting",
            depends_on=f"[{dependency.entity_id}]",
        )
        operation = {
            "action": "update",
            "kind": "tasks",
            "id": dependency.entity_id,
            "base_hash": dependency.content_hash,
            "fields": {
                "status": "done",
                "completed_at": "2026-07-19T09:08:07+09:00",
            },
            "body": None,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["completed", "dependency_released"],
        )
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200)
        self.assertEqual(
            [effect["id"] for effect in applied["effects"]],  # type: ignore[index]
            [dependency.entity_id, dependent.entity_id],
        )
        self.assertEqual(
            [effect["role"] for effect in applied["effects"]],  # type: ignore[index]
            ["completed", "dependency_released"],
        )

    def test_dependency_edit_uses_atomic_update_effect(self) -> None:
        dependency = self.write_task(
            entity_id="task-dependency-edit",
            status="next",
        )
        target = self.write_task(
            entity_id="task-dependent-edit",
            status="next",
        )
        operation = {
            "action": "update",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": target.content_hash,
            "fields": {"depends_on": f"[{dependency.entity_id}]"},
            "body": None,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["effects"][0]["role"], "dependency_retargeted")  # type: ignore[index]
        self.assertEqual(
            preview["effects"][0]["proposed"]["frontmatter"]["status"],  # type: ignore[index]
            "waiting",
        )
        self.assertEqual(
            preview["effects"][0]["proposed"]["frontmatter"]["depends_on"],  # type: ignore[index]
            f"[{dependency.entity_id}]",
        )

    def test_same_path_update_preserves_path_and_replaces_body(self) -> None:
        current = self.write_task(status="next", body="Old\n")
        status, preview = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "fields": {},
                "body": "New\n",
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            preview["path_change"],
            {"from": current.relative_path, "to": current.relative_path},
        )
        self.assertIn("-Old", preview["diff"])
        self.assertIn("+New", preview["diff"])
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200)
        self.assertEqual(applied["body"], "\nNew\n")

    def test_task_action_date_update_sets_clears_and_preserves_due(self) -> None:
        """Catches the one-card update contract altering the distinct deadline."""
        current = self.write_task(
            status="next", due="2026-09-01", **CALENDAR_FIELDS
        )
        operation = {
            "action": "update",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": current.content_hash,
            "fields": {"action_date": "2026-08-29"},
            "body": None,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200, preview)
        proposed = preview["proposed"]["frontmatter"]  # type: ignore[index]
        self.assertEqual(proposed["action_date"], "2026-08-29")
        self.assertEqual(proposed["due"], "2026-09-01")
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200, applied)
        readback = self.store.get_entity(current.entity_id)
        self.assertEqual(readback.frontmatter["action_date"], "2026-08-29")
        self.assertEqual(readback.frontmatter["due"], "2026-09-01")
        for key, value in CALENDAR_FIELDS.items():
            self.assertEqual(readback.frontmatter[key], value)

        clear_operation = {
            **operation,
            "base_hash": readback.content_hash,
            "fields": {"action_date": ""},
        }
        clear_status, clear_preview = self.preview(clear_operation)
        self.assertEqual(clear_status, 200, clear_preview)
        self.assertNotIn(
            "action_date", clear_preview["proposed"]["frontmatter"]  # type: ignore[index]
        )
        self.assertEqual(self.apply(clear_preview)[0], 200)
        cleared = self.store.get_entity(current.entity_id)
        self.assertNotIn("action_date", cleared.frontmatter)
        self.assertEqual(cleared.frontmatter["due"], "2026-09-01")
        for key, value in CALENDAR_FIELDS.items():
            self.assertEqual(cleared.frontmatter[key], value)

    def test_generic_create_and_update_reject_unknown_must_date_field(self) -> None:
        """Catches the removed field entering canonical data through generic writes."""
        create_status, create_payload = self.preview(
            {
                "action": "create",
                "kind": "tasks",
                "fields": {
                    "title": "Legacy",
                    "status": "next",
                    "must_date": "2026-08-29",
                    **CALENDAR_FIELDS,
                },
                "body": "",
            }
        )
        self.assertEqual(create_status, 400, create_payload)

        current = self.write_task(
            entity_id="task-unknown-field-write", status="next", **CALENDAR_FIELDS
        )
        for value in ("2026-08-29", ""):
            with self.subTest(value=value):
                status, payload = self.preview(
                    {
                        "action": "update",
                        "kind": "tasks",
                        "id": current.entity_id,
                        "base_hash": current.content_hash,
                        "fields": {"must_date": value},
                    }
                )
                self.assertEqual(status, 400, payload)

    def test_task_action_date_update_rejects_invalid_status_and_stale_hash(self) -> None:
        """Catches malformed or non-executable date writes and stale previews."""
        project = self.write_project(entity_id="project-action-date")
        candidates = (
            self.write_task(entity_id="task-action-date-next", status="next"),
            self.write_task(entity_id="task-action-date-inbox", status="inbox"),
            self.write_task(
                entity_id="task-action-date-planned",
                status="planned",
                project_id=project.entity_id,
            ),
        )
        next_task, inbox_task, planned_task = candidates
        invalid_requests = (
            {
                "action": "update",
                "kind": "tasks",
                "id": next_task.entity_id,
                "base_hash": next_task.content_hash,
                "fields": {"action_date": "2026-08-29T09:00:00+09:00"},
            },
            {
                "action": "update",
                "kind": "tasks",
                "id": inbox_task.entity_id,
                "base_hash": inbox_task.content_hash,
                "fields": {"action_date": "2026-08-29"},
            },
            {
                "action": "update",
                "kind": "tasks",
                "id": planned_task.entity_id,
                "base_hash": planned_task.content_hash,
                "fields": {"action_date": "2026-08-29"},
            },
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                self.assert_error(self.preview(request), 400, "invalid_request")

        stale = {
            "action": "update",
            "kind": "tasks",
            "id": next_task.entity_id,
            "base_hash": "0" * 64,
            "fields": {"action_date": "2026-08-29"},
        }
        self.assert_error(self.preview(stale), 409, "conflict")

    def test_task_action_date_unknown_apply_is_single_use_and_not_retried(self) -> None:
        """Catches an ambiguous apply being accepted a second time."""
        current = self.write_task(entity_id="task-action-date-unknown", status="next")
        status, preview = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "fields": {"action_date": "2026-08-29"},
            }
        )
        self.assertEqual(status, 200, preview)

        with mock.patch.object(
            self.store, "apply_mutation_plan", side_effect=TimeoutError("ambiguous")
        ) as apply_once:
            first = self.apply(preview)
            second = self.apply(preview)

        self.assert_error(first, 500, "mutation_failed")
        self.assert_error(second, 409, "stale_preview")
        apply_once.assert_called_once()

    def test_removed_date_migration_operations_are_rejected(self) -> None:
        for action in ("migrate_action_date", "remove_must_date"):
            with self.subTest(action=action):
                self.assert_error(
                    self.preview({"action": action, "kind": "tasks"}),
                    400,
                    "invalid_request",
                )

    def test_pause_notifications_previews_one_strict_marker_and_applies(self) -> None:
        current = self.write_task(
            entity_id="task-pause",
            status="doing",
            body=(
                "Body\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-08-19T09:10:00+09:00\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-02-30T09:10:00+09:00\n"
            ),
        )
        now = datetime.datetime(
            2026, 8, 19, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        )

        with mock.patch("webapp.store.current_time", return_value=now):
            status, preview = self.preview(
                {
                    "action": "pause_notifications",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": current.content_hash,
                    "minutes": 30,
                }
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            preview["operation"],
            {
                "action": "pause_notifications",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "minutes": 30,
            },
        )
        self.assertEqual(
            preview["proposed"]["body"],  # type: ignore[index]
            "\nBody\n[gtd-focus-monitor] 通知停止期限: 2026-02-30T09:10:00+09:00\n\n"
            "[gtd-focus-monitor] 通知停止期限: 2026-08-19T09:30:00+09:00\n",
        )
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200)
        self.assertEqual(applied["body"], preview["proposed"]["body"])  # type: ignore[index]

    def test_pause_notifications_rejects_invalid_duration_and_noncurrent_or_stale_task(self) -> None:
        current = self.write_task(entity_id="task-doing", status="doing")
        other = self.write_task(entity_id="task-next", status="next")
        base = {
            "action": "pause_notifications",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": current.content_hash,
            "minutes": 1,
        }
        for invalid in (
            {**base, "minutes": True},
            {**base, "minutes": 0},
            {**base, "minutes": 1441},
            {**base, "minutes": "30"},
            {**base, "extra": "no"},
        ):
            with self.subTest(invalid=invalid):
                self.assert_error(self.preview(invalid), 400, "invalid_request")
        self.assert_error(
            self.preview({**base, "id": other.entity_id, "base_hash": other.content_hash}),
            409,
            "conflict",
        )
        self.assert_error(
            self.preview({**base, "base_hash": "0" * 64}), 409, "conflict"
        )

    def test_resume_notifications_removes_only_valid_strict_marker_lines(self) -> None:
        current = self.write_task(
            entity_id="task-resume",
            status="doing",
            body=(
                "Body\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-08-19T09:10:00+09:00\n"
                "[gtd-focus-monitor] 通知停止期限: 2026-02-30T09:10:00+09:00\n"
                "prefix [gtd-focus-monitor] 通知停止期限: 2026-08-19T09:11:00+09:00\n"
            ),
        )
        operation = {
            "action": "resume_notifications",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": current.content_hash,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            preview["proposed"]["body"],  # type: ignore[index]
            "\nBody\n[gtd-focus-monitor] 通知停止期限: 2026-02-30T09:10:00+09:00\n"
            "prefix [gtd-focus-monitor] 通知停止期限: 2026-08-19T09:11:00+09:00\n",
        )
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200)
        self.assertEqual(applied["body"], preview["proposed"]["body"])  # type: ignore[index]

    def test_create_supports_all_kinds_and_review_shape(self) -> None:
        cases = (
            ("tasks", {"title": "T"}, None),
            ("purposes", {"title": "Purpose"}, None),
            ("visions", {"title": "Vision"}, None),
            ("areas", {"title": "Area"}, None),
            ("projects", {"title": "P"}, None),
            ("goals", {"title": "G"}, None),
            ("reviews", {"period_start": "2026-07-18"}, "daily"),
        )
        for kind, fields, review_kind in cases:
            payload: dict[str, object] = {
                "action": "create",
                "kind": kind,
                "fields": fields,
            }
            if kind == "purposes":
                payload["body"] = PURPOSE_BODY
            if review_kind:
                payload["review_kind"] = review_kind
            with self.subTest(kind=kind):
                status, preview = self.preview(payload)
                self.assertEqual(status, 200)
                self.assertEqual(preview["proposed"]["kind"], kind)  # type: ignore[index]

    def test_direction_kinds_use_existing_preview_apply_update_and_archive(self) -> None:
        cases = (
            ("purposes", {"title": "Purpose"}, {"title": "Purpose updated"}),
            ("visions", {"title": "Vision"}, {"status": "on_hold"}),
            (
                "areas",
                {"title": "Area"},
                {
                    "health": "needs_attention",
                    "last_reviewed_on": "2026-08-14",
                },
            ),
        )
        for kind, fields, updates in cases:
            with self.subTest(kind=kind):
                create_operation: dict[str, object] = {
                    "action": "create",
                    "kind": kind,
                    "fields": fields,
                }
                if kind == "purposes":
                    create_operation["body"] = PURPOSE_BODY
                create_status, create_preview = self.preview(
                    create_operation
                )
                self.assertEqual(create_status, 200)
                applied_status, created = self.apply(create_preview)
                self.assertEqual(applied_status, 201)

                update_status, update_preview = self.preview(
                    {
                        "action": "update",
                        "kind": kind,
                        "id": created["id"],
                        "base_hash": created["content_hash"],
                        "fields": updates,
                    }
                )
                self.assertEqual(update_status, 200)
                updated_status, updated = self.apply(update_preview)
                self.assertEqual(updated_status, 200)
                self.assertNotEqual(updated["content_hash"], created["content_hash"])
                for key, value in updates.items():
                    self.assertEqual(updated["frontmatter"][key], value)  # type: ignore[index]

                detail_status, detail = handle(
                    self.store,
                    "GET",
                    f"/api/v1/entities/{kind}/{updated['id']}",
                )
                self.assertEqual(detail_status, 200)
                self.assertEqual(detail["content_hash"], updated["content_hash"])

                archive_status, archive_preview = self.preview(
                    {
                        "action": "archive",
                        "kind": kind,
                        "id": updated["id"],
                        "base_hash": updated["content_hash"],
                    }
                )
                self.assertEqual(archive_status, 200)
                archived_status, archived = self.apply(archive_preview)
                self.assertEqual(archived_status, 200)
                self.assertTrue(archived["archived"])

    def test_purpose_singleton_and_archive_blocker_are_invalid_requests(self) -> None:
        purpose_status, purpose_preview = self.preview(
            {
                "action": "create",
                "kind": "purposes",
                "fields": {"title": "Only purpose"},
                "body": PURPOSE_BODY,
            }
        )
        self.assertEqual(purpose_status, 200)
        self.assertEqual(self.apply(purpose_preview)[0], 201)
        self.assert_error(
            self.preview(
                {
                    "action": "create",
                    "kind": "purposes",
                    "fields": {"title": "Second purpose"},
                    "body": PURPOSE_BODY,
                }
            ),
            400,
            "invalid_request",
        )

        _, area_preview = self.preview(
            {"action": "create", "kind": "areas", "fields": {"title": "Area"}}
        )
        _, area = self.apply(area_preview)
        _, project_preview = self.preview(
            {
                "action": "create",
                "kind": "projects",
                "fields": {"title": "Project", "area_id": area["id"]},
            }
        )
        self.assertEqual(self.apply(project_preview)[0], 201)

        blocked = self.preview(
            {
                "action": "archive",
                "kind": "areas",
                "id": area["id"],
                "base_hash": area["content_hash"],
            }
        )
        self.assert_error(blocked, 400, "invalid_request")
        self.assertEqual(
            self.store.get_entity(area["id"]).content_hash,  # type: ignore[arg-type]
            area["content_hash"],
        )

    def test_all_schema_forbidden_fields_are_rejected_without_state_or_data_changes(self) -> None:
        state = ApiState(self.store, max_previews=1)
        valid_status, valid_preview = self.preview(
            {
                "action": "create",
                "kind": "tasks",
                "fields": {"title": "Valid preview remains live"},
            },
            state=state,
        )
        self.assertEqual(valid_status, 200)
        before_files = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

        for forbidden_key in sorted(FORBIDDEN_KEYS):
            raw_value = f"SECRET-{forbidden_key}-/home/example/private/{forbidden_key}.md"
            with self.subTest(forbidden_key=forbidden_key):
                payload = self.assert_error(
                    self.preview(
                        {
                            "action": "create",
                            "kind": "tasks",
                            "fields": {
                                "title": "Rejected",
                                forbidden_key: raw_value,
                            },
                        },
                        state=state,
                    ),
                    400,
                    "invalid_request",
                )
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(raw_value, serialized)
                self.assertNotIn("/home/example/private", serialized)
                self.assertNotIn(forbidden_key, serialized)
                self.assertEqual(
                    {
                        path.relative_to(self.root): path.read_bytes()
                        for path in self.root.rglob("*")
                        if path.is_file()
                    },
                    before_files,
                )

        applied_status, _ = self.apply(valid_preview, state=state)
        self.assertEqual(applied_status, 201)

    def test_strict_operation_shapes_and_kind_mismatch_nondisclosure(self) -> None:
        current = self.write_task()
        invalid = (
            {},
            {"action": "create", "kind": "tasks", "fields": {}, "id": "x"},
            {"action": "create", "kind": "reviews", "fields": {}},
            {"action": "create", "kind": "tasks", "fields": {}, "review_kind": "daily"},
            {"action": "update", "kind": "tasks", "id": "../x", "base_hash": "0" * 64, "fields": {}},
            {"action": "update", "kind": "tasks", "id": current.entity_id, "base_hash": "A" * 64, "fields": {}},
            {"action": "update", "kind": "tasks", "id": current.entity_id, "base_hash": current.content_hash, "fields": {"title": 1}},
            {"action": "archive", "kind": "tasks", "id": current.entity_id, "base_hash": current.content_hash, "body": "x"},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_error(self.preview(payload), 400, "invalid_request")

        mismatch = self.preview(
            {
                "action": "archive",
                "kind": "projects",
                "id": current.entity_id,
                "base_hash": current.content_hash,
            }
        )
        unknown = self.preview(
            {
                "action": "archive",
                "kind": "projects",
                "id": "unknown-id",
                "base_hash": current.content_hash,
            }
        )
        self.assertEqual(mismatch, unknown)
        self.assertNotIn("task", json.dumps(mismatch))

    def test_tampered_and_fabricated_previews_do_not_write_or_consume_valid_record(self) -> None:
        _, preview = self.preview(
            {"action": "create", "kind": "tasks", "fields": {"title": "Safe"}}
        )
        for section in (
            "operation",
            "proposed",
            "before",
            "diff",
            "path_change",
            "validation",
            "base_hash",
        ):
            altered = json.loads(json.dumps(preview))
            altered[section] = {"altered": True}
            altered_without_hash = {
                key: value for key, value in altered.items() if key != "preview_hash"
            }
            altered["preview_hash"] = hashlib.sha256(
                json.dumps(
                    altered_without_hash,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            with self.subTest(section=section):
                self.assert_error(self.apply(altered), 409, "stale_preview")
                self.assertEqual(self.store.list_entities(), [])

        fabricated = json.loads(json.dumps(preview))
        fabricated_without_hash = {
            key: value for key, value in fabricated.items() if key != "preview_hash"
        }
        fabricated_without_hash["diff"] = "coherent fake"
        fabricated["diff"] = "coherent fake"
        fabricated["preview_hash"] = hashlib.sha256(
            json.dumps(
                fabricated_without_hash,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assert_error(self.apply(fabricated), 409, "stale_preview")
        self.assertEqual(self.apply(preview)[0], 201)

    def test_registry_expiry_eviction_cross_store_and_default_weak_keys(self) -> None:
        now = [100.0]
        state = ApiState(self.store, clock=lambda: now[0], ttl_seconds=10, max_previews=2)
        _, expired = self.preview(
            {"action": "create", "kind": "tasks", "fields": {"title": "Expired"}},
            state=state,
        )
        now[0] = 111.0
        self.assert_error(self.apply(expired, state=state), 409, "stale_preview")

        now[0] = 200.0
        previews = []
        for title in ("One", "Two", "Three"):
            _, item = self.preview(
                {"action": "create", "kind": "tasks", "fields": {"title": title}},
                state=state,
            )
            previews.append(item)
            now[0] += 1
        self.assert_error(self.apply(previews[0], state=state), 409, "stale_preview")

        with tempfile.TemporaryDirectory() as other_directory:
            other_root = pathlib.Path(other_directory)
            for directory in ("inbox", "tasks", "projects", "goals", "reviews/daily", "reviews/weekly", "archive"):
                (other_root / directory).mkdir(parents=True, exist_ok=True)
            other = Store(other_root)
            self.assert_error(
                self.apply(previews[-1], store=other, state=state),
                409,
                "stale_preview",
            )

        with tempfile.TemporaryDirectory() as weak_directory:
            weak_root = pathlib.Path(weak_directory)
            for directory in ("inbox", "tasks", "projects", "goals", "reviews/daily", "reviews/weekly", "archive"):
                (weak_root / directory).mkdir(parents=True, exist_ok=True)
            temporary_store = Store(weak_root)
            reference = weakref.ref(temporary_store)
            result = handle(
                temporary_store,
                "POST",
                "/api/v1/mutations/preview",
                headers=self.headers,
                body=self.body(
                    {"action": "create", "kind": "tasks", "fields": {"title": "Weak"}}
                ),
            )
            self.assertEqual(result[0], 200)
            del temporary_store
            gc.collect()
            self.assertIsNone(reference())

    def test_apply_uses_same_strict_json_and_header_boundary(self) -> None:
        _, preview = self.preview(
            {"action": "create", "kind": "tasks", "fields": {"title": "Strict"}}
        )
        invalid_headers = {**self.headers, "Origin": "null"}
        self.assert_error(
            handle(
                self.store,
                "POST",
                "/api/v1/mutations/apply",
                headers=invalid_headers,
                body=b"{" + b"x" * 1_048_576,
            ),
            403,
            "forbidden",
        )
        self.assert_error(
            handle(
                self.store,
                "POST",
                "/api/v1/mutations/apply",
                headers=self.headers,
                body=self.body(
                    {
                        "preview": {
                            key: value for key, value in preview.items() if key != "preview_hash"
                        },
                        "preview_hash": preview["preview_hash"],
                        "extra": True,
                    }
                ),
            ),
            400,
            "invalid_request",
        )
        self.assertEqual(self.apply(preview)[0], 201)

    def test_concurrent_duplicate_apply_executes_at_most_once(self) -> None:
        state = ApiState(self.store)
        _, preview = self.preview(
            {"action": "create", "kind": "tasks", "fields": {"title": "Once"}},
            state=state,
        )
        barrier = threading.Barrier(3)
        results: list[tuple[int, dict[str, object]]] = []

        def run() -> None:
            barrier.wait()
            results.append(self.apply(preview, state=state))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(len(self.store.list_entities()), 1)

    def test_task_workflow_request_shapes_and_active_resolution_are_strict(self) -> None:
        target = self.write_task(entity_id="task-target")
        invalid = (
            {
                "action": "start",
                "kind": "projects",
                "id": target.entity_id,
                "base_hash": target.content_hash,
            },
            {"action": "start", "kind": "tasks", "id": target.entity_id},
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "fields": {},
            },
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": None,
            },
            {
                "action": "interrupt",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "body": "forbidden",
            },
            {
                "action": "complete",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "review_kind": "daily",
            },
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {"id": "task-active", "base_hash": "a" * 64},
            },
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": "task-active",
                    "base_hash": "a" * 64,
                    "action": "start",
                },
            },
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": "task-active",
                    "base_hash": "a" * 64,
                    "action": "interrupt",
                    "extra": True,
                },
            },
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": "task-unnecessary-active",
                    "base_hash": "a" * 64,
                    "action": "interrupt",
                },
            },
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": target.entity_id,
                    "base_hash": target.content_hash,
                    "action": "complete",
                },
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_error(self.preview(payload), 400, "invalid_request")

        active = self.write_task(entity_id="task-active", status="doing")
        self.assert_error(
            self.preview(
                {
                    "action": "start",
                    "kind": "tasks",
                    "id": target.entity_id,
                    "base_hash": target.content_hash,
                }
            ),
            400,
            "invalid_request",
        )
        status, preview = self.preview(
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": active.entity_id,
                    "base_hash": active.content_hash,
                    "action": "interrupt",
                },
            }
        )
        self.assertEqual(status, 200)

    def test_create_and_start_preview_and_apply_are_one_atomic_workflow(self) -> None:
        active = self.write_task(
            entity_id="task-active-create-start",
            status="doing",
            work_started_at="2026-07-19T08:00:00+09:00",
            resume_status="next",
        )
        operation = {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": {"title": "Fresh"},
            "body": "",
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": active.content_hash,
                "action": "complete",
            },
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["completed", "started"],
        )
        self.assertIsNone(preview["effects"][-1]["before"])  # type: ignore[index]

        apply_status, result = self.apply(preview)

        self.assertEqual(apply_status, 200)
        self.assertEqual(
            [effect["frontmatter"]["status"] for effect in result["effects"]],  # type: ignore[index]
            ["done", "doing"],
        )
        self.assertEqual(validate_repository(self.root), [])

    def test_snapshot_succeeds_while_create_and_start_preview_validation_is_slow(
        self,
    ) -> None:
        entered_validation = threading.Event()
        release_validation = threading.Event()
        preview_results: list[tuple[int, dict[str, object]]] = []
        real_validate_document_map = validate_document_map

        def slow_validate_document_map(documents) -> list[str]:
            if not entered_validation.is_set():
                entered_validation.set()
                self.assertTrue(release_validation.wait(2.0))
            return real_validate_document_map(documents)

        operation = {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": {"title": "Fresh"},
            "body": "",
        }
        with (
            mock.patch(
                "webapp.store.validate_document_map",
                side_effect=slow_validate_document_map,
            ),
            mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.05),
        ):
            preview_thread = threading.Thread(
                target=lambda: preview_results.append(self.preview(operation))
            )
            preview_thread.start()
            self.assertTrue(entered_validation.wait(2.0))
            snapshot_status, snapshot = handle(
                self.store, "GET", "/api/v1/snapshot"
            )
            release_validation.set()
            preview_thread.join(2.0)

        self.assertFalse(preview_thread.is_alive())
        self.assertEqual(preview_results[0][0], 200)
        self.assertEqual(snapshot_status, 200)
        self.assertEqual(
            snapshot["mutation_state"], {"recovery_required": False}
        )

    def test_snapshot_succeeds_while_start_preview_validation_is_slow(
        self,
    ) -> None:
        target = self.write_task(
            entity_id="task-slow-start-preview",
            status="next",
        )
        entered_validation = threading.Event()
        release_validation = threading.Event()
        preview_results: list[tuple[int, dict[str, object]]] = []
        real_validate_document_map = validate_document_map

        def slow_validate_document_map(documents) -> list[str]:
            if not entered_validation.is_set():
                entered_validation.set()
                self.assertTrue(release_validation.wait(2.0))
            return real_validate_document_map(documents)

        operation = {
            "action": "start",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": target.content_hash,
        }
        with (
            mock.patch(
                "webapp.store.validate_document_map",
                side_effect=slow_validate_document_map,
            ),
            mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.05),
        ):
            preview_thread = threading.Thread(
                target=lambda: preview_results.append(self.preview(operation))
            )
            preview_thread.start()
            self.assertTrue(entered_validation.wait(2.0))
            snapshot_status, snapshot = handle(
                self.store, "GET", "/api/v1/snapshot"
            )
            release_validation.set()
            preview_thread.join(2.0)

        self.assertFalse(preview_thread.is_alive())
        self.assertEqual(preview_results[0][0], 200)
        self.assertEqual(snapshot_status, 200)
        self.assertEqual(
            snapshot["mutation_state"], {"recovery_required": False}
        )

    def test_start_preview_does_not_materialize_a_validation_tree(
        self,
    ) -> None:
        target = self.write_task(
            entity_id="task-slow-preview-tree-write",
            status="next",
        )
        real_write_bytes = pathlib.Path.write_bytes

        def reject_validation_tree_write(path: pathlib.Path, data: bytes) -> int:
            self.assertNotIn("gtd-workflow", path.as_posix())
            return real_write_bytes(path, data)

        operation = {
            "action": "start",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": target.content_hash,
        }
        with (
            mock.patch.object(
                pathlib.Path,
                "write_bytes",
                autospec=True,
                side_effect=reject_validation_tree_write,
            ),
        ):
            preview_status, _ = self.preview(operation)

        self.assertEqual(preview_status, 200)

    def test_start_preview_does_not_reread_unrelated_warm_cached_documents(
        self,
    ) -> None:
        target = self.write_task(
            entity_id="task-slow-preview-source-read",
            status="next",
        )
        unrelated = self.write_task(
            entity_id="task-unrelated-preview-source-read",
            status="next",
        )
        real_read_regular_file = self.store._read_regular_file
        read_paths: list[pathlib.Path] = []

        def record_source_read(path: pathlib.Path) -> bytes:
            read_paths.append(path)
            return real_read_regular_file(path)

        operation = {
            "action": "start",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": target.content_hash,
        }
        with (
            mock.patch.object(
                self.store,
                "_read_regular_file",
                side_effect=record_source_read,
            ),
        ):
            preview_status, _ = self.preview(operation)

        self.assertEqual(preview_status, 200)
        self.assertNotIn(
            self.root / unrelated.relative_path,
            read_paths,
        )

    def test_create_and_start_preview_interrupts_then_creates_continuation_and_starts(self) -> None:
        active = self.write_task(
            entity_id="task-active-create-start-interrupt",
            title="Deep work",
            status="doing",
            work_started_at="2026-07-19T08:00:00+09:00",
            resume_status="next",
        )
        operation = {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": {"title": "Fresh"},
            "body": "",
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": active.content_hash,
                "action": "interrupt",
            },
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["interrupted", "continuation", "started"],
        )
        continuation = preview["effects"][1]["proposed"]["frontmatter"]  # type: ignore[index]
        self.assertEqual(continuation["continuation_of"], active.entity_id)
        self.assertEqual(continuation["status"], "next")

        apply_status, result = self.apply(preview)

        self.assertEqual(apply_status, 200)
        self.assertEqual(
            [effect["frontmatter"]["status"] for effect in result["effects"]],  # type: ignore[index]
            ["done", "next", "doing"],
        )
        self.assertEqual(validate_repository(self.root), [])

    def test_start_break_preview_interrupts_then_creates_continuation_and_fixed_break(self) -> None:
        active = self.write_task(
            entity_id="task-active-break",
            title="Deep work",
            status="doing",
            work_started_at="2026-08-17T10:00:00+09:00",
            resume_status="next",
        )
        started_at = datetime.datetime(
            2026,
            8,
            17,
            11,
            0,
            0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )
        operation = {
            "action": "start_break",
            "kind": "tasks",
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": active.content_hash,
                "action": "interrupt",
            },
        }

        with mock.patch("webapp.store.current_time", return_value=started_at):
            status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["interrupted", "continuation", "started"],
        )
        break_frontmatter = preview["effects"][2]["proposed"]["frontmatter"]  # type: ignore[index]
        self.assertEqual(break_frontmatter["title"], "5分休憩")
        self.assertEqual(break_frontmatter["timer_kind"], "break")
        self.assertEqual(
            break_frontmatter["timer_ends_at"], "2026-08-17T11:05:00+09:00"
        )
        self.assertNotIn("duration", preview["operation"])
        self.assertEqual(self.apply(preview)[0], 200)
        self.assertEqual(validate_repository(self.root), [])

    def test_start_break_preview_completes_current_without_continuation(self) -> None:
        active = self.write_task(
            entity_id="task-active-complete-break",
            title="Finished work",
            status="doing",
            work_started_at="2026-08-17T10:00:00+09:00",
            resume_status="next",
        )
        operation = {
            "action": "start_break",
            "kind": "tasks",
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": active.content_hash,
                "action": "complete",
            },
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["completed", "started"],
        )
        self.assertFalse(
            any(effect["role"] == "continuation" for effect in preview["effects"])  # type: ignore[index]
        )
        self.assertEqual(self.apply(preview)[0], 200)
        self.assertEqual(validate_repository(self.root), [])

    def test_start_break_without_doing_task_uses_only_server_fixed_values(self) -> None:
        started_at = datetime.datetime(
            2026, 8, 17, 11, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        )
        with mock.patch("webapp.store.current_time", return_value=started_at):
            status, preview = self.preview(
                {"action": "start_break", "kind": "tasks"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]], ["started"]  # type: ignore[index]
        )
        proposed = preview["effects"][0]["proposed"]["frontmatter"]  # type: ignore[index]
        self.assertEqual(
            {key: proposed[key] for key in ("title", "timer_kind", "timer_ends_at")},
            {
                "title": "5分休憩",
                "timer_kind": "break",
                "timer_ends_at": "2026-08-17T11:05:00+09:00",
            },
        )

    def test_start_break_request_shape_and_server_managed_timer_fields_are_strict(self) -> None:
        active = self.write_task(entity_id="task-active-strict-break", status="doing")
        base = {"action": "start_break", "kind": "tasks"}
        resolution = {
            "id": active.entity_id,
            "base_hash": active.content_hash,
            "action": "interrupt",
        }
        invalid = (
            {**base, "duration": 300},
            {**base, "title": "5分休憩"},
            {**base, "body": ""},
            {**base, "active_resolution": None},
            {**base, "active_resolution": {**resolution, "action": "pause"}},
            {**base, "active_resolution": {**resolution, "extra": True}},
        )
        for operation in invalid:
            with self.subTest(operation=operation):
                self.assert_error(self.preview(operation), 400, "invalid_request")

        self.assert_error(
            self.preview(
                {
                    "action": "create",
                    "kind": "tasks",
                    "fields": {
                        "title": "forged break",
                        "timer_kind": "break",
                        "timer_ends_at": "2026-08-17T11:05:00+09:00",
                    },
                }
            ),
            400,
            "invalid_request",
        )

    def test_start_break_active_resolution_requires_current_normal_task_and_fresh_hash(self) -> None:
        resolution = {
            "id": "task-missing",
            "base_hash": "a" * 64,
            "action": "complete",
        }
        self.assert_error(
            self.preview(
                {
                    "action": "start_break",
                    "kind": "tasks",
                    "active_resolution": resolution,
                }
            ),
            400,
            "invalid_request",
        )

        active = self.write_task(entity_id="task-active-resolution", status="doing")
        base = {"action": "start_break", "kind": "tasks"}
        self.assert_error(
            self.preview(
                {
                    **base,
                    "active_resolution": {
                        **resolution,
                        "id": "task-not-current",
                        "base_hash": active.content_hash,
                    },
                }
            ),
            400,
            "invalid_request",
        )
        self.assert_error(
            self.preview(
                {
                    **base,
                    "active_resolution": {
                        "id": active.entity_id,
                        "base_hash": "0" * 64,
                        "action": "complete",
                    },
                }
            ),
            409,
            "conflict",
        )

        self.assertEqual(
            self.apply(
                self.preview(
                    {
                        **base,
                        "active_resolution": {
                            "id": active.entity_id,
                            "base_hash": active.content_hash,
                            "action": "complete",
                        },
                    }
                )[1]
            )[0],
            200,
        )
        break_task = next(
            entity
            for entity in self.store.list_entities()
            if entity.frontmatter.get("timer_kind") == "break"
        )
        self.assert_error(
            self.preview(
                {
                    **base,
                    "active_resolution": {
                        "id": break_task.entity_id,
                        "base_hash": break_task.content_hash,
                        "action": "interrupt",
                    },
                }
            ),
            400,
            "invalid_request",
        )

    def test_break_task_cannot_be_interrupted_but_can_be_completed_early(self) -> None:
        started_at = datetime.datetime(
            2026, 8, 17, 11, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        )
        with mock.patch("webapp.store.current_time", return_value=started_at):
            status, start = self.preview({"action": "start_break", "kind": "tasks"})
        self.assertEqual(status, 200)
        self.assertEqual(self.apply(start)[0], 200)
        break_task = next(
            entity
            for entity in self.store.list_entities()
            if entity.frontmatter.get("timer_kind") == "break"
        )

        self.assert_error(
            self.preview(
                {
                    "action": "interrupt",
                    "kind": "tasks",
                    "id": break_task.entity_id,
                    "base_hash": break_task.content_hash,
                }
            ),
            400,
            "invalid_request",
        )

        completed_at = started_at + datetime.timedelta(seconds=120)
        with mock.patch("webapp.store.current_time", return_value=completed_at):
            status, complete = self.preview(
                {
                    "action": "complete",
                    "kind": "tasks",
                    "id": break_task.entity_id,
                    "base_hash": break_task.content_hash,
                }
            )
        self.assertEqual(status, 200)
        proposed = complete["effects"][0]["proposed"]["frontmatter"]  # type: ignore[index]
        self.assertEqual(proposed["work_ended_at"], "2026-08-17T11:02:00+09:00")
        self.assertEqual(proposed["timer_ends_at"], "2026-08-17T11:05:00+09:00")

    def test_create_and_start_request_shape_is_strict(self) -> None:
        base = {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": {"title": "Fresh"},
            "body": "",
        }
        invalid = (
            {key: value for key, value in base.items() if key != "body"},
            {**base, "body": "non-empty"},
            {**base, "fields": {}},
            {**base, "fields": {"title": "Fresh", "status": "inbox"}},
            {**base, "id": "task-client-id"},
            {**base, "extra": True},
            {**base, "active_resolution": None},
            {
                **base,
                "active_resolution": {
                    "id": "task-active",
                    "base_hash": "a" * 64,
                    "action": "pause",
                },
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_error(self.preview(payload), 400, "invalid_request")

    def test_create_and_start_rejects_stale_active_hash_and_applies_once(self) -> None:
        active = self.write_task(
            entity_id="task-same-title",
            title="Same title",
            status="doing",
        )
        operation = {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": {"title": "Same title"},
            "body": "",
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": "0" * 64,
                "action": "complete",
            },
        }
        self.assert_error(self.preview(operation), 409, "conflict")

        operation["active_resolution"]["base_hash"] = active.content_hash  # type: ignore[index]
        status, preview = self.preview(operation)
        self.assertEqual(status, 200)
        self.assertEqual(self.apply(preview)[0], 200)
        self.assert_error(self.apply(preview), 409, "stale_preview")
        matches = [
            entity
            for entity in self.store.list_entities()
            if entity.frontmatter["title"] == "Same title"
        ]
        self.assertEqual(len(matches), 2)
        self.assertEqual(
            sorted(entity.frontmatter["status"] for entity in matches),
            ["doing", "done"],
        )

    def test_task_workflow_rejects_non_string_and_huge_shape_values(self) -> None:
        target = self.write_task(entity_id="task-strict-target")
        base = {
            "action": "start",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": target.content_hash,
        }
        invalid = [{**base, "action": value} for value in (True, 1, None)]
        invalid.extend({**base, "kind": value} for value in (True, 1, None))
        invalid.extend(
            {**base, "id": value}
            for value in (True, 1, None, "x" * 101)
        )
        invalid.extend(
            {**base, "base_hash": value}
            for value in (True, 1, None, "f" * 65)
        )
        invalid.extend(
            {**base, "active_resolution": value}
            for value in (True, 1, None)
        )
        for field, values in (
            ("id", (True, 1, None, "x" * 101)),
            ("base_hash", (True, 1, None, "f" * 65)),
            ("action", (True, 1, None, "x" * 101)),
        ):
            for value in values:
                resolution = {
                    "id": "task-active",
                    "base_hash": "a" * 64,
                    "action": "interrupt",
                }
                resolution[field] = value
                invalid.append({**base, "active_resolution": resolution})
        invalid.extend(
            (
                {**base, "extra": True},
                {
                    **base,
                    "active_resolution": {
                        "id": "task-active",
                        "base_hash": "a" * 64,
                        "action": "interrupt",
                        "extra": True,
                    },
                },
                {
                    **base,
                    "active_resolution": {
                        "id": target.entity_id,
                        "base_hash": target.content_hash,
                        "action": "complete",
                    },
                },
            )
        )
        source = self.root / target.relative_path
        before = source.read_bytes()
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_error(self.preview(payload), 400, "invalid_request")
        self.assertEqual(source.read_bytes(), before)

        huge_number_body = (
            b'{"action":"start","kind":"tasks","id":"task-strict-target",'
            b'"base_hash":' + b"9" * 5000 + b"}"
        )
        self.assert_error(
            handle(
                self.store,
                "POST",
                "/api/v1/mutations/preview",
                headers=self.headers,
                body=huge_number_body,
            ),
            400,
            "invalid_request",
        )

        oversized_body = (
            b'{"action":"start","kind":"tasks","id":"'
            + b"x" * 1_048_576
            + b'","base_hash":"'
            + b"a" * 64
            + b'"}'
        )
        self.assert_error(
            handle(
                self.store,
                "POST",
                "/api/v1/mutations/preview",
                headers=self.headers,
                body=oversized_body,
            ),
            413,
            "payload_too_large",
        )
        self.assertEqual(source.read_bytes(), before)

    def test_task_workflow_preview_effects_hash_and_apply_are_exact(self) -> None:
        current = self.write_task(entity_id="task-start")
        source = self.root / current.relative_path
        before = source.read_bytes()
        operation = {
            "action": "start",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": current.content_hash,
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200)
        self.assertEqual(preview["operation"], operation)
        self.assertEqual(preview["validation"], {"ok": True, "errors": []})
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["started"],
        )
        effect = preview["effects"][0]  # type: ignore[index]
        self.assertEqual(
            set(effect),
            {"role", "before", "proposed", "diff", "path_change"},
        )
        self.assertEqual(effect["before"]["content_hash"], current.content_hash)
        self.assertEqual(effect["proposed"]["frontmatter"]["status"], "doing")
        self.assertIn("+status: doing", effect["diff"])
        self.assertEqual(
            effect["path_change"],
            {"from": current.relative_path, "to": current.relative_path},
        )
        canonical = self.body(
            {key: value for key, value in preview.items() if key != "preview_hash"}
        )
        self.assertEqual(preview["preview_hash"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(source.read_bytes(), before)
        self.assertNotIn("integrity", json.dumps(preview))
        self.assertNotIn(str(self.root), json.dumps(preview))

        apply_status, result = self.apply(preview)
        self.assertEqual(apply_status, 200)
        self.assertEqual(
            [item["frontmatter"]["status"] for item in result["effects"]],  # type: ignore[index]
            ["doing"],
        )
        self.assertEqual(
            self.store.get_entity(current.entity_id).frontmatter["status"], "doing"
        )
        self.assert_error(self.apply(preview), 409, "stale_preview")

    def test_calendar_lifecycle_is_in_same_api_preview_and_apply_effect(self) -> None:
        current = self.write_task(
            entity_id="task-calendar-api", **CALENDAR_FIELDS
        )
        started_at = datetime.datetime(
            2026,
            7,
            19,
            9,
            8,
            7,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )
        with mock.patch("webapp.store.current_time", return_value=started_at):
            status, start = self.preview(
                {
                    "action": "start",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": current.content_hash,
                }
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(start["effects"]), 1)
        start_after = start["effects"][0]["proposed"]["frontmatter"]
        self.assertEqual(start_after["started_at"], start_after["work_started_at"])
        self.assertEqual(start_after["calendar_event_kind"], "timed")
        self.assertEqual(self.apply(start)[0], 200)

        stored = self.store.get_entity(current.entity_id)
        completed_at = started_at + datetime.timedelta(minutes=5)
        with mock.patch("webapp.store.current_time", return_value=completed_at):
            status, complete = self.preview(
                {
                    "action": "complete",
                    "kind": "tasks",
                    "id": stored.entity_id,
                    "base_hash": stored.content_hash,
                }
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(complete["effects"]), 1)
        complete_after = complete["effects"][0]["proposed"]["frontmatter"]
        self.assertEqual(
            complete_after["completed_at"], complete_after["work_ended_at"]
        )
        self.assertEqual(complete_after["started_at"], start_after["started_at"])
        self.assertEqual(self.apply(complete)[0], 200)
        self.assertEqual(validate_repository(self.root), [])

    def test_start_switch_preview_orders_all_effects_and_applies_with_200(self) -> None:
        active = self.write_task(entity_id="task-active", status="doing")
        target = self.write_task(entity_id="task-target", status="next")
        status, preview = self.preview(
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": active.entity_id,
                    "base_hash": active.content_hash,
                    "action": "interrupt",
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            ["interrupted", "continuation", "started"],
        )
        self.assertIsNone(preview["effects"][1]["before"])  # type: ignore[index]
        self.assertEqual(self.apply(preview)[0], 200)
        statuses = {
            entity.entity_id: entity.frontmatter["status"]
            for entity in self.store.list_entities()
        }
        self.assertEqual(statuses[active.entity_id], "done")
        self.assertEqual(statuses[target.entity_id], "doing")
        self.assertEqual(len(statuses), 3)

    def test_start_and_create_switch_accept_exact_work_end_time_for_both_resolutions(self) -> None:
        """Catches a switch closing at now instead of the user supplied boundary."""
        active = self.write_task(
            entity_id="task-explicit-switch-active",
            status="doing",
            work_started_at="2026-07-18T22:00:00+09:00",
            resume_status="next",
        )
        target = self.write_task(
            entity_id="task-explicit-switch-target", status="next"
        )
        event_timestamp = "2026-07-19T07:45:30+09:00"
        now = datetime.datetime(
            2026, 7, 19, 9, 8, 7,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )

        for action in ("start", "create_and_start"):
            for resolution in ("complete", "interrupt"):
                with self.subTest(action=action, resolution=resolution):
                    operation = {
                        "action": action,
                        "kind": "tasks",
                        "active_resolution": {
                            "id": active.entity_id,
                            "base_hash": active.content_hash,
                            "action": resolution,
                            "work_ended_at": event_timestamp,
                        },
                    }
                    if action == "start":
                        operation.update(
                            {"id": target.entity_id, "base_hash": target.content_hash}
                        )
                    else:
                        operation.update({"fields": {"title": "Fresh"}, "body": ""})

                    with mock.patch("webapp.store.current_time", return_value=now):
                        status, preview = self.preview(operation)

                    self.assertEqual(status, 200)
                    self.assertEqual(preview["operation"], operation)
                    effects = preview["effects"]
                    self.assertEqual(
                        effects[0]["proposed"]["frontmatter"]["work_ended_at"],
                        event_timestamp,
                    )
                    self.assertEqual(
                        effects[-1]["proposed"]["frontmatter"]["work_started_at"],
                        event_timestamp,
                    )

    def test_quick_start_without_explicit_end_uses_one_server_time_despite_client_clock_skew(self) -> None:
        active = self.write_task(
            entity_id="task-clock-skew-active",
            status="doing",
            work_started_at="2026-09-24T18:01:53+09:00",
            resume_status="next",
        )
        now = datetime.datetime(
            2026, 9, 24, 18, 52, 0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )
        operation = {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": {"title": "Next", "area_id": ""},
            "body": "",
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": active.content_hash,
                "action": "complete",
            },
        }
        with mock.patch("webapp.store.current_time", return_value=now):
            status, preview = self.preview(operation)
            future = {**operation, "active_resolution": {
                **operation["active_resolution"],
                "work_ended_at": "2026-09-24T18:52:30+09:00",
            }}
            self.assert_error(self.preview(future), 400, "invalid_request")
        self.assertEqual(status, 200, preview)
        self.assertEqual(
            preview["effects"][0]["proposed"]["frontmatter"]["work_ended_at"],
            "2026-09-24T18:52:00+09:00",
        )
        self.assertEqual(
            preview["effects"][-1]["proposed"]["frontmatter"]["work_started_at"],
            "2026-09-24T18:52:00+09:00",
        )

    def test_switch_work_end_time_rejects_malformed_future_and_before_start(self) -> None:
        """Catches invalid explicit boundaries entering an atomic Task switch."""
        active = self.write_task(
            entity_id="task-explicit-switch-invalid-active",
            status="doing",
            work_started_at="2026-07-19T08:00:00+09:00",
            resume_status="next",
        )
        target = self.write_task(
            entity_id="task-explicit-switch-invalid-target", status="next"
        )
        now = datetime.datetime(
            2026, 7, 19, 9, 8, 7,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )
        for work_ended_at in (
            "",
            "2026-07-19T08:30+09:00",
            "2026-07-19T09:08:08+09:00",
            "2026-07-19T07:59:59+09:00",
        ):
            with self.subTest(work_ended_at=work_ended_at):
                operation = {
                    "action": "start",
                    "kind": "tasks",
                    "id": target.entity_id,
                    "base_hash": target.content_hash,
                    "active_resolution": {
                        "id": active.entity_id,
                        "base_hash": active.content_hash,
                        "action": "complete",
                        "work_ended_at": work_ended_at,
                    },
                }
                with mock.patch("webapp.store.current_time", return_value=now):
                    self.assert_error(
                        self.preview(operation), 400, "invalid_request"
                    )

        for invalid_value in (None, 123):
            non_string = {
                "action": "create_and_start",
                "kind": "tasks",
                "fields": {"title": "Fresh"},
                "body": "",
                "active_resolution": {
                    "id": active.entity_id,
                    "base_hash": active.content_hash,
                    "action": "interrupt",
                    "work_ended_at": invalid_value,
                },
            }
            self.assert_error(self.preview(non_string), 400, "invalid_request")

    def test_switch_work_end_time_rejects_legacy_active_without_start(self) -> None:
        active = self.write_task(
            entity_id="task-explicit-switch-legacy-active",
            status="doing",
        )
        target = self.write_task(
            entity_id="task-explicit-switch-legacy-target", status="next"
        )
        operation = {
            "action": "start",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": target.content_hash,
            "active_resolution": {
                "id": active.entity_id,
                "base_hash": active.content_hash,
                "action": "complete",
                "work_ended_at": "2026-07-19T08:00:01+09:00",
            },
        }
        with mock.patch("webapp.store.current_time", return_value=datetime.datetime(
            2026, 7, 19, 9, 8, 7,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )):
            self.assert_error(self.preview(operation), 400, "invalid_request")

    def test_consecutive_historical_switches_keep_each_exact_boundary(self) -> None:
        """Catches the second switch reverting either interval boundary to now."""
        active = self.write_task(
            entity_id="task-consecutive-active",
            status="doing",
            work_started_at="2026-07-18T22:00:00+09:00",
            resume_status="next",
        )
        first = self.write_task(entity_id="task-consecutive-first", status="next")
        second = self.write_task(entity_id="task-consecutive-second", status="next")
        now = datetime.datetime(
            2026, 7, 19, 9, 8, 7,
            tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
        )

        def switch(target: Entity, current: Entity, boundary: str) -> Entity:
            operation = {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
                "active_resolution": {
                    "id": current.entity_id,
                    "base_hash": current.content_hash,
                    "action": "complete",
                    "work_ended_at": boundary,
                },
            }
            with mock.patch("webapp.store.current_time", return_value=now):
                preview_status, preview = self.preview(operation)
            self.assertEqual(preview_status, 200)
            self.assertEqual(self.apply(preview)[0], 200)
            return self.store.get_entity(target.entity_id)

        running_first = switch(first, active, "2026-07-19T07:45:30+09:00")
        running_second = switch(
            second, running_first, "2026-07-19T08:10:05+09:00"
        )

        closed_first = self.store.get_entity(first.entity_id)
        self.assertEqual(
            closed_first.frontmatter["work_started_at"],
            "2026-07-19T07:45:30+09:00",
        )
        self.assertEqual(
            closed_first.frontmatter["work_ended_at"],
            "2026-07-19T08:10:05+09:00",
        )
        self.assertEqual(
            running_second.frontmatter["work_started_at"],
            "2026-07-19T08:10:05+09:00",
        )

    def test_task_workflow_tampering_cross_store_staleness_and_concurrency_fail_closed(
        self,
    ) -> None:
        current = self.write_task(entity_id="task-once")
        state = ApiState(self.store)
        _, preview = self.preview(
            {
                "action": "start",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
            },
            state=state,
        )
        tampered = json.loads(json.dumps(preview))
        tampered["effects"][0]["role"] = "completed"
        tampered_without_hash = {
            key: value for key, value in tampered.items() if key != "preview_hash"
        }
        tampered["preview_hash"] = hashlib.sha256(
            self.body(tampered_without_hash)
        ).hexdigest()
        self.assert_error(self.apply(tampered, state=state), 409, "stale_preview")

        other_root = pathlib.Path(self.temporary_directory.name) / "other"
        for directory in (
            "inbox", "tasks", "projects", "goals",
            "reviews/daily", "reviews/weekly", "archive",
        ):
            (other_root / directory).mkdir(parents=True, exist_ok=True)
        self.assert_error(
            self.apply(preview, store=Store(other_root), state=state),
            409,
            "stale_preview",
        )

        barrier = threading.Barrier(3)
        results: list[tuple[int, dict[str, object]]] = []

        def run() -> None:
            barrier.wait()
            results.append(self.apply(preview, state=state))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(status for status, _ in results), [200, 409])

        doing = self.store.get_entity(current.entity_id)
        _, complete_preview = self.preview(
            {
                "action": "complete",
                "kind": "tasks",
                "id": doing.entity_id,
                "base_hash": doing.content_hash,
            }
        )
        self.assertEqual(self.apply(complete_preview)[0], 200)

        stale_target = self.write_task(entity_id="task-stale")
        _, stale_preview = self.preview(
            {
                "action": "start",
                "kind": "tasks",
                "id": stale_target.entity_id,
                "base_hash": stale_target.content_hash,
            }
        )
        self.write_task(entity_id="task-unexpected-doing", status="doing")
        payload = self.assert_error(self.apply(stale_preview), 409, "conflict")
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("task-unexpected-doing", serialized)
        self.assert_error(self.apply(stale_preview), 409, "stale_preview")

    def test_task_workflow_apply_failure_mapping_is_safe_and_consumes_record(self) -> None:
        cases = (
            (
                WorkflowPostCommitCleanupError(
                    (),
                    (OSError("cleanup SECRET /tmp/private"),),
                    (self.root / "SECRET.quarantine",),
                ),
                500,
                "mutation_committed_cleanup_failed",
                {"committed": True},
            ),
            (
                WorkflowRollbackError(
                    (OSError("rollback SECRET /tmp/private"),),
                    (self.root / "SECRET.md",),
                ),
                503,
                "mutation_recovery_required",
                {"recovery_required": True},
            ),
            (OSError("apply SECRET /tmp/private"), 500, "mutation_failed", []),
        )
        for index, (error, expected_status, code, details) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                current = self.write_task(entity_id=f"task-failure-{index}")
                _, preview = self.preview(
                    {
                        "action": "start",
                        "kind": "tasks",
                        "id": current.entity_id,
                        "base_hash": current.content_hash,
                    }
                )
                with mock.patch.object(
                    self.store, "apply_task_workflow", side_effect=error
                ):
                    actual_status, payload = self.apply(preview)
                self.assertEqual(actual_status, expected_status)
                self.assertEqual(payload["error"]["code"], code)  # type: ignore[index]
                self.assertEqual(payload["error"]["details"], details)  # type: ignore[index]
                serialized = json.dumps(payload)
                self.assertNotIn("SECRET", serialized)
                self.assertNotIn("/tmp/private", serialized)
                self.assertNotIn(str(self.root), serialized)
                self.assert_error(self.apply(preview), 409, "stale_preview")

    def test_persistent_recovery_gate_rejects_preview_and_all_apply_writes_safely(
        self,
    ) -> None:
        state = ApiState(self.store)
        status, issued = self.preview(
            {"action": "create", "kind": "goals", "fields": {"title": "Before"}},
            state=state,
        )
        self.assertEqual(status, 200)
        (self.root / ".webapp-mutation-state").write_bytes(b"opaque")

        preview_status, blocked_preview = self.preview(
            {"action": "create", "kind": "tasks", "fields": {"title": "Blocked"}},
            state=state,
        )
        apply_status, blocked_apply = self.apply(issued, state=state)
        self.assertEqual((preview_status, apply_status), (503, 503))
        for payload in (blocked_preview, blocked_apply):
            self.assertEqual(
                payload["error"]["code"],  # type: ignore[index]
                "mutation_recovery_required",
            )
            self.assertEqual(
                payload["error"]["details"],  # type: ignore[index]
                {"recovery_required": True},
            )
            serialized = json.dumps(payload)
            self.assertNotIn(str(self.root), serialized)
            self.assertNotIn(".webapp-mutation-state", serialized)
        self.assert_error(
            self.apply(issued, state=state), 409, "stale_preview"
        )
        self.assertEqual([path for path in self.root.rglob("*.md")], [])

    def test_snapshot_never_exposes_normal_inflight_armed_state(self) -> None:
        target = self.write_task(entity_id="task-inflight-state")
        workflow = self.store.plan_task_start(
            target.entity_id, target.content_hash
        )
        entered = threading.Event()
        release = threading.Event()
        real_apply = self.store._apply_task_workflow_locked

        def pause_after_arm(plan):
            entered.set()
            self.assertTrue(release.wait(2.0))
            return real_apply(plan)

        apply_errors: list[BaseException] = []
        snapshots: list[tuple[int, dict[str, object]]] = []
        with mock.patch.object(
            self.store, "_apply_task_workflow_locked", side_effect=pause_after_arm
        ):
            apply_thread = threading.Thread(
                target=lambda: self._capture_thread_error(
                    apply_errors, self.store.apply_task_workflow, workflow
                )
            )
            apply_thread.start()
            self.assertTrue(entered.wait(2.0))
            snapshot_thread = threading.Thread(
                target=lambda: snapshots.append(
                    handle(self.store, "GET", "/api/v1/snapshot")
                )
            )
            snapshot_thread.start()
            snapshot_thread.join(0.05)
            self.assertTrue(snapshot_thread.is_alive())
            release.set()
            apply_thread.join(2.0)
            snapshot_thread.join(2.0)
        self.assertEqual(apply_errors, [])
        self.assertEqual(snapshots[0][0], 200)
        self.assertEqual(
            snapshots[0][1]["mutation_state"], {"recovery_required": False}
        )
        target_summaries = [
            entity
            for entity in snapshots[0][1]["entities"]
            if entity["id"] == target.entity_id
        ]
        self.assertEqual(target_summaries[0]["frontmatter"]["status"], "doing")

    def test_committed_idle_transition_failure_is_committed_and_persistently_gated(
        self,
    ) -> None:
        target = self.write_task(entity_id="task-state-transition")
        state = ApiState(self.store)
        _, preview = self.preview(
            {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": target.content_hash,
            },
            state=state,
        )
        real_atomic_write = __import__(
            "webapp.store", fromlist=["atomic_write"]
        ).atomic_write

        def fail_idle(path, data):
            if data == b"idle\n":
                raise OSError("SECRET idle transition /tmp/private")
            return real_atomic_write(path, data)

        with mock.patch("webapp.store.atomic_write", side_effect=fail_idle):
            status, payload = self.apply(preview, state=state)
        self.assertEqual(status, 500)
        self.assertEqual(
            payload["error"],  # type: ignore[index]
            {
                "code": "mutation_committed_cleanup_failed",
                "message": "mutation committed but cleanup could not be completed",
                "details": {"committed": True},
            },
        )
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual(
            self.store.get_entity(target.entity_id).frontmatter["status"], "doing"
        )
        snapshot_status, snapshot = handle(
            self.store, "GET", "/api/v1/snapshot"
        )
        self.assertEqual(snapshot_status, 200)
        self.assertEqual(
            snapshot["mutation_state"], {"recovery_required": True}
        )
        self.assert_error(self.apply(preview, state=state), 409, "stale_preview")

    @staticmethod
    def _capture_thread_error(errors, callback, *args) -> None:
        try:
            callback(*args)
        except BaseException as error:
            errors.append(error)

    def test_expand_preserves_legacy_preview_and_apply_wire_contract(self) -> None:
        status, preview = self.preview(
            {"action": "create", "kind": "tasks", "fields": {"title": "Legacy"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(preview),
            {
                "operation", "before", "proposed", "diff", "path_change",
                "source_paths", "destination_paths", "validation", "base_hash",
                "preview_hash",
            },
        )
        self.assertNotIn("effects", preview)
        apply_status, applied = self.apply(preview)
        self.assertEqual(apply_status, 201, applied)
        self.assertEqual(applied["id"], preview["proposed"]["id"])
        self.assertEqual(
            self.store.get_entity(applied["id"]).content_hash,
            preview["proposed"]["content_hash"],
        )

    def test_external_edit_conflict_returns_safe_latest_and_consumes_preview(self) -> None:
        current = self.write_task()
        _, preview = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "fields": {"title": "Proposed"},
            }
        )
        source = self.root / current.relative_path
        external = source.read_text(encoding="utf-8").replace("title: Before", "title: External")
        source.write_text(external, encoding="utf-8")

        payload = self.assert_error(self.apply(preview), 409, "conflict")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("Proposed", serialized)
        self.assertIn("External", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertEqual(source.read_text(encoding="utf-8"), external)
        self.assert_error(self.apply(preview), 409, "stale_preview")

    def test_apply_lock_timeout_is_busy_before_write_and_consumes_preview(
        self,
    ) -> None:
        current = self.write_task(entity_id="task-busy-apply")
        _, preview = self.preview({
            "action": "update",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": current.content_hash,
            "fields": {"title": "Proposed"},
        })
        source = self.root / current.relative_path
        before = source.read_bytes()
        entered = threading.Event()
        release = threading.Event()

        def hold_mutation_lock() -> None:
            with self.store.mutation_lock():
                entered.set()
                self.assertTrue(release.wait(2.0))

        with mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.05):
            holder = threading.Thread(target=hold_mutation_lock)
            holder.start()
            self.assertTrue(entered.wait(2.0))
            try:
                self.assert_error(self.apply(preview), 503, "busy")
            finally:
                release.set()
                holder.join(2.0)

        self.assertFalse(holder.is_alive())
        self.assertEqual(source.read_bytes(), before)
        self.assert_error(self.apply(preview), 409, "stale_preview")

    def test_destination_winner_conflict_preserves_source_and_winner(self) -> None:
        current = self.write_task(status="inbox")
        _, preview = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "fields": {"status": "next"},
            }
        )
        source = self.root / current.relative_path
        before = source.read_bytes()
        destination = self.root / preview["path_change"]["to"]  # type: ignore[index]
        winner = b"external winner\n"
        destination.write_bytes(winner)

        self.assert_error(self.apply(preview), 409, "conflict")
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(destination.read_bytes(), winner)

    def test_conflict_never_discloses_a_different_current_kind(self) -> None:
        current = self.write_task()
        _, preview = self.preview(
            {
                "action": "update",
                "kind": "tasks",
                "id": current.entity_id,
                "base_hash": current.content_hash,
                "fields": {"title": "Proposed"},
            }
        )
        changed_kind = Entity(
            entity_id=current.entity_id,
            entity_type="project",
            relative_path="projects/secret.md",
            frontmatter={"id": current.entity_id, "type": "project", "title": "Secret"},
            body="private\n",
            content_hash="f" * 64,
        )
        with mock.patch.object(
            self.store,
            "apply_mutation_plan",
            side_effect=ConflictError(changed_kind),
        ):
            payload = self.assert_error(self.apply(preview), 409, "conflict")
        serialized = json.dumps(payload)
        self.assertNotIn("project", serialized)
        self.assertNotIn("secret", serialized.lower())

    def test_preview_kind_change_race_is_generic_not_found_without_details(self) -> None:
        current = self.write_task()
        changed_kind = Entity(
            entity_id=current.entity_id,
            entity_type="project",
            relative_path="projects/SECRET-race.md",
            frontmatter={
                "id": current.entity_id,
                "type": "project",
                "title": "SECRET frontmatter",
            },
            body="SECRET body /home/private\n",
            content_hash="e" * 64,
        )
        cases = (
            (
                "plan_update_entity",
                {
                    "action": "update",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": current.content_hash,
                    "fields": {"title": "After"},
                },
            ),
            (
                "plan_archive_entity",
                {
                    "action": "archive",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": current.content_hash,
                },
            ),
        )
        for method_name, request in cases:
            with self.subTest(method=method_name):
                with mock.patch.object(
                    self.store,
                    method_name,
                    side_effect=ConflictError(changed_kind),
                ):
                    result = self.preview(request)
                payload = self.assert_error(result, 404, "not_found")
                self.assertEqual(payload["error"]["details"], [])  # type: ignore[index]
                serialized = json.dumps(payload)
                for forbidden in (
                    "project",
                    "SECRET",
                    "projects/",
                    "/home/private",
                    "e" * 64,
                ):
                    self.assertNotIn(forbidden, serialized)

    def test_preview_conflict_preserves_legacy_latest_but_workflow_is_generic(
        self,
    ) -> None:
        current = self.write_task(body="Latest body\n", title="Latest title")
        latest = {
            "id": current.entity_id,
            "kind": "tasks",
            "type": "task",
            "path": current.relative_path,
            "frontmatter": dict(current.frontmatter),
            "body": current.body,
            "content_hash": current.content_hash,
            "archived": False,
        }
        legacy_cases = (
            (
                "plan_update_entity",
                {
                    "action": "update",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": "0" * 64,
                    "fields": {"title": "Proposed"},
                },
            ),
            (
                "plan_archive_entity",
                {
                    "action": "archive",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": "0" * 64,
                },
            ),
        )
        for method_name, operation in legacy_cases:
            with self.subTest(action=operation["action"]):
                with mock.patch.object(
                    self.store,
                    method_name,
                    side_effect=ConflictError(current),
                ):
                    status, payload = self.preview(operation)
                self.assertEqual(status, 409)
                self.assertEqual(
                    payload,
                    {
                        "error": {
                            "code": "conflict",
                            "message": (
                                "repository state changed; create a new preview"
                            ),
                            "details": [
                                {
                                    "reason": "current_state_changed",
                                    "latest": latest,
                                    "latest_hash": current.content_hash,
                                }
                            ],
                        }
                    },
                )

        with mock.patch.object(
            self.store,
            "plan_task_start",
            side_effect=ConflictError(current),
        ):
            status, payload = self.preview(
                {
                    "action": "start",
                    "kind": "tasks",
                    "id": current.entity_id,
                    "base_hash": current.content_hash,
                }
            )
        self.assertEqual(status, 409)
        self.assertEqual(
            payload,
            {
                "error": {
                    "code": "conflict",
                    "message": "mutation conflicts with repository state",
                    "details": [],
                }
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("Latest title", serialized)
        self.assertNotIn("Latest body", serialized)

    def test_schema_errors_are_structured_without_raw_values_or_unrelated_paths(self) -> None:
        operation = {"action": "create", "kind": "tasks", "fields": {"title": "X"}}
        unsafe_errors = [
            "/home/example/SECRET.md: invalid document",
            "unrelated/SECRET.md: SECRET=/home/private",
            "unparseable SECRET=/home/private",
        ]
        with mock.patch.object(
            self.store,
            "plan_create_entity",
            side_effect=SchemaError(unsafe_errors),
        ):
            result = self.preview(operation)
        payload = self.assert_error(result, 422, "validation_failed")
        self.assertEqual(
            payload["error"]["details"],  # type: ignore[index]
            [{"code": "repository_validation_failed"}],
        )
        serialized = json.dumps(payload)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("unrelated/", serialized)

        _, preview = self.preview(operation)
        planned_path = preview["proposed"]["path"]  # type: ignore[index]
        apply_errors = [
            f"{planned_path}: SECRET=/home/private",
            "unrelated/SECRET.md: invalid",
            "/home/example/SECRET.md: invalid",
        ]
        with mock.patch.object(
            self.store,
            "apply_mutation_plan",
            side_effect=SchemaError(apply_errors),
        ):
            apply_payload = self.assert_error(self.apply(preview), 422, "validation_failed")
        self.assertEqual(
            apply_payload["error"]["details"],  # type: ignore[index]
            [
                {"path": planned_path, "code": "invalid_document"},
                {"code": "repository_validation_failed"},
            ],
        )
        apply_serialized = json.dumps(apply_payload)
        self.assertNotIn("SECRET", apply_serialized)
        self.assertNotIn("/home/", apply_serialized)
        self.assertNotIn("unrelated/", apply_serialized)

    def test_error_mapping_does_not_leak_exception_text(self) -> None:
        operation = {"action": "create", "kind": "tasks", "fields": {"title": "X"}}
        mappings = (
            (InputError("private body /tmp/secret"), 400, "invalid_request"),
            (DestinationConflict("/tmp/secret"), 409, "conflict"),
            (MutationPlanConflict("/tmp/secret"), 409, "conflict"),
            (SchemaError(["tasks/x.md: invalid status"]), 422, "validation_failed"),
            (StoreLockTimeout("/tmp/secret"), 503, "busy"),
            (OSError("/tmp/secret"), 500, "mutation_failed"),
        )
        for error, expected_status, code in mappings:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.store, "plan_create_entity", side_effect=error):
                    result = self.preview(operation)
                payload = self.assert_error(result, expected_status, code)
                self.assertNotIn("/tmp/secret", json.dumps(payload))

    def _prepare_roadmap_api(self) -> None:
        (self.root / "roadmap-outcomes").mkdir(exist_ok=True)
        (self.root / "cycles").mkdir(exist_ok=True)

    def _api_create(self, kind: str, fields: dict[str, str], body: str = "Body") -> dict[str, object]:
        status, preview = self.preview(
            {"action": "create", "kind": kind, "fields": fields, "body": body}
        )
        self.assertEqual(status, 200, preview)
        applied_status, result = self.apply(preview)
        self.assertEqual(applied_status, 201, result)
        return result

    def test_roadmap_kinds_facts_and_filter_are_exposed_by_read_api(self) -> None:
        """Catches missing API kinds, Roadmap facts, or Project/Task filter propagation."""
        self._prepare_roadmap_api()
        goal = self._api_create("goals", {"title": "Goal"})
        outcome = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Outcome",
                "goal_id": goal["id"],
                "roadmap_lane": "next",
                "roadmap_position": "1",
            },
            "## 成功条件\n\n完了済み\n\n## メモ\n",
        )
        project = self._api_create(
            "projects",
            {"title": "Project", "roadmap_outcome_id": outcome["id"]},
        )
        task = self._api_create(
            "tasks", {"title": "Task", "project_id": project["id"]}
        )

        status, snapshot = handle(
            self.store,
            "GET",
            "/api/v1/snapshot",
            query={"roadmap_outcome_id": outcome["id"]},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [entity["id"] for entity in snapshot["entities"]],
            [task["id"], project["id"]],
        )
        self.assertEqual(
            snapshot["facts"]["roadmap"],
            {
                "active_cycle_id": None,
                "now_outcome_ids": [],
                "next_outcome_ids": [outcome["id"]],
                "later_outcome_ids": [],
                "planned_cycle_ids": [],
            },
        )

    def test_roadmap_outcome_target_date_round_trips_and_can_be_cleared(self) -> None:
        """Catches missing Outcome arrival-estimate create/update/read support."""
        self._prepare_roadmap_api()
        goal = self._api_create("goals", {"title": "Goal"})
        outcome = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Dated Outcome",
                "goal_id": goal["id"],
                "roadmap_lane": "next",
                "roadmap_position": "1",
                "target_date": "2027-01-31",
            },
            "## 成功条件\n\n完了済み\n\n## メモ\n",
        )
        self.assertEqual(
            outcome["frontmatter"]["target_date"], "2027-01-31"  # type: ignore[index]
        )

        snapshot_status, snapshot = handle(self.store, "GET", "/api/v1/snapshot")
        self.assertEqual(snapshot_status, 200)
        snapshot_outcome = next(
            entity for entity in snapshot["entities"]  # type: ignore[index]
            if entity["id"] == outcome["id"]
        )
        self.assertEqual(snapshot_outcome["frontmatter"]["target_date"], "2027-01-31")
        detail_status, detail = handle(
            self.store, "GET", f"/api/v1/entities/roadmap_outcomes/{outcome['id']}"
        )
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["frontmatter"]["target_date"], "2027-01-31")  # type: ignore[index]

        invalid = self.preview(
            {
                "action": "create",
                "kind": "roadmap_outcomes",
                "fields": {
                    "title": "Invalid Outcome",
                    "goal_id": goal["id"],
                    "roadmap_lane": "later",
                    "roadmap_position": "1",
                    "target_date": "2027-02-30",
                },
                "body": "## 成功条件\n\n完了済み\n\n## メモ\n",
            }
        )
        self.assert_error(invalid, 400, "invalid_request")

        omitted = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Undated Outcome",
                "goal_id": goal["id"],
                "roadmap_lane": "later",
                "roadmap_position": "1",
            },
            "## 成功条件\n\n完了済み\n\n## メモ\n",
        )
        self.assertNotIn("target_date", omitted["frontmatter"])  # type: ignore[index]

        clear = self.preview(
            {
                "action": "update",
                "kind": "roadmap_outcomes",
                "id": outcome["id"],
                "base_hash": outcome["content_hash"],
                "fields": {"target_date": ""},
                "body": None,
            }
        )
        self.assertEqual(clear[0], 200)
        cleared_status, cleared = self.apply(clear[1])
        self.assertEqual(cleared_status, 200)
        self.assertNotIn("target_date", cleared["frontmatter"])  # type: ignore[index]

    def test_roadmap_outcome_update_preserves_existing_non_tail_position(self) -> None:
        """Catches ordinary updates being rejected as invalid lane appends."""
        self._prepare_roadmap_api()
        goal = self._api_create("goals", {"title": "Goal"})
        created_at = datetime.datetime(
            2026, 8, 26, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        )
        with mock.patch("webapp.store.current_time", return_value=created_at):
            first = self._api_create(
                "roadmap_outcomes",
                {
                    "title": "First Outcome",
                    "goal_id": goal["id"],
                    "roadmap_lane": "next",
                    "roadmap_position": "1",
                },
                "## 成功条件\n\n完了済み\n\n## メモ\n",
            )
            second = self._api_create(
                "roadmap_outcomes",
                {
                    "title": "Second Outcome",
                    "goal_id": goal["id"],
                    "roadmap_lane": "next",
                    "roadmap_position": "2",
                },
                "## 成功条件\n\n完了済み\n\n## メモ\n",
            )

        updated_at = created_at + datetime.timedelta(minutes=1)
        with mock.patch("webapp.store.current_time", return_value=updated_at):
            preview_status, preview = self.preview(
                {
                    "action": "update",
                    "kind": "roadmap_outcomes",
                    "id": first["id"],
                    "base_hash": first["content_hash"],
                    "fields": {"target_date": "2027-01-31"},
                    "body": None,
                }
            )
        self.assertEqual(preview_status, 200, preview)
        applied_status, applied = self.apply(preview)
        self.assertEqual(applied_status, 200, applied)
        self.assertEqual(applied["frontmatter"]["target_date"], "2027-01-31")
        self.assertEqual(applied["frontmatter"]["roadmap_position"], "1")

        second_status, second_detail = handle(
            self.store,
            "GET",
            f"/api/v1/entities/roadmap_outcomes/{second['id']}",
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_detail["frontmatter"]["roadmap_position"], "2")

    def test_roadmap_move_activate_and_close_use_one_shot_workflow_preview_apply(self) -> None:
        """Catches dedicated actions bypassing exact effects or reusable apply."""
        self._prepare_roadmap_api()
        goal = self._api_create("goals", {"title": "Goal"})
        outcome = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Outcome",
                "goal_id": goal["id"],
                "roadmap_lane": "next",
                "roadmap_position": "1",
            },
            "## 成功条件\n\n完了済み\n\n## メモ\n",
        )
        cycle = self._api_create(
            "cycles",
            {
                "title": "Cycle",
                "start_date": "2026-08-25",
                "end_date": "2026-10-05",
                "outcome_ids": f"[{outcome['id']}]",
            },
            "## 振り返り\n\n## メモ\n",
        )
        activate_status, activate = self.preview(
            {
                "action": "cycle_activate",
                "kind": "cycles",
                "id": cycle["id"],
                "base_hash": cycle["content_hash"],
            }
        )
        self.assertEqual(activate_status, 200, activate)
        self.assertEqual([effect["role"] for effect in activate["effects"]], ["cycle_activated", "outcome_now"])
        applied_status, activated = self.apply(activate)
        self.assertEqual(applied_status, 200, activated)
        self.assert_error(self.apply(activate), 409, "stale_preview")

        current_cycle = self.store.get_entity(cycle["id"])
        close_status, close = self.preview(
            {
                "action": "cycle_close",
                "kind": "cycles",
                "id": cycle["id"],
                "base_hash": current_cycle.content_hash,
                "status": "completed",
                "outcome_results": {outcome["id"]: "next"},
                "retrospective": "次へ進める。",
            }
        )
        self.assertEqual(close_status, 200, close)
        self.assertEqual([effect["role"] for effect in close["effects"]], ["cycle_closed", "outcome_next"])
        self.assertEqual(self.apply(close)[0], 200)

    def test_roadmap_rejections_use_stable_documented_error_codes(self) -> None:
        """Catches Roadmap validation collapsing into generic invalid_request."""
        self._prepare_roadmap_api()
        invalid_move = {
            "action": "roadmap_move",
            "kind": "roadmap_outcomes",
            "id": "roadmap-outcome-missing",
            "base_hash": "0" * 64,
            "lane": "soon",
            "position": 0,
        }
        self.assert_error(self.preview(invalid_move), 400, "roadmap_operation_invalid")

        goal = self._api_create("goals", {"title": "Goal"})
        outcome = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Outcome",
                "goal_id": goal["id"],
                "roadmap_lane": "next",
                "roadmap_position": "1",
            },
            "## 成功条件\n\n\n## メモ\n",
        )
        cycle = self._api_create(
            "cycles",
            {
                "title": "Cycle",
                "start_date": "2026-08-25",
                "end_date": "2026-10-05",
                "outcome_ids": f"[{outcome['id']}]",
            },
            "## 振り返り\n\n## メモ\n",
        )
        self.assert_error(
            self.preview(
                {
                    "action": "cycle_activate",
                    "kind": "cycles",
                    "id": cycle["id"],
                    "base_hash": cycle["content_hash"],
                }
            ),
            422,
            "cycle_success_condition_required",
        )

    def test_roadmap_bundle_api_previews_and_applies_mixed_project_changes_once(self) -> None:
        """Catches the API splitting one reviewed bundle into independent applies."""
        self._prepare_roadmap_api()
        goal = self._api_create("goals", {"title": "Goal"})
        outcome = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Outcome",
                "goal_id": goal["id"],
                "roadmap_lane": "next",
                "roadmap_position": "1",
            },
            "## 成功条件\n\n確認済み\n\n## メモ\n",
        )
        other = self._api_create(
            "roadmap_outcomes",
            {
                "title": "Other",
                "goal_id": goal["id"],
                "roadmap_lane": "later",
                "roadmap_position": "1",
            },
            "## 成功条件\n\n確認済み\n\n## メモ\n",
        )
        updated = self._api_create(
            "projects",
            {"title": "Update", "roadmap_outcome_id": outcome["id"]},
        )
        moved = self._api_create(
            "projects",
            {"title": "Move", "roadmap_outcome_id": other["id"]},
        )
        archived = self._api_create(
            "projects",
            {"title": "Archive", "roadmap_outcome_id": outcome["id"]},
        )
        child = self._api_create(
            "tasks", {"title": "Child", "project_id": archived["id"]}
        )
        operation = {
            "action": "roadmap_outcome_bundle_update",
            "kind": "roadmap_outcomes",
            "id": outcome["id"],
            "base_hash": outcome["content_hash"],
            "fields": {"title": "Updated Outcome"},
            "project_changes": [
                {
                    "action": "create",
                    "fields": {
                        "title": "Created",
                        "roadmap_outcome_id": outcome["id"],
                    },
                },
                {
                    "action": "update",
                    "id": updated["id"],
                    "base_hash": updated["content_hash"],
                    "fields": {"title": "Updated Project"},
                },
                {
                    "action": "move",
                    "id": moved["id"],
                    "base_hash": moved["content_hash"],
                },
                {
                    "action": "archive",
                    "id": archived["id"],
                    "base_hash": archived["content_hash"],
                    "task_cascade": [
                        {"id": child["id"], "base_hash": child["content_hash"]}
                    ],
                },
            ],
        }

        status, preview = self.preview(operation)

        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["operation"]["body"], None)  # type: ignore[index]
        self.assertEqual(
            preview["operation"]["project_changes"][0]["body"],  # type: ignore[index]
            "",
        )
        self.assertEqual(
            preview["operation"]["project_changes"][1]["body"],  # type: ignore[index]
            None,
        )
        self.assertEqual(
            [effect["role"] for effect in preview["effects"]],  # type: ignore[index]
            [
                "outcome_updated",
                "project_created",
                "project_updated",
                "project_moved",
                "task_archived",
                "project_archived",
            ],
        )
        moved_effect = preview["effects"][3]  # type: ignore[index]
        self.assertEqual(
            moved_effect["before"]["frontmatter"]["roadmap_outcome_id"],
            other["id"],
        )
        self.assertEqual(
            moved_effect["proposed"]["frontmatter"]["roadmap_outcome_id"],
            outcome["id"],
        )

        applied_status, result = self.apply(preview)

        self.assertEqual(applied_status, 200, result)
        self.assertEqual(
            [effect["id"] for effect in result["effects"]],  # type: ignore[index]
            [effect["proposed"]["id"] for effect in preview["effects"]],  # type: ignore[index]
        )
        self.assertEqual(validate_repository(self.root), [])


if __name__ == "__main__":
    unittest.main()
