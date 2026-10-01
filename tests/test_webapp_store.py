import contextlib
import datetime
import dataclasses
import fcntl
import hashlib
import multiprocessing
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from scripts.validate_frontmatter import (
    REQUIRED_KEYS,
    parse_frontmatter,
    validate_document_map,
    validate_repository,
)
from webapp.store import (
    ConflictError,
    DestinationConflict,
    InputError,
    MutationPlan,
    MutationPlanConflict,
    MutationRecoveryRequired,
    NotFoundError,
    ProjectOperationError,
    RoadmapOperationError,
    SchemaError,
    Store,
    StoreLockTimeout,
    WorkflowPlan,
    WorkflowPostCommitCleanupError,
    WorkflowRollbackError,
    _rename_no_replace,
    _link_atomic_create_no_replace,
    atomic_write,
    safe_unlink,
    serialize_frontmatter,
    split_document,
)


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
CALENDAR_FIELDS = {
    "calendar_id": "example@group.calendar.google.com",
    "calendar_event_id": "event-calendar-workflow",
    "calendar_event_url": "https://calendar.google.com/calendar/event?eid=ZXZlbnQ",
    "calendar_event_kind": "all_day",
}
PURPOSE_BODY = "## Purpose\n\n## Principles\n"


def _hold_repository_lock(
    lock_path: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(5.0)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _hold_store_mutation_lock(
    root: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    attempting: multiprocessing.synchronize.Event | None = None,
) -> None:
    store = Store(pathlib.Path(root))
    if attempting is not None:
        attempting.set()
    with store.mutation_lock():
        entered.set()
        release.wait(5.0)


def _create_task_in_process(
    root: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    start.wait(5.0)
    try:
        entity = Store(pathlib.Path(root)).create_entity(
            "task", {"title": "Concurrent capture"}, "Body"
        )
        results.put((entity.entity_id, entity.relative_path, None))
    except BaseException as error:
        results.put((None, None, repr(error)))


class SerializeFrontmatterTest(unittest.TestCase):
    def test_task_template_round_trips_through_existing_parser(self) -> None:
        template_path = REPOSITORY_ROOT / "templates" / "task.md"
        template_frontmatter = parse_frontmatter(template_path)
        expected = {
            key: value
            for key, value in template_frontmatter.items()
            if key in REQUIRED_KEYS["task"] or value
        }

        serialized = serialize_frontmatter(template_frontmatter, "task")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = pathlib.Path(temporary_directory) / "task.md"
            path.write_text(serialized, encoding="utf-8")

            self.assertEqual(parse_frontmatter(path), expected)

    def test_required_and_optional_keys_have_exact_deterministic_order(self) -> None:
        frontmatter = {
            "completed_at": "2026-07-18T10:45:00+09:00",
            "waiting_for": "reply",
            "work_started_at": "2026-07-18T10:00:00+09:00",
            "work_ended_at": "2026-07-18T10:45:00+09:00",
            "resume_status": "scheduled",
            "continuation_of": "task-20260716-099",
            "updated_at": "2026-07-17T10:30:00+09:00",
            "calendar_event_kind": "timed",
            "contexts": "[home, phone]",
            "id": "task-20260717-001",
            "calendar_event_url": "https://calendar.google.com/calendar/event?eid=ZXZlbnQ",
            "scheduled_end": "2026-07-18T11:00:00+09:00",
            "title": "Call",
            "calendar_id": "example@group.calendar.google.com",
            "project_id": "project-20260717-001",
            "created_at": "2026-07-17T10:00:00+09:00",
            "estimated_minutes": "15",
            "available_from": "2026-07-19",
            "due": "2026-07-19",
            "calendar_event_id": "event_20260718T010000Z",
            "type": "task",
            "scheduled_start": "2026-07-18T10:00:00+09:00",
            "started_at": "2026-07-18T10:00:00+09:00",
            "status": "done",
        }

        serialized = serialize_frontmatter(frontmatter, "task")

        self.assertEqual(
            serialized.splitlines(),
            [
                "---",
                "id: task-20260717-001",
                "type: task",
                "title: Call",
                "status: done",
                "created_at: 2026-07-17T10:00:00+09:00",
                "updated_at: 2026-07-17T10:30:00+09:00",
                "project_id: project-20260717-001",
                "due: 2026-07-19",
                "scheduled_start: 2026-07-18T10:00:00+09:00",
                "scheduled_end: 2026-07-18T11:00:00+09:00",
                "available_from: 2026-07-19",
                "contexts: [home, phone]",
                "estimated_minutes: 15",
                "waiting_for: reply",
                "work_started_at: 2026-07-18T10:00:00+09:00",
                "work_ended_at: 2026-07-18T10:45:00+09:00",
                "resume_status: scheduled",
                "continuation_of: task-20260716-099",
                'calendar_id: "example@group.calendar.google.com"',
                "calendar_event_id: event_20260718T010000Z",
                'calendar_event_url: "https://calendar.google.com/calendar/event?eid=ZXZlbnQ"',
                "calendar_event_kind: timed",
                "started_at: 2026-07-18T10:00:00+09:00",
                "completed_at: 2026-07-18T10:45:00+09:00",
                "---",
            ],
        )
        self.assertTrue(serialized.endswith("---\n"))

    def test_empty_optional_values_are_omitted(self) -> None:
        frontmatter = self.required_task(project_id="", due="", contexts="[]")

        serialized = serialize_frontmatter(frontmatter, "task")

        self.assertNotIn("project_id:", serialized)
        self.assertNotIn("due:", serialized)
        self.assertIn("contexts: []\n", serialized)

    def test_empty_required_value_is_not_omitted(self) -> None:
        frontmatter = self.required_task(title="")

        serialized = serialize_frontmatter(frontmatter, "task")

        self.assertIn('title: ""\n', serialized)

    def test_japanese_title_is_quoted_and_round_trips(self) -> None:
        frontmatter = self.required_task(title="歯科検診を予約する")

        serialized = serialize_frontmatter(frontmatter, "task")
        parsed, _ = split_document(serialized)

        self.assertIn('title: "歯科検診を予約する"\n', serialized)
        self.assertEqual(parsed, frontmatter)

    def test_contexts_inline_list_stays_unquoted_and_round_trips(self) -> None:
        frontmatter = self.required_task(contexts="[home, phone]")

        serialized = serialize_frontmatter(frontmatter, "task")
        parsed, _ = split_document(serialized)

        self.assertIn("contexts: [home, phone]\n", serialized)
        self.assertEqual(parsed, frontmatter)

    def test_invalid_values_keys_and_entity_types_raise_input_error(self) -> None:
        cases = {
            "double quote": (self.required_task(title='Call "home"'), "task"),
            "line feed": (self.required_task(title="Call\nhome"), "task"),
            "carriage return": (self.required_task(title="Call\rhome"), "task"),
            "comment-like": (self.required_task(title="# hidden"), "task"),
            "unknown key": (self.required_task(extra="value"), "task"),
            "forbidden key": (self.required_task(priority="high"), "task"),
            "unknown entity type": (self.required_task(), "area"),
        }

        for name, (frontmatter, entity_type) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(InputError):
                    serialize_frontmatter(frontmatter, entity_type)

    def test_missing_required_key_raises_input_error(self) -> None:
        frontmatter = self.required_task()
        del frontmatter["title"]

        with self.assertRaises(InputError):
            serialize_frontmatter(frontmatter, "task")

    def test_non_string_required_value_raises_input_error(self) -> None:
        frontmatter = self.required_task()
        frontmatter["title"] = None  # type: ignore[assignment]

        with self.assertRaises(InputError):
            serialize_frontmatter(frontmatter, "task")

    def required_task(self, **overrides: str) -> dict[str, str]:
        frontmatter = {
            "id": "task-20260717-001",
            "type": "task",
            "title": "Call",
            "status": "next",
            "created_at": "2026-07-17T10:00:00+09:00",
            "updated_at": "2026-07-17T10:00:00+09:00",
        }
        frontmatter.update(overrides)
        return frontmatter


class SplitDocumentTest(unittest.TestCase):
    def test_preserves_body_and_later_delimiter_line(self) -> None:
        text = """\
---
id: task-20260717-001
title: "電話する"
---

## Notes

before
---
after
"""

        frontmatter, body = split_document(text)

        self.assertEqual(
            frontmatter,
            {"id": "task-20260717-001", "title": "電話する"},
        )
        self.assertEqual(body, "\n## Notes\n\nbefore\n---\nafter\n")

    def test_allows_blank_and_comment_lines_and_strips_matching_quotes(self) -> None:
        text = " --- \n\n" + """\
  # comment
single: 'value'
double: "value two"
unmatched: 'value
---
body"""

        frontmatter, body = split_document(text)

        self.assertEqual(
            frontmatter,
            {"single": "value", "double": "value two", "unmatched": "'value"},
        )
        self.assertEqual(body, "body")

    def test_rejects_invalid_frontmatter_structure(self) -> None:
        cases = {
            "missing opening": "id: task-1\n---\n",
            "missing closing": "---\nid: task-1\n",
            "indented nested line": "---\nmeta:\n  title: nested\n---\n",
            "missing colon": "---\nid task-1\n---\n",
            "empty key": "---\n: value\n---\n",
            "duplicate key": "---\nid: task-1\nid: task-2\n---\n",
        }

        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(InputError):
                    split_document(text)


class StoreTest(unittest.TestCase):
    def test_root_must_resolve_to_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)

            with self.assertRaises(InputError):
                Store(temporary_path / "missing")

            file_path = temporary_path / "file"
            file_path.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(InputError):
                Store(file_path)

    def test_lists_supported_entities_deterministically_with_exact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            documents = {
                "inbox/inbox.md": self.document(
                    "task-20260717-001", "task", "Inbox body\n", status="inbox"
                ),
                "tasks/nested/task.md": self.document(
                    "task-20260717-002", "task", "Task body\n", status="next"
                ),
                "projects/project.md": self.document(
                    "project-20260717-001", "project", "Project body\n"
                ),
                "goals/goal.md": self.document(
                    "goal-20260717-001", "goal", "Goal body\n"
                ),
                "reviews/daily/daily.md": self.document(
                    "review-daily-20260717-001",
                    "review",
                    "Daily body\n",
                    review_kind="daily",
                ),
                "reviews/weekly/weekly.md": self.document(
                    "review-weekly-20260717-001",
                    "review",
                    "Weekly body\n",
                    review_kind="weekly",
                ),
                "archive/2026/task.md": self.document(
                    "task-20260716-001", "task", "Archived body\n", status="done"
                ),
            }
            for relative_path, content in reversed(tuple(documents.items())):
                self.write(root / relative_path, content)
            for readme_path in (root / "tasks/README.md", root / "archive/README.md"):
                self.write(readme_path, "not an entity")
            self.write(
                root / "private/ignored.md",
                self.document(
                    "task-20260717-999", "task", "Private body\n", status="next"
                ),
            )

            entities = Store(root).list_entities()

            expected_paths = sorted(documents)
            self.assertEqual(
                [entity.relative_path for entity in entities], expected_paths
            )
            by_path = {entity.relative_path: entity for entity in entities}
            nested_task = by_path["tasks/nested/task.md"]
            self.assertEqual(nested_task.entity_id, "task-20260717-002")
            self.assertEqual(nested_task.entity_type, "task")
            self.assertEqual(nested_task.frontmatter["status"], "next")
            self.assertEqual(nested_task.body, "Task body\n")
            self.assertEqual(
                nested_task.content_hash,
                hashlib.sha256(documents["tasks/nested/task.md"]).hexdigest(),
            )
            self.assertEqual(
                {entity.entity_type for entity in entities},
                {"task", "project", "goal", "review"},
            )

    def test_excludes_symlink_files_and_everything_below_symlink_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = pathlib.Path(temporary_directory)
            root = base / "repository"
            tasks = root / "tasks"
            tasks.mkdir(parents=True)
            self.write(
                tasks / "ordinary.md",
                self.document(
                    "task-20260717-001", "task", "Ordinary\n", status="next"
                ),
            )
            inside_file = root / "unscanned" / "inside.md"
            inside_directory_file = root / "unscanned" / "directory" / "inside.md"
            outside_file = base / "outside.md"
            outside_directory_file = base / "outside-directory" / "outside.md"
            for path in (
                inside_file,
                inside_directory_file,
                outside_file,
                outside_directory_file,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\xffnot-utf8")
            (tasks / "inside-file.md").symlink_to(inside_file)
            (tasks / "outside-file.md").symlink_to(outside_file)
            (tasks / "inside-directory").symlink_to(inside_directory_file.parent)
            (tasks / "outside-directory").symlink_to(outside_directory_file.parent)

            entities = Store(root).list_entities()

            self.assertEqual(
                [entity.relative_path for entity in entities],
                ["tasks/ordinary.md"],
            )

    @unittest.skipUnless(
        sys.platform.startswith("linux")
        and hasattr(os, "O_NOFOLLOW")
        and pathlib.Path("/proc/self/fd").is_dir(),
        "descriptor path verification requires Linux /proc and O_NOFOLLOW",
    )
    def test_rejects_parent_symlink_swap_before_reading_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = pathlib.Path(temporary_directory)
            root = base / "repository"
            nested = root / "tasks" / "nested"
            candidate = nested / "entity.md"
            self.write(
                candidate,
                self.document(
                    "task-20260717-001", "task", "Inside\n", status="next"
                ),
            )
            outside_directory = base / "outside"
            self.write(
                outside_directory / candidate.name,
                self.document(
                    "task-20260717-999", "task", "Outside\n", status="next"
                ),
            )
            store = Store(root)
            original_nested = base / "original-nested"
            real_resolve = pathlib.Path.resolve
            swapped = False

            def resolve_then_swap(
                path: pathlib.Path, *args: object, **kwargs: object
            ) -> pathlib.Path:
                nonlocal swapped
                resolved = real_resolve(path, *args, **kwargs)
                if path == candidate and not swapped:
                    nested.rename(original_nested)
                    nested.symlink_to(outside_directory, target_is_directory=True)
                    swapped = True
                return resolved

            with (
                mock.patch.object(pathlib.Path, "resolve", resolve_then_swap),
                mock.patch("os.fdopen", wraps=os.fdopen) as fdopen,
            ):
                with self.assertRaises(InputError):
                    store.list_entities()

            self.assertTrue(swapped)
            fdopen.assert_not_called()

    def test_get_entity_handles_unknown_and_duplicate_ids_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            duplicate_id = "task-20260717-001"
            self.write(
                root / "tasks/one.md",
                self.document(duplicate_id, "task", "One\n", status="next"),
            )
            store = Store(root)

            self.assertEqual(store.get_entity(duplicate_id).body, "One\n")
            with self.assertRaises(NotFoundError):
                store.get_entity("task-20260717-999")

            self.write(
                root / "archive/two.md",
                self.document(duplicate_id, "task", "Two\n", status="done"),
            )
            with self.assertRaises(InputError):
                store.get_entity(duplicate_id)

    def test_next_entity_id_uses_active_and_archive_max_for_local_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            self.write(
                root / "tasks/one.md",
                self.document(
                    "task-20260717-001", "task", "One\n", status="next"
                ),
            )
            self.write(
                root / "archive/two.md",
                self.document(
                    "task-20260717-002", "task", "Two\n", status="done"
                ),
            )
            for index, entity_id in enumerate(
                (
                    "task-20260716-900",
                    "task-20260717-004x",
                    "task-20260717-12",
                    "project-20260717-950",
                )
            ):
                entity_type = "project" if entity_id.startswith("project") else "task"
                self.write(
                    root / "archive" / f"unrelated-{index}.md",
                    self.document(
                        entity_id,
                        entity_type,
                        "Unrelated\n",
                        status="done" if entity_type == "task" else "completed",
                    ),
                )

            next_id = Store(root).next_entity_id(
                "task", on_date=datetime.date(2026, 7, 17)
            )

            self.assertEqual(next_id, "task-20260717-003")

    def test_next_entity_id_supports_all_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))
            on_date = datetime.date(2026, 7, 17)

            self.assertEqual(
                store.next_entity_id("task", on_date=on_date),
                "task-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("purpose", on_date=on_date),
                "purpose-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("vision", on_date=on_date),
                "vision-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("area", on_date=on_date),
                "area-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("project", on_date=on_date),
                "project-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("goal", on_date=on_date),
                "goal-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("review", review_kind="daily", on_date=on_date),
                "review-daily-20260717-001",
            )
            self.assertEqual(
                store.next_entity_id("review", review_kind="weekly", on_date=on_date),
                "review-weekly-20260717-001",
            )

    def test_next_entity_id_ignores_non_ascii_digit_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            self.write(
                root / "tasks/ascii.md",
                self.document(
                    "task-20260717-001", "task", "ASCII\n", status="next"
                ),
            )
            for index, entity_id in enumerate(
                ("task-20260717-９９９", "task-20260717-١٢٣")
            ):
                self.write(
                    root / "archive" / f"unicode-{index}.md",
                    self.document(entity_id, "task", "Unicode\n", status="done"),
                )

            next_id = Store(root).next_entity_id(
                "task", on_date=datetime.date(2026, 7, 17)
            )

            self.assertEqual(next_id, "task-20260717-002")

    def test_next_entity_id_rejects_invalid_arguments_and_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            store = Store(root)
            on_date = datetime.date(2026, 7, 17)
            invalid_arguments = (
                ("review", None),
                ("review", "monthly"),
                ("task", "daily"),
                ("project", "weekly"),
                ("goal", "daily"),
            )
            for entity_type, review_kind in invalid_arguments:
                with self.subTest(
                    entity_type=entity_type, review_kind=review_kind
                ):
                    with self.assertRaises(InputError):
                        store.next_entity_id(
                            entity_type,
                            review_kind=review_kind,
                            on_date=on_date,
                        )

            self.write(
                root / "archive/exhausted.md",
                self.document(
                    "task-20260717-999", "task", "Exhausted\n", status="done"
                ),
            )
            with self.assertRaises(InputError):
                store.next_entity_id("task", on_date=on_date)

    def test_repository_cache_reuses_warm_entities_with_cold_equivalence(self) -> None:
        """Catches a warm read reparsing unchanged canonical documents."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            self.write(
                root / "tasks/one.md",
                self.document("task-20260717-001", "task", "One\n", status="next"),
            )
            self.write(
                root / "projects/one.md",
                self.document("project-20260717-001", "project", "Project\n"),
            )
            store = Store(root)
            cold = store.list_entities()
            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                warm = store.list_entities()

            self.assertEqual(warm, cold)
            self.assertEqual(warm, Store(root).list_entities())
            self.assertEqual(safe_read.call_count, 0)

    def test_repository_cache_refreshes_only_added_and_renamed_paths(self) -> None:
        """Catches stale add/delete/rename state or full warm rereads."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            first = root / "tasks/first.md"
            renamed = root / "tasks/renamed.md"
            second = root / "tasks/second.md"
            unchanged = root / "projects/unchanged.md"
            self.write(first, self.document("task-20260717-001", "task", "A\n", status="next"))
            self.write(second, self.document("task-20260717-002", "task", "B\n", status="next"))
            self.write(unchanged, self.document("project-20260717-001", "project", "P\n"))
            store = Store(root)
            store.list_entities()

            first.unlink()
            second.rename(renamed)
            self.write(
                root / "tasks/third.md",
                self.document("task-20260717-003", "task", "C\n", status="next"),
            )
            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                paths = [entity.relative_path for entity in store.list_entities()]

            self.assertEqual(
                paths,
                ["projects/unchanged.md", "tasks/renamed.md", "tasks/third.md"],
            )
            self.assertEqual(safe_read.call_count, 2)

    def test_repository_cache_detects_same_size_external_edit(self) -> None:
        """Catches a signature that ignores authoritative same-size edits."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            changed = root / "tasks/changed.md"
            self.write(changed, self.document("task-20260717-001", "task", "Alpha\n", status="next"))
            self.write(
                root / "projects/unchanged.md",
                self.document("project-20260717-001", "project", "Stable\n"),
            )
            store = Store(root)
            before = {entity.relative_path: entity for entity in store.list_entities()}
            original_stat = os.stat(changed, follow_symlinks=False)
            changed.write_bytes(changed.read_bytes().replace(b"Alpha", b"Bravo"))
            os.utime(
                changed,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                follow_symlinks=False,
            )

            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                after = {entity.relative_path: entity for entity in store.list_entities()}

            self.assertEqual(after["tasks/changed.md"].body, "Bravo\n")
            self.assertNotEqual(
                after["tasks/changed.md"].content_hash,
                before["tasks/changed.md"].content_hash,
            )
            self.assertEqual(safe_read.call_count, 1)

    def test_repository_cache_detects_external_inode_replacement(self) -> None:
        """Catches cache reuse across a path-preserving inode replacement."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            target = root / "tasks/target.md"
            self.write(target, self.document("task-20260717-001", "task", "Before\n", status="next"))
            self.write(
                root / "projects/unchanged.md",
                self.document("project-20260717-001", "project", "Stable\n"),
            )
            store = Store(root)
            store.list_entities()
            replacement = root / "tasks/replacement.md"
            self.write(replacement, self.document("task-20260717-001", "task", "After!\n", status="next"))
            os.replace(replacement, target)

            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                entities = {entity.relative_path: entity for entity in store.list_entities()}

            self.assertEqual(entities["tasks/target.md"].body, "After!\n")
            self.assertEqual(safe_read.call_count, 1)

    def test_repository_cache_never_returns_entity_replaced_by_symlink(self) -> None:
        """Catches a cached entity surviving a symlink replacement."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            target = root / "tasks/target.md"
            outside = pathlib.Path(temporary_directory) / "outside.md"
            self.write(target, self.document("task-20260717-001", "task", "Target\n", status="next"))
            self.write(
                root / "projects/unchanged.md",
                self.document("project-20260717-001", "project", "Stable\n"),
            )
            outside.write_bytes(target.read_bytes())
            store = Store(root)
            store.list_entities()
            target.unlink()
            target.symlink_to(outside)

            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                paths = [entity.relative_path for entity in store.list_entities()]

            self.assertEqual(paths, ["projects/unchanged.md"])
            self.assertEqual(safe_read.call_count, 0)

    def test_repository_cache_preserves_duplicate_id_failure_and_validator_errors(self) -> None:
        """Catches path cache collapsing duplicate IDs or bypassing validation."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            original = self.document("task-20260717-001", "task", "Original\n", status="next")
            self.write(root / "tasks/original.md", original)
            self.write(
                root / "projects/unchanged.md",
                self.document("project-20260717-001", "project", "Stable\n"),
            )
            store = Store(root)
            store.list_entities()
            self.write(root / "archive/duplicate.md", original.replace(b"status: next", b"status: done"))

            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                with self.assertRaisesRegex(InputError, "duplicate entity id"):
                    store.get_entity("task-20260717-001")
                cached_errors = store.repository_errors()

            self.assertEqual(cached_errors, sorted(validate_repository(root)))
            self.assertEqual(safe_read.call_count, 1)

    def test_repository_cache_fails_closed_when_root_inode_changes(self) -> None:
        """Catches cache reuse after the repository root is replaced."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = pathlib.Path(temporary_directory)
            root = base / "repository"
            self.write(root / "tasks/task.md", self.document("task-20260717-001", "task", "Old\n", status="next"))
            store = Store(root)
            store.list_entities()
            root.rename(base / "old-repository")
            self.write(root / "tasks/task.md", self.document("task-20260717-002", "task", "New\n", status="next"))

            with self.assertRaisesRegex(InputError, "root.*inode"):
                store.list_entities()

    def test_repository_cache_refreshes_entities_while_recovery_stays_fail_closed(self) -> None:
        """Catches recovery state bypass or stale reads behind the recovery gate."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            changed = root / "tasks/changed.md"
            self.write(changed, self.document("task-20260717-001", "task", "Alpha\n", status="next"))
            self.write(
                root / "projects/unchanged.md",
                self.document("project-20260717-001", "project", "Stable\n"),
            )
            store = Store(root)
            store.list_entities()
            changed.write_bytes(changed.read_bytes().replace(b"Alpha", b"Bravo"))
            (root / ".webapp-mutation-state").write_bytes(b"armed\n")

            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                wraps=store._read_regular_file_with_token,
            ) as safe_read:
                snapshot = store.read_snapshot()

            by_path = {entity.relative_path: entity for entity in snapshot.entities}
            self.assertEqual(by_path["tasks/changed.md"].body, "Bravo\n")
            self.assertTrue(snapshot.recovery_required)
            self.assertEqual(safe_read.call_count, 2)

    @staticmethod
    def write(path: pathlib.Path, content: bytes | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")

    @staticmethod
    def document(
        entity_id: str,
        entity_type: str,
        body: str,
        *,
        status: str = "active",
        review_kind: str | None = None,
    ) -> bytes:
        if entity_type == "review":
            fields = (
                f"id: {entity_id}\n"
                "type: review\n"
                "title: Review\n"
                f"review_kind: {review_kind}\n"
                "period_start: 2026-07-17\n"
                "created_at: 2026-07-17T10:00:00+09:00\n"
            )
        else:
            fields = (
                f"id: {entity_id}\n"
                f"type: {entity_type}\n"
                "title: Entity\n"
                f"status: {status}\n"
                "created_at: 2026-07-17T10:00:00+09:00\n"
                "updated_at: 2026-07-17T10:00:00+09:00\n"
            )
        return f"---\n{fields}---\n{body}".encode("utf-8")


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "O_NOFOLLOW")
    and pathlib.Path("/proc/self/fd").is_dir(),
    "secure mutations require Linux /proc and O_NOFOLLOW",
)
class StoreProjectKanbanOrderTest(unittest.TestCase):
    FIXED_NOW = datetime.datetime(
        2026, 8, 29, 2, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
    )

    def make_store(self, base: pathlib.Path) -> Store:
        root = base / "repository"
        for directory in (
            "inbox", "tasks", "purposes", "visions", "areas", "projects",
            "goals", "roadmap-outcomes", "cycles", "reviews/daily",
            "reviews/weekly", "archive",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        return Store(root)

    def write_project(
        self,
        store: Store,
        entity_id: str,
        status: str,
        position: int | None,
    ):
        frontmatter = {
            "id": entity_id,
            "type": "project",
            "title": entity_id,
            "status": status,
            "created_at": self.FIXED_NOW.isoformat(timespec="seconds"),
            "updated_at": self.FIXED_NOW.isoformat(timespec="seconds"),
        }
        path = store._root / "projects" / f"{entity_id}.md"
        document = serialize_frontmatter(frontmatter, "project")
        if position is not None:
            document = document.replace("\n---\n", f"\nkanban_position: {position}\n---\n", 1)
        path.write_text(
            document + "Body\n",
            encoding="utf-8",
        )
        return store.get_entity(entity_id)

    def apply_plan(self, store: Store, plan: MutationPlan | WorkflowPlan):
        if isinstance(plan, WorkflowPlan):
            return store.apply_task_workflow(plan)
        return (store.apply_mutation_plan(plan),)

    def lane(self, store: Store, status: str) -> list[tuple[str, str | None]]:
        entities = [
            entity
            for entity in store.list_entities()
            if entity.entity_type == "project"
            and not entity.relative_path.startswith("archive/")
            and ("not_started" if entity.frontmatter.get("status") == "active" else entity.frontmatter.get("status")) == status
        ]
        entities.sort(key=lambda entity: int(entity.frontmatter.get("kanban_position", "999999")))
        return [
            (entity.entity_id, entity.frontmatter.get("kanban_position"))
            for entity in entities
        ]

    def test_project_move_reorders_same_lane_and_cross_lane_atomically(self) -> None:
        """Catches a move updating only the target and leaving lane positions ambiguous."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            first = self.write_project(store, "project-first", "not_started", 1)
            second = self.write_project(store, "project-second", "not_started", 2)
            doing = self.write_project(store, "project-doing", "doing", 1)

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                same = store.plan_project_move(
                    second.entity_id, second.content_hash, "not_started", 1
                )
            self.assertEqual(
                [effect.role for effect in same.effects],
                ["project_moved", "project_reordered"],
            )
            store.apply_task_workflow(same)
            self.assertEqual(
                self.lane(store, "not_started"),
                [("project-second", "1"), ("project-first", "2")],
            )

            current_second = store.get_entity(second.entity_id)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                cross = store.plan_project_move(
                    current_second.entity_id, current_second.content_hash, "doing", 2
                )
            store.apply_task_workflow(cross)
            self.assertEqual(self.lane(store, "not_started"), [("project-first", "1")])
            self.assertEqual(
                self.lane(store, "doing"),
                [(doing.entity_id, "1"), (second.entity_id, "2")],
            )
            self.assertEqual(validate_repository(store._root), [])

    def test_project_move_normalizes_legacy_order_and_rejects_invalid_requests(self) -> None:
        """Catches path fallback loss and invalid status/position/hash bypasses."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            first = self.write_project(store, "project-a", "not_started", None)
            second = self.write_project(store, "project-b", "not_started", None)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                plan = store.plan_project_move(
                    second.entity_id, second.content_hash, "not_started", 1
                )
            store.apply_task_workflow(plan)
            self.assertEqual(
                self.lane(store, "not_started"),
                [(second.entity_id, "1"), (first.entity_id, "2")],
            )

            current = store.get_entity(first.entity_id)
            for status, position in (("active", 1), ("blocked", 1), ("doing", 0), ("doing", 3)):
                with self.subTest(status=status, position=position), self.assertRaises(InputError):
                    store.plan_project_move(
                        current.entity_id, current.content_hash, status, position
                    )
            with self.assertRaises(ConflictError):
                store.plan_project_move(current.entity_id, "0" * 64, "doing", 1)

    def test_project_create_normalizes_legacy_lane_and_appends_atomically(self) -> None:
        """Catches create leaving a mixed positioned/legacy lane ambiguous."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            first = self.write_project(store, "project-a", "not_started", None)
            second = self.write_project(store, "project-b", "not_started", None)

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                plan = store.plan_create_entity(
                    "project", {"title": "New project"}, "Body"
                )

            self.assertIsInstance(plan, WorkflowPlan)
            results = store.apply_task_workflow(plan)
            self.assertEqual(results[0].frontmatter["title"], "New project")
            self.assertEqual(
                self.lane(store, "not_started"),
                [
                    (first.entity_id, "1"),
                    (second.entity_id, "2"),
                    (results[0].entity_id, "3"),
                ],
            )
            self.assertEqual(validate_repository(store._root), [])

    def test_project_create_status_change_task_start_and_archive_keep_lane_order(self) -> None:
        """Catches non-drag lifecycle paths bypassing the shared lane contract."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            first = self.write_project(store, "project-first", "not_started", 1)
            target = self.write_project(store, "project-target", "not_started", 2)
            doing = self.write_project(store, "project-doing", "doing", 1)

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                create = store.plan_create_entity("project", {"title": "New"}, "Body")
            created = store.apply_mutation_plan(create)
            self.assertEqual(created.frontmatter.get("kanban_position"), "3")

            current_target = store.get_entity(target.entity_id)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                update = store.plan_update_entity(
                    current_target.entity_id,
                    current_target.content_hash,
                    {"status": "doing"},
                    None,
                )
            self.apply_plan(store, update)
            self.assertEqual(
                self.lane(store, "doing"),
                [(doing.entity_id, "1"), (target.entity_id, "2")],
            )

            linked = self.write_project(store, "project-linked", "not_started", 3)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                task_plan = store.plan_create_entity(
                    "task",
                    {"title": "Linked task", "status": "next", "project_id": linked.entity_id},
                    "Body",
                )
            task = store.apply_mutation_plan(task_plan)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                start = store.plan_task_start(task.entity_id, task.content_hash)
            store.apply_task_workflow(start)
            self.assertEqual(store.get_entity(linked.entity_id).frontmatter["kanban_position"], "3")

            current_first = store.get_entity(first.entity_id)
            archive = store.plan_archive_entity(first.entity_id, current_first.content_hash)
            self.apply_plan(store, archive)
            remaining = self.lane(store, "not_started")
            self.assertEqual([position for _, position in remaining], ["1"])
            archived = store.get_entity(first.entity_id)
            self.assertTrue(archived.relative_path.startswith("archive/"))
            self.assertEqual(archived.frontmatter["kanban_position"], "1")
            self.assertEqual(validate_repository(store._root), [])

    def test_task_start_moves_non_tail_project_to_doing_tail_and_compacts_source(self) -> None:
        """Catches automatic Project start retaining its old lane position."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            target = self.write_project(store, "project-target", "not_started", 1)
            sibling = self.write_project(store, "project-sibling", "not_started", 2)
            doing = self.write_project(store, "project-doing", "doing", 1)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                task_plan = store.plan_create_entity(
                    "task",
                    {"title": "Start", "status": "next", "project_id": target.entity_id},
                    "Body",
                )
            task = store.apply_mutation_plan(task_plan)

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                start = store.plan_task_start(task.entity_id, task.content_hash)

            self.assertEqual(
                [effect.role for effect in start.effects],
                ["project_started", "project_reordered", "started"],
            )
            store.apply_task_workflow(start)
            self.assertEqual(self.lane(store, "not_started"), [(sibling.entity_id, "1")])
            self.assertEqual(
                self.lane(store, "doing"),
                [(doing.entity_id, "1"), (target.entity_id, "2")],
            )

    def test_project_move_rejects_terminal_transition_with_unfinished_task(self) -> None:
        """Catches reorder bypassing the established Project lifecycle guard."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            project = self.write_project(store, "project-target", "doing", 1)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                task_plan = store.plan_create_entity(
                    "task",
                    {"title": "Unfinished", "status": "next", "project_id": project.entity_id},
                    "Body",
                )
            store.apply_mutation_plan(task_plan)

            with self.assertRaisesRegex(ProjectOperationError, "unfinished_project_tasks"):
                store.plan_project_move(
                    project.entity_id, project.content_hash, "completed", 1
                )

    def test_project_move_publication_failure_restores_every_lane_member(self) -> None:
        """Catches a partial Project reorder surviving a multi-effect publication failure."""
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(pathlib.Path(directory))
            first = self.write_project(store, "project-first", "not_started", 1)
            second = self.write_project(store, "project-second", "not_started", 2)
            before = {
                entity.entity_id: entity.content_hash
                for entity in store.list_entities()
                if entity.entity_type == "project"
            }
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                plan = store.plan_project_move(
                    second.entity_id, second.content_hash, "not_started", 1
                )
            real_publish = store._publish_no_replace
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected Project reorder publication failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(
                store, "_publish_no_replace", side_effect=fail_second
            ), self.assertRaises(OSError):
                store.apply_task_workflow(plan)

            self.assertEqual(
                {
                    entity.entity_id: entity.content_hash
                    for entity in store.list_entities()
                    if entity.entity_type == "project"
                },
                before,
            )
            self.assertEqual(
                self.lane(store, "not_started"),
                [(first.entity_id, "1"), (second.entity_id, "2")],
            )
            self.assertFalse(store.mutation_recovery_required())


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "O_NOFOLLOW")
    and pathlib.Path("/proc/self/fd").is_dir(),
    "secure mutations require Linux /proc and O_NOFOLLOW",
)
class StoreEntityMutationTest(unittest.TestCase):
    FIXED_NOW = datetime.datetime(
        2026, 7, 17, 12, 34, 56, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
    )

    def make_repository(self, base: pathlib.Path) -> pathlib.Path:
        root = base / "repository"
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
            (root / directory).mkdir(parents=True, exist_ok=True)
        return root

    def test_direction_entities_support_canonical_create_update_and_archive(self) -> None:
        cases = (
            ("purpose", {"title": "Purpose"}, {"title": "Updated purpose"}),
            (
                "vision",
                {"title": "Vision", "status": "active"},
                {"status": "on_hold"},
            ),
            (
                "area",
                {"title": "Health", "health": "maintained"},
                {
                    "health": "needs_attention",
                    "last_reviewed_on": "2026-07-17",
                },
            ),
        )
        for entity_type, create_fields, update_fields in cases:
            with self.subTest(entity_type=entity_type), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                    create_plan = store.plan_create_entity(
                        entity_type,
                        create_fields,
                        PURPOSE_BODY if entity_type == "purpose" else f"{entity_type} body",
                    )

                self.assertEqual(
                    create_plan.destination_relative_path,
                    f"{entity_type}s/{entity_type}-20260717-001.md",
                )
                created = store.apply_mutation_plan(create_plan)
                self.assertEqual(
                    created.frontmatter.get("status"), create_fields.get("status")
                )

                later = self.FIXED_NOW + datetime.timedelta(hours=1)
                with mock.patch("webapp.store.current_time", return_value=later):
                    update_plan = store.plan_update_entity(
                        created.entity_id,
                        created.content_hash,
                        update_fields,
                        None,
                    )
                updated = store.apply_mutation_plan(update_plan)
                self.assertEqual(
                    updated.frontmatter["updated_at"],
                    "2026-07-17T13:34:56+09:00",
                )

                archive_plan = store.plan_archive_entity(
                    updated.entity_id, updated.content_hash
                )
                archived = store.apply_mutation_plan(archive_plan)
                self.assertTrue(archived.relative_path.startswith("archive/"))
                self.assertEqual(validate_repository(root), [])

    def test_direction_links_require_active_parents_of_the_declared_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            vision = self.create(store, "vision", {"title": "Vision"})
            area = self.create(store, "area", {"title": "Area"})
            goal = self.create(
                store,
                "goal",
                {"title": "Goal", "vision_id": vision.entity_id},
            )
            project = self.create(
                store,
                "project",
                {
                    "title": "Project",
                    "goal_id": goal.entity_id,
                    "area_id": area.entity_id,
                },
            )
            standalone = self.create(
                store,
                "task",
                {"title": "Standalone", "area_id": area.entity_id},
            )
            linked = self.create(
                store,
                "task",
                {"title": "Linked", "project_id": project.entity_id},
            )
            self.assertEqual(standalone.frontmatter["area_id"], area.entity_id)
            self.assertEqual(linked.frontmatter["project_id"], project.entity_id)
            self.assertEqual(validate_repository(root), [])

            invalid_fields = (
                ("task", {"title": "Both", "project_id": project.entity_id, "area_id": area.entity_id}),
                ("task", {"title": "Wrong area", "area_id": goal.entity_id}),
                ("project", {"title": "Wrong area", "area_id": goal.entity_id}),
                ("goal", {"title": "Wrong vision", "vision_id": area.entity_id}),
                ("goal", {"title": "Missing vision", "vision_id": "vision-missing"}),
            )
            for entity_type, fields in invalid_fields:
                with self.subTest(entity_type=entity_type, fields=fields):
                    with self.assertRaises((InputError, SchemaError)):
                        store.plan_create_entity(entity_type, fields, "Body")

    def test_link_target_is_rechecked_when_applying_a_stale_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            area = self.create(store, "area", {"title": "Area"})
            child_plan = store.plan_create_entity(
                "task", {"title": "Later", "area_id": area.entity_id}, "Body"
            )
            store.apply_mutation_plan(
                store.plan_archive_entity(area.entity_id, area.content_hash)
            )

            with self.assertRaises(InputError):
                store.plan_create_entity(
                    "task", {"title": "Too late", "area_id": area.entity_id}, "Body"
                )
            with self.assertRaises(InputError):
                store.apply_mutation_plan(child_plan)
            self.assertFalse(root.joinpath(child_plan.destination_relative_path).exists())

    def test_purpose_singleton_is_enforced_at_preview_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            first = self.create(
                store, "purpose", {"title": "First"}, PURPOSE_BODY
            )
            with self.assertRaises(InputError):
                store.plan_create_entity(
                    "purpose", {"title": "Second"}, PURPOSE_BODY
                )

            archived = store.apply_mutation_plan(
                store.plan_archive_entity(first.entity_id, first.content_hash)
            )
            self.assertTrue(archived.relative_path.startswith("archive/"))

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                earlier = store.plan_create_entity(
                    "purpose", {"title": "Earlier preview"}, PURPOSE_BODY
                )
            next_day = self.FIXED_NOW + datetime.timedelta(days=1)
            with mock.patch("webapp.store.current_time", return_value=next_day):
                later = store.plan_create_entity(
                    "purpose", {"title": "Later preview"}, PURPOSE_BODY
                )
            store.apply_mutation_plan(earlier)
            with self.assertRaises(InputError):
                store.apply_mutation_plan(later)
            self.assertFalse(root.joinpath(later.destination_relative_path).exists())

    def test_purpose_create_and_update_require_both_exact_heading_lines(self) -> None:
        invalid_bodies = (
            "## Principles\n",
            "## Purpose\n",
            "## Purpose \n\n## Principles\n",
        )
        for body in invalid_bodies:
            with self.subTest(operation="create", body=body), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                with self.assertRaises(InputError):
                    store.plan_create_entity(
                        "purpose", {"title": "Invalid"}, body
                    )

            with self.subTest(operation="update", body=body), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create(
                    store, "purpose", {"title": "Current"}, PURPOSE_BODY
                )
                with self.assertRaises(InputError):
                    store.plan_update_entity(
                        current.entity_id,
                        current.content_hash,
                        {},
                        body,
                    )

        allowed_bodies = (
            PURPOSE_BODY,
            "## Principles\n\n## Purpose\n",
            "## Purpose\n\n## Purpose\n\n## Principles\n\n## Principles\n",
        )
        for body in allowed_bodies:
            with self.subTest(allowed=body), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                plan = Store(root).plan_create_entity(
                    "purpose", {"title": "Allowed"}, body
                )
                self.assertEqual(plan.planned_entity.body, "\n" + body)

    def test_purpose_body_contract_is_rechecked_when_applying_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            valid = store.plan_create_entity(
                "purpose", {"title": "Purpose"}, PURPOSE_BODY
            )
            invalid_body = "## Purpose\n"
            invalid_after = store._canonical_document(
                dict(valid.planned_entity.frontmatter), "purpose", invalid_body
            )
            invalid_inputs = tuple(
                (key, invalid_body if key == "body" else value)
                for key, value in valid.operation_inputs
            )
            invalid_plan = dataclasses.replace(
                valid,
                after_bytes=invalid_after,
                planned_entity=store._entity_from_bytes(
                    valid.destination_relative_path, invalid_after
                ),
                operation_inputs=invalid_inputs,
                integrity_tag=store._mutation_plan_integrity_tag(
                    action=valid.action,
                    entity_id=valid.entity_id,
                    entity_type=valid.entity_type,
                    source_relative_path=valid.source_relative_path,
                    destination_relative_path=valid.destination_relative_path,
                    base_hash=valid.base_hash,
                    before_bytes=valid.before_bytes,
                    after_bytes=invalid_after,
                    operation_inputs=invalid_inputs,
                    validation_errors=valid.validation_errors,
                ),
            )

            with self.assertRaises(InputError):
                store.apply_mutation_plan(invalid_plan)
            self.assertFalse(root.joinpath(valid.destination_relative_path).exists())

    def test_archive_refuses_active_children_without_cascade(self) -> None:
        cases = (
            ("vision", "goal", "vision_id"),
            ("area", "project", "area_id"),
            ("goal", "project", "goal_id"),
            ("project", "task", "project_id"),
        )
        for parent_type, child_type, link_key in cases:
            with self.subTest(parent_type=parent_type), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                parent = self.create(store, parent_type, {"title": "Parent"})
                child = self.create(
                    store,
                    child_type,
                    {"title": "Child", link_key: parent.entity_id},
                )
                before = {
                    entity.entity_id: entity.content_hash
                    for entity in store.list_entities()
                }

                with self.assertRaises(InputError):
                    store.plan_archive_entity(parent.entity_id, parent.content_hash)

                self.assertEqual(
                    {entity.entity_id: entity.content_hash for entity in store.list_entities()},
                    before,
                )
                self.assertFalse(child.relative_path.startswith("archive/"))

    def test_progress_is_private_and_does_not_block_archiving_its_origin(self) -> None:
        """Catches historical Progress links being treated as active children."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create(store, "project", {"title": "Project"})
            progress = self.create(
                store,
                "progress",
                {
                    "title": "説明が具体化した",
                    "occurred_on": "2026-07-17",
                    "project_id": project.entity_id,
                },
                "## メリット・学び\n\n学び\n",
            )

            archived = store.apply_mutation_plan(
                store.plan_archive_entity(project.entity_id, project.content_hash)
            )

            self.assertEqual(progress.frontmatter["visibility"], "private")
            self.assertEqual(archived.relative_path.split("/", 1)[0], "archive")
            self.assertEqual(store.get_entity(progress.entity_id).frontmatter["project_id"], project.entity_id)

    def test_progress_rejects_multiple_or_wrong_type_origins(self) -> None:
        """Catches Progress records with ambiguous or type-confused attribution."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            goal = self.create(store, "goal", {"title": "Goal"})
            common = {"title": "変化", "occurred_on": "2026-07-17"}

            with self.assertRaises(InputError):
                store.plan_create_entity(
                    "progress", {**common, "project_id": goal.entity_id}, "Body"
                )
            with self.assertRaises(InputError):
                store.plan_create_entity(
                    "progress",
                    {**common, "project_id": goal.entity_id, "goal_id": goal.entity_id},
                    "Body",
                )

    def test_archive_child_blocker_is_rechecked_when_applying_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create(store, "project", {"title": "Project"})
            archive_plan = store.plan_archive_entity(
                project.entity_id, project.content_hash
            )
            task = self.create(
                store,
                "task",
                {"title": "New child", "project_id": project.entity_id},
            )

            with self.assertRaises(InputError):
                store.apply_mutation_plan(archive_plan)

            self.assertEqual(
                store.get_entity(project.entity_id).relative_path,
                project.relative_path,
            )
            self.assertEqual(store.get_entity(task.entity_id).content_hash, task.content_hash)

    def create(
        self,
        store: Store,
        entity_type: str,
        fields: dict[str, str],
        body: str = "Body",
        review_kind: str | None = None,
    ):
        with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
            return store.create_entity(entity_type, fields, body, review_kind)

    def test_capture_creates_canonical_validator_clean_inbox_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))

            entity = self.create(
                Store(root),
                "task",
                {"title": "Capture this"},
                "Line one\n\nLine two",
            )

            self.assertEqual(entity.entity_id, "task-20260717-001")
            self.assertEqual(entity.relative_path, "inbox/task-20260717-001.md")
            self.assertEqual(
                entity.frontmatter,
                {
                    "id": "task-20260717-001",
                    "type": "task",
                    "title": "Capture this",
                    "status": "inbox",
                    "created_at": "2026-07-17T12:34:56+09:00",
                    "updated_at": "2026-07-17T12:34:56+09:00",
                },
            )
            self.assertEqual(entity.body, "\nLine one\n\nLine two\n")
            self.assertEqual(validate_repository(root), [])
            self.assertEqual(
                (root / entity.relative_path).read_bytes(),
                (
                    b"---\n"
                    b"id: task-20260717-001\n"
                    b"type: task\n"
                    b'title: "Capture this"\n'
                    b"status: inbox\n"
                    b"created_at: 2026-07-17T12:34:56+09:00\n"
                    b"updated_at: 2026-07-17T12:34:56+09:00\n"
                    b"---\n\nLine one\n\nLine two\n"
                ),
            )

    def test_task_action_date_update_validates_date_and_preserves_due(self) -> None:
        """Catches generic Task updates accepting timestamps or overwriting deadlines."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            task = self.create(
                store,
                "task",
                {"title": "Plan it", "status": "next", "due": "2026-09-01"},
            )

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                try:
                    plan = store.plan_update_entity(
                        task.entity_id,
                        task.content_hash,
                        {"action_date": "2026-08-29"},
                        None,
                    )
                except InputError as error:
                    self.fail(f"valid action_date update was rejected: {error}")
            self.assertIsInstance(plan, MutationPlan)
            updated = store.apply_mutation_plan(plan)
            self.assertEqual(updated.frontmatter["action_date"], "2026-08-29")
            self.assertEqual(updated.frontmatter["due"], "2026-09-01")

            with self.assertRaises(InputError):
                store.plan_update_entity(
                    updated.entity_id,
                    updated.content_hash,
                    {"action_date": "2026-08-29T09:00:00+09:00"},
                    None,
                )

            clear = store.plan_update_entity(
                updated.entity_id,
                updated.content_hash,
                {"action_date": ""},
                None,
            )
            self.assertIsInstance(clear, MutationPlan)
            cleared = store.apply_mutation_plan(clear)
            self.assertNotIn("action_date", cleared.frontmatter)
            self.assertEqual(cleared.frontmatter["due"], "2026-09-01")

    def test_transition_to_inbox_or_planned_atomically_removes_action_date(self) -> None:
        """Catches a status transition leaving a Focus-eligible date behind."""
        for status in ("inbox", "planned"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = self.make_repository(pathlib.Path(directory))
                store = Store(root)
                project = self.create(store, "project", {"title": "Project"})
                try:
                    task = self.create(
                        store,
                        "task",
                        {
                            "title": "Plan it",
                            "status": "next",
                            "project_id": project.entity_id,
                            "action_date": "2026-08-29",
                        },
                    )
                except InputError as error:
                    self.fail(f"valid action_date create was rejected: {error}")

                plan = store.plan_update_entity(
                    task.entity_id,
                    task.content_hash,
                    {"status": status},
                    None,
                )

                self.assertIsInstance(plan, WorkflowPlan)
                self.assertEqual(len(plan.effects), 1)
                self.assertNotIn(
                    "action_date", plan.effects[0].planned_entity.frontmatter
                )
                updated = store.apply_task_workflow(plan)
                self.assertEqual(updated[0].frontmatter["status"], status)
                self.assertNotIn("action_date", updated[0].frontmatter)

    def test_inbox_and_planned_task_updates_reject_action_date(self) -> None:
        """Catches direct date assignment bypassing executable-work admission."""
        for status in ("inbox", "planned"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = self.make_repository(pathlib.Path(directory))
                store = Store(root)
                fields = {"title": "Not executable", "status": status}
                if status == "planned":
                    project = self.create(store, "project", {"title": "Project"})
                    fields["project_id"] = project.entity_id
                task = self.create(store, "task", fields)

                with self.assertRaisesRegex(
                    InputError, f"{status} Task forbids action_date"
                ):
                    store.plan_update_entity(
                        task.entity_id,
                        task.content_hash,
                        {"action_date": "2026-08-29"},
                        None,
                    )

    def test_calendar_import_create_binds_exact_requested_task_id(self) -> None:
        requested_id = "task-calendar-0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                plan = store.plan_create_entity(
                    "task", {"title": "Calendar import"}, "Body",
                    requested_id=requested_id,
                )

            self.assertEqual(plan.entity_id, requested_id)
            self.assertEqual(
                plan.destination_relative_path, f"inbox/{requested_id}.md"
            )
            self.assertIn(("requested_id", requested_id), plan.operation_inputs)
            self.assertEqual(plan.planned_entity.frontmatter["id"], requested_id)
            self.assertEqual(
                plan.planned_entity.frontmatter["created_at"],
                "2026-07-17T12:34:56+09:00",
            )

            created = store.apply_mutation_plan(plan)

            self.assertEqual(created.entity_id, requested_id)
            self.assertEqual(store.get_entity(requested_id), created)
            self.assertEqual(validate_repository(root), [])

    def test_requested_create_id_is_restricted_and_collision_fails_closed(self) -> None:
        valid = "task-calendar-abcdef0123456789abcdef01"
        invalid = (
            ("project", valid),
            ("goal", valid),
            ("review", valid),
            ("task", "task-20260717-001"),
            ("task", "task-calendar-ABCDEF0123456789abcdef01"),
            ("task", "task-calendar-abcdef0123456789abcdef0"),
            ("task", "task-calendar-abcdef0123456789abcdef012"),
            ("task", "../task-calendar-abcdef0123456789abcdef01"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            for entity_type, requested_id in invalid:
                with self.subTest(entity_type=entity_type, requested_id=requested_id):
                    with self.assertRaises(InputError):
                        store.plan_create_entity(
                            entity_type,
                            ({"period_start": "2026-07-17"}
                             if entity_type == "review" else {"title": "Invalid"}),
                            "Body",
                            "daily" if entity_type == "review" else None,
                            requested_id=requested_id,
                        )

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                store.create_entity(
                    "task", {"title": "First"}, "Body", requested_id=valid
                )
            with self.assertRaises((DestinationConflict, InputError)):
                store.plan_create_entity(
                    "task", {"title": "Collision"}, "Body", requested_id=valid
                )

    def test_requested_id_is_integrity_bound_and_normal_create_stays_auto_numbered(self) -> None:
        requested_id = "task-calendar-111111111111111111111111"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                ordinary = store.plan_create_entity(
                    "task", {"title": "Ordinary"}, "Body"
                )
                exact = store.plan_create_entity(
                    "task", {"title": "Exact"}, "Body",
                    requested_id=requested_id,
                )
            self.assertEqual(ordinary.entity_id, "task-20260717-001")
            self.assertIn(("requested_id", ""), ordinary.operation_inputs)
            forged_inputs = tuple(
                (key, "task-calendar-222222222222222222222222")
                if key == "requested_id" else (key, value)
                for key, value in exact.operation_inputs
            )
            with self.assertRaises(InputError):
                store.apply_mutation_plan(
                    dataclasses.replace(exact, operation_inputs=forged_inputs)
                )
            self.assertFalse((root / exact.destination_relative_path).exists())

    def test_create_plan_has_no_entity_effect_and_applies_exact_planned_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            with (
                mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW),
                mock.patch(
                    "webapp.store.tempfile.TemporaryDirectory",
                    side_effect=AssertionError("planning must not stage a document"),
                ),
            ):
                plan = store.plan_create_entity(
                    "task", {"title": "Preview"}, "Exact body"
                )

            self.assertIsInstance(plan, MutationPlan)
            self.assertEqual(plan.action, "create")
            self.assertIsNone(plan.source_relative_path)
            self.assertIsNone(plan.before_bytes)
            self.assertIsNone(plan.before_entity)
            self.assertIsNone(plan.base_hash)
            self.assertEqual(plan.validation_errors, ())
            self.assertEqual(list(root.rglob("*.md")), [])
            self.assertEqual(list(root.rglob("*.tmp")), [])
            self.assertEqual(list(root.rglob("*.quarantine")), [])

            later = self.FIXED_NOW + datetime.timedelta(days=1)
            with mock.patch("webapp.store.current_time", return_value=later):
                created = store.apply_mutation_plan(plan)

            self.assertEqual(created.entity_id, "task-20260717-001")
            self.assertEqual(
                (root / created.relative_path).read_bytes(), plan.after_bytes
            )
            self.assertEqual(created, plan.planned_entity)

    def test_update_and_archive_plans_capture_exact_state_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store, "task", {"title": "Plan", "status": "next"}, "Original"
            )
            source = root / original.relative_path
            exact = source.read_bytes()
            later = self.FIXED_NOW + datetime.timedelta(hours=2)
            with mock.patch("webapp.store.current_time", return_value=later):
                update_plan = store.plan_update_entity(
                    original.entity_id,
                    original.content_hash,
                    {"title": "Changed", "status": "inbox"},
                    "Replacement",
                )

            self.assertEqual(source.read_bytes(), exact)
            self.assertEqual(update_plan.before_bytes, exact)
            self.assertEqual(update_plan.before_entity, original)
            self.assertEqual(update_plan.source_relative_path, original.relative_path)
            self.assertEqual(
                update_plan.destination_relative_path,
                f"inbox/{original.entity_id}.md",
            )
            self.assertEqual(
                update_plan.planned_entity.frontmatter["updated_at"],
                "2026-07-17T14:34:56+09:00",
            )

            archive_plan = store.plan_archive_entity(
                original.entity_id, original.content_hash
            )
            self.assertEqual(archive_plan.after_bytes, exact)
            self.assertEqual(archive_plan.before_bytes, exact)
            self.assertEqual(source.read_bytes(), exact)

            much_later = later + datetime.timedelta(days=3)
            with mock.patch("webapp.store.current_time", return_value=much_later):
                updated = store.apply_mutation_plan(update_plan)
            self.assertEqual(
                (root / updated.relative_path).read_bytes(), update_plan.after_bytes
            )
            self.assertEqual(updated, update_plan.planned_entity)

    def test_plan_apply_conflicts_preserve_concurrent_winners_and_external_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            create_plan = store.plan_create_entity(
                "task", {"title": "Planned"}, "Body"
            )
            destination = root / create_plan.destination_relative_path
            winner = b"external winner\n"
            destination.write_bytes(winner)
            with self.assertRaises(DestinationConflict):
                store.apply_mutation_plan(create_plan)
            self.assertEqual(destination.read_bytes(), winner)

            destination.unlink()
            original = self.create(
                store, "task", {"title": "Original", "status": "next"}
            )
            plan = store.plan_update_entity(
                original.entity_id,
                original.content_hash,
                {"title": "Planned update"},
                None,
            )
            source = root / original.relative_path
            external = source.read_bytes().replace(b"Original", b"External")
            source.write_bytes(external)
            with self.assertRaises(ConflictError):
                store.apply_mutation_plan(plan)
            self.assertEqual(source.read_bytes(), external)

    def test_planned_move_and_archive_destination_collisions_require_new_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store, "task", {"title": "Move", "status": "next"}
            )
            move_plan = store.plan_update_entity(
                original.entity_id,
                original.content_hash,
                {"status": "inbox"},
                None,
            )
            move_destination = root / move_plan.destination_relative_path
            move_destination.write_bytes(b"move winner\n")
            with self.assertRaises(DestinationConflict):
                store.apply_mutation_plan(move_plan)
            self.assertTrue((root / original.relative_path).exists())
            self.assertEqual(move_destination.read_bytes(), b"move winner\n")

            move_destination.unlink()
            archive_plan = store.plan_archive_entity(
                original.entity_id, original.content_hash
            )
            archive_destination = root / archive_plan.destination_relative_path
            archive_destination.write_bytes(b"archive winner\n")
            with self.assertRaises(DestinationConflict):
                store.apply_mutation_plan(archive_plan)
            self.assertTrue((root / original.relative_path).exists())
            self.assertEqual(archive_destination.read_bytes(), b"archive winner\n")

    def test_tampered_plans_are_rejected_before_effect_and_double_apply_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            plan = store.plan_create_entity(
                "task", {"title": "Untampered"}, "Body"
            )
            destination = root / plan.destination_relative_path
            variants = (
                dataclasses.replace(plan, after_bytes=plan.after_bytes + b"x"),
                dataclasses.replace(
                    plan,
                    planned_entity=dataclasses.replace(
                        plan.planned_entity, body="\nTampered\n"
                    ),
                ),
                dataclasses.replace(plan, action="archive"),
                dataclasses.replace(plan, entity_id="task-20260717-999"),
                dataclasses.replace(plan, source_relative_path="../escape.md"),
                dataclasses.replace(plan, destination_relative_path="../escape.md"),
                dataclasses.replace(plan, base_hash="0" * 64),
                dataclasses.replace(
                    plan,
                    operation_inputs=plan.operation_inputs + (("field:title", "Changed"),),
                ),
            )
            for variant in variants:
                with self.subTest(variant=variant):
                    with self.assertRaises(InputError):
                        store.apply_mutation_plan(variant)
                    self.assertFalse(destination.exists())

            created = store.apply_mutation_plan(plan)
            exact = (root / created.relative_path).read_bytes()
            with self.assertRaises(DestinationConflict):
                store.apply_mutation_plan(plan)
            self.assertEqual((root / created.relative_path).read_bytes(), exact)

    def test_source_move_or_replacement_after_preview_is_preserved_as_conflict(self) -> None:
        for change in ("move", "replace"):
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    original = self.create(
                        store, "task", {"title": "Source", "status": "next"}
                    )
                    plan = store.plan_archive_entity(
                        original.entity_id, original.content_hash
                    )
                    source = root / original.relative_path
                    if change == "move":
                        changed_path = root / "inbox" / source.name
                        source.rename(changed_path)
                        expected = changed_path.read_bytes()
                    else:
                        changed_path = source
                        expected = b"external invalid replacement\n"
                        changed_path.write_bytes(expected)

                    with self.assertRaises((ConflictError, MutationPlanConflict)):
                        store.apply_mutation_plan(plan)

                    self.assertEqual(changed_path.read_bytes(), expected)
                    self.assertFalse(
                        (root / plan.destination_relative_path).exists()
                    )

    def test_archive_plan_fixes_existing_collision_suffix_and_never_reselects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store, "task", {"title": "Archive suffix", "status": "next"}
            )
            base_destination = root / "archive" / pathlib.Path(
                original.relative_path
            ).name
            base_destination.write_bytes(b"base occupant\n")
            plan = store.plan_archive_entity(
                original.entity_id, original.content_hash
            )
            self.assertTrue(plan.destination_relative_path.endswith("-2.md"))
            planned_destination = root / plan.destination_relative_path
            planned_destination.write_bytes(b"suffix occupant\n")

            with self.assertRaises(DestinationConflict):
                store.apply_mutation_plan(plan)

            self.assertEqual(base_destination.read_bytes(), b"base occupant\n")
            self.assertEqual(planned_destination.read_bytes(), b"suffix occupant\n")
            self.assertTrue((root / original.relative_path).exists())
            self.assertFalse(
                planned_destination.with_name(
                    planned_destination.name.replace("-2.md", "-3.md")
                ).exists()
            )

    def test_archive_plan_apply_preserves_exact_bytes_and_double_apply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store, "task", {"title": "Exact archive", "status": "next"}, "Body"
            )
            source = root / original.relative_path
            exact = source.read_bytes()
            plan = store.plan_archive_entity(
                original.entity_id, original.content_hash
            )

            archived = store.apply_mutation_plan(plan)

            destination = root / archived.relative_path
            self.assertEqual(destination.read_bytes(), exact)
            self.assertEqual(plan.after_bytes, exact)
            self.assertFalse(source.exists())
            with self.assertRaises((ConflictError, MutationPlanConflict)):
                store.apply_mutation_plan(plan)
            self.assertEqual(destination.read_bytes(), exact)

    def test_plan_application_uses_authoritative_validation_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "goal", {"title": "Original"})
            path = root / original.relative_path
            exact = path.read_bytes()
            plan = store.plan_update_entity(
                original.entity_id,
                original.content_hash,
                {"title": "Changed"},
                None,
            )
            real_validate = validate_repository
            calls = 0

            def introduce_error(repository: pathlib.Path) -> list[str]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    return ["goals/fake.md: induced new error"]
                return real_validate(repository)

            with mock.patch(
                "webapp.store.validate_repository", side_effect=introduce_error
            ):
                with self.assertRaises(SchemaError):
                    store.apply_mutation_plan(plan)

            self.assertEqual(path.read_bytes(), exact)

    def test_same_path_plan_never_overwrites_external_inode_after_final_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "goal", {"title": "Original"})
            plan = store.plan_update_entity(
                original.entity_id,
                original.content_hash,
                {"title": "Planned"},
                None,
            )
            source = root / original.relative_path
            external = source.read_bytes().replace(b"Original", b"External")
            real_read = store._read_regular_file_with_token
            replaced = False
            reads = 0

            def read_then_replace(path: pathlib.Path):
                nonlocal reads, replaced
                result = real_read(path)
                if path == source:
                    reads += 1
                if path == source and reads == 3 and not replaced:
                    replacement = source.with_name("external-winner.md")
                    replacement.write_bytes(external)
                    os.replace(replacement, source)
                    replaced = True
                return result

            with mock.patch.object(
                store,
                "_read_regular_file_with_token",
                side_effect=read_then_replace,
            ):
                with self.assertRaises(MutationPlanConflict):
                    store.apply_mutation_plan(plan)

            self.assertTrue(replaced)
            self.assertEqual(source.read_bytes(), external)
            self.assertNotEqual(source.read_bytes(), plan.after_bytes)
            self.assertEqual(validate_repository(root), [])
            self.assertEqual(list(source.parent.glob("*.tmp")), [])
            self.assertEqual(list(source.parent.glob("*.quarantine")), [])

    def test_same_path_plan_preserves_winners_during_and_after_publication(self) -> None:
        for timing in ("during_publish", "after_publish"):
            with self.subTest(timing=timing):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    original = self.create(store, "goal", {"title": "Original"})
                    plan = store.plan_update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"title": "Planned"},
                        None,
                    )
                    source = root / original.relative_path
                    external = source.read_bytes().replace(b"Original", b"External")

                    if timing == "during_publish":
                        def collide_before_link(
                            temporary_name: str,
                            destination: pathlib.Path,
                            parent_descriptor: int,
                        ) -> None:
                            destination.write_bytes(external)
                            _link_atomic_create_no_replace(
                                temporary_name, destination, parent_descriptor
                            )

                        patches = (
                            mock.patch(
                                "webapp.store._link_atomic_create_no_replace",
                                side_effect=collide_before_link,
                            ),
                        )
                    else:
                        real_validate = validate_repository
                        validate_calls = 0

                        def replace_after_publish(
                            repository: pathlib.Path,
                        ) -> list[str]:
                            nonlocal validate_calls
                            validate_calls += 1
                            if validate_calls == 2:
                                replacement = source.with_name("external-after.md")
                                replacement.write_bytes(external)
                                os.replace(replacement, source)
                            return real_validate(repository)

                        patches = (
                            mock.patch(
                                "webapp.store.validate_repository",
                                side_effect=replace_after_publish,
                            ),
                        )

                    with patches[0]:
                        with self.assertRaises(MutationPlanConflict):
                            store.apply_mutation_plan(plan)

                    self.assertEqual(source.read_bytes(), external)
                    self.assertNotEqual(source.read_bytes(), plan.after_bytes)
                    self.assertEqual(validate_repository(root), [])
                    self.assertEqual(list(source.parent.glob("*.tmp")), [])
                    self.assertEqual(list(source.parent.glob("*.quarantine")), [])

    def test_same_path_plan_atomic_failure_restores_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "goal", {"title": "Original"})
            plan = store.plan_update_entity(
                original.entity_id,
                original.content_hash,
                {"title": "Planned"},
                None,
            )
            source = root / original.relative_path
            exact = source.read_bytes()

            with mock.patch.object(
                store,
                "_publish_no_replace",
                side_effect=OSError("simulated same-path publish failure"),
            ):
                with self.assertRaisesRegex(
                    OSError, "simulated same-path publish failure"
                ):
                    store.apply_mutation_plan(plan)

            self.assertEqual(source.read_bytes(), exact)
            self.assertEqual(validate_repository(root), [])
            self.assertEqual(list(source.parent.glob("*.tmp")), [])
            self.assertEqual(list(source.parent.glob("*.quarantine")), [])

    def test_same_path_success_is_final_before_post_validation_cleanup_boundary(self) -> None:
        for after_cleanup in ("read_fault", "external_edit"):
            with self.subTest(after_cleanup=after_cleanup):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    original = self.create(store, "goal", {"title": "Original"})
                    plan = store.plan_update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"title": "Planned"},
                        None,
                    )
                    source = root / original.relative_path
                    external = source.read_bytes().replace(b"Original", b"External")
                    real_discard = store._discard_reserved_source
                    real_read = store._read_regular_file_with_token
                    cleanup_done = False

                    def cleanup_then_boundary(
                        quarantine: pathlib.Path,
                        token,
                    ) -> None:
                        nonlocal cleanup_done
                        real_discard(quarantine, token)
                        cleanup_done = True
                        if after_cleanup == "external_edit":
                            replacement = source.with_name("external-later.md")
                            replacement.write_bytes(external)
                            os.replace(replacement, source)

                    def fail_only_post_cleanup_read(path: pathlib.Path):
                        if cleanup_done and after_cleanup == "read_fault":
                            raise OSError("post-cleanup read fault")
                        return real_read(path)

                    with (
                        mock.patch.object(
                            store,
                            "_discard_reserved_source",
                            side_effect=cleanup_then_boundary,
                        ),
                        mock.patch.object(
                            store,
                            "_read_regular_file_with_token",
                            side_effect=fail_only_post_cleanup_read,
                        ),
                    ):
                        result = store.apply_mutation_plan(plan)

                    self.assertTrue(cleanup_done)
                    self.assertEqual(result, plan.planned_entity)
                    if after_cleanup == "read_fault":
                        self.assertEqual(source.read_bytes(), plan.after_bytes)
                    else:
                        self.assertEqual(source.read_bytes(), external)
                    self.assertEqual(validate_repository(root), [])
                    self.assertEqual(list(source.parent.glob("*.tmp")), [])
                    self.assertEqual(list(source.parent.glob("*.quarantine")), [])

    def test_valid_planned_destination_winner_is_classified_before_duplicate_scan(self) -> None:
        for action in ("update", "archive"):
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    original = self.create(
                        store,
                        "task",
                        {"title": "Original", "status": "next"},
                    )
                    if action == "update":
                        plan = store.plan_update_entity(
                            original.entity_id,
                            original.content_hash,
                            {"status": "inbox"},
                            None,
                        )
                    else:
                        plan = store.plan_archive_entity(
                            original.entity_id, original.content_hash
                        )
                    source = root / original.relative_path
                    destination = root / plan.destination_relative_path
                    winner = source.read_bytes()
                    destination.write_bytes(winner)

                    with self.assertRaises(DestinationConflict):
                        store.apply_mutation_plan(plan)

                    self.assertEqual(source.read_bytes(), winner)
                    self.assertEqual(destination.read_bytes(), winner)

    def test_plan_integrity_is_bound_to_issuing_store_and_plan_entities_are_copy_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            issuing_store = Store(root)
            other_store = Store(root)
            plan = issuing_store.plan_create_entity(
                "task", {"title": "Bound plan"}, "Body"
            )
            destination = root / plan.destination_relative_path

            with self.assertRaises(TypeError):
                plan.planned_entity.frontmatter["title"] = "Tampered"
            with self.assertRaises(InputError):
                other_store.apply_mutation_plan(plan)
            self.assertFalse(destination.exists())

            forged_entity = dataclasses.replace(
                plan.planned_entity,
                frontmatter=dict(plan.planned_entity.frontmatter),
            )
            forged = dataclasses.replace(plan, planned_entity=forged_entity)
            with self.assertRaises(InputError):
                other_store.apply_mutation_plan(forged)
            self.assertFalse(destination.exists())

            forged_frontmatter = dict(plan.planned_entity.frontmatter)
            forged_frontmatter["title"] = "Consistent forgery"
            forged_after = (
                serialize_frontmatter(forged_frontmatter, "task")
                + plan.planned_entity.body
            ).encode("utf-8")
            forged_inputs = tuple(
                (key, "Consistent forgery" if key == "field:title" else value)
                for key, value in plan.operation_inputs
            )
            forged_entity = dataclasses.replace(
                plan.planned_entity,
                frontmatter=forged_frontmatter,
                content_hash=hashlib.sha256(forged_after).hexdigest(),
            )
            public_digest = hashlib.sha256()
            public_values = (
                plan.action.encode(),
                plan.entity_id.encode(),
                plan.entity_type.encode(),
                b"",
                plan.destination_relative_path.encode(),
                b"",
                b"",
                forged_after,
                *(f"{key}\0{value}".encode() for key, value in forged_inputs),
            )
            for value in public_values:
                public_digest.update(len(value).to_bytes(8, "big"))
                public_digest.update(value)
            consistent_forgery = dataclasses.replace(
                plan,
                after_bytes=forged_after,
                planned_entity=forged_entity,
                operation_inputs=forged_inputs,
                integrity_tag=public_digest.hexdigest(),
            )
            with self.assertRaises(InputError):
                issuing_store.apply_mutation_plan(consistent_forgery)
            self.assertFalse(destination.exists())

    def test_create_supports_project_goal_and_both_review_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)

            project = self.create(store, "project", {"title": "Project"})
            goal = self.create(store, "goal", {"title": "Goal"})
            daily = self.create(
                store,
                "review",
                {"period_start": "2026-07-17"},
                review_kind="daily",
            )
            weekly = self.create(
                store,
                "review",
                {"title": "Custom", "period_start": "2026-07-13"},
                review_kind="weekly",
            )

            self.assertEqual(project.relative_path, "projects/project-20260717-001.md")
            self.assertEqual(project.frontmatter["status"], "not_started")
            self.assertEqual(goal.relative_path, "goals/goal-20260717-001.md")
            self.assertEqual(goal.frontmatter["status"], "active")
            self.assertEqual(daily.relative_path, "reviews/daily/review-daily-20260717-001.md")
            self.assertEqual(daily.frontmatter["title"], "2026-07-17 Daily Review")
            self.assertNotIn("updated_at", daily.frontmatter)
            self.assertEqual(weekly.relative_path, "reviews/weekly/review-weekly-20260717-001.md")
            self.assertEqual(weekly.frontmatter["title"], "Custom")
            self.assertEqual(validate_repository(root), [])

    def test_project_planned_period_crud_clears_only_as_a_pair(self) -> None:
        """Catches Project dates bypassing generic CRUD validation or altering lane state."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            project = self.create(
                store,
                "project",
                {
                    "title": "Timed Project",
                    "planned_start_date": "2026-09-01",
                    "planned_end_date": "2026-09-30",
                },
            )
            self.assertEqual(
                project.frontmatter["planned_start_date"], "2026-09-01"
            )
            self.assertEqual(project.frontmatter["planned_end_date"], "2026-09-30")
            status = project.frontmatter["status"]
            position = project.frontmatter["kanban_position"]

            with self.assertRaisesRegex(
                InputError, "planned_start_date and planned_end_date must be paired"
            ):
                store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"planned_start_date": ""},
                    None,
                )

            cleared = store.apply_mutation_plan(
                store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"planned_start_date": "", "planned_end_date": ""},
                    None,
                )
            )
            self.assertNotIn("planned_start_date", cleared.frontmatter)
            self.assertNotIn("planned_end_date", cleared.frontmatter)
            self.assertEqual(cleared.frontmatter["status"], status)
            self.assertEqual(cleared.frontmatter["kanban_position"], position)

            archived = self.create(
                store,
                "project",
                {
                    "title": "Archive period",
                    "planned_start_date": "2026-10-01",
                    "planned_end_date": "2026-10-31",
                },
            )
            before_archive = (root / archived.relative_path).read_bytes()
            archived_result = store.apply_mutation_plan(
                store.plan_archive_entity(archived.entity_id, archived.content_hash)
            )
            self.assertEqual(
                (root / archived_result.relative_path).read_bytes(), before_archive
            )
            self.assertEqual(validate_repository(root), [])

    def test_project_legacy_status_can_only_migrate_and_new_writes_reject_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            with self.assertRaises(InputError):
                store.plan_create_entity(
                    "project", {"title": "Legacy write", "status": "active"}, "Body"
                )

            StoreTest.write(
                root / "projects/project-20260717-099.md",
                StoreTest.document(
                    "project-20260717-099", "project", "Body", status="active"
                ),
            )
            legacy = store.get_entity("project-20260717-099")
            with self.assertRaises(InputError):
                store.plan_update_entity(
                    legacy.entity_id, legacy.content_hash, {"title": "Still legacy"}, None
                )
            with self.assertRaises(InputError):
                store.plan_update_entity(
                    legacy.entity_id, legacy.content_hash, {"status": "on_hold"}, None
                )

            plan = store.plan_update_entity(
                legacy.entity_id, legacy.content_hash, {"status": "doing"}, None
            )
            migrated = store.apply_mutation_plan(plan)
            self.assertEqual(migrated.frontmatter["status"], "doing")
            with self.assertRaises(InputError):
                store.plan_update_entity(
                    migrated.entity_id,
                    migrated.content_hash,
                    {"status": "active"},
                    None,
                )

    def test_project_transition_rechecks_new_children_when_applying_preview(self) -> None:
        cases = (
            ("completed", "next", "unfinished_project_tasks"),
            ("dropped", "next", "unfinished_project_tasks"),
            ("not_started", "doing", "project_has_doing_task"),
            ("on_hold", "doing", "project_has_doing_task"),
        )
        for target_status, child_status, error_code in cases:
            with self.subTest(target_status=target_status), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                project = self.create(
                    store, "project", {"title": "Project", "status": "doing"}
                )
                transition = store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"status": target_status},
                    None,
                )
                child = store.plan_create_entity(
                    "task",
                    {
                        "title": "Concurrent child",
                        "status": child_status,
                        "project_id": project.entity_id,
                    },
                    "Body",
                )
                store.apply_mutation_plan(child)

                with self.assertRaisesRegex(ProjectOperationError, error_code):
                    store.apply_mutation_plan(transition)

                self.assertEqual(
                    store.get_entity(project.entity_id).frontmatter["status"],
                    "doing",
                )

    def test_unfinished_task_create_and_relink_reject_terminal_project_at_preview_and_apply(self) -> None:
        for terminal_status in ("completed", "dropped"):
            with self.subTest(terminal_status=terminal_status), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                terminal = self.create(
                    store,
                    "project",
                    {"title": "Terminal", "status": terminal_status},
                )
                with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                    store.plan_create_entity(
                        "task",
                        {"title": "New child", "project_id": terminal.entity_id},
                        "Body",
                    )
                standalone = self.create(
                    store, "task", {"title": "Standalone", "status": "next"}
                )
                with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                    store.plan_update_entity(
                        standalone.entity_id,
                        standalone.content_hash,
                        {"project_id": terminal.entity_id},
                        None,
                    )

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create(
                store, "project", {"title": "Project", "status": "doing"}
            )
            create_preview = store.plan_create_entity(
                "task",
                {"title": "Late child", "project_id": project.entity_id},
                "Body",
            )
            standalone = self.create(
                store, "task", {"title": "Relink later", "status": "next"}
            )
            relink_preview = store.plan_update_entity(
                standalone.entity_id,
                standalone.content_hash,
                {"project_id": project.entity_id},
                None,
            )
            completed = store.apply_mutation_plan(
                store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"status": "completed"},
                    None,
                )
            )
            self.assertEqual(completed.frontmatter["status"], "completed")
            with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                store.apply_mutation_plan(create_preview)
            with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                store.apply_mutation_plan(relink_preview)

            done = self.create(
                store,
                "task",
                {"title": "Done child", "status": "done", "project_id": project.entity_id},
            )
            self.assertEqual(done.frontmatter["project_id"], project.entity_id)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create(
                store, "project", {"title": "Project", "status": "doing"}
            )
            dependency = self.create(
                store, "task", {"title": "Dependency", "status": "next"}
            )
            target = self.create(
                store, "task", {"title": "Workflow relink", "status": "next"}
            )
            relink_workflow = store.plan_update_entity(
                target.entity_id,
                target.content_hash,
                {
                    "project_id": project.entity_id,
                    "depends_on": f"[{dependency.entity_id}]",
                },
                None,
            )
            self.assertIsInstance(relink_workflow, WorkflowPlan)
            store.apply_mutation_plan(
                store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"status": "completed"},
                    None,
                )
            )
            with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                store.apply_task_workflow(relink_workflow)

    def test_regular_update_cannot_start_task_and_doing_relink_requires_doing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            task = self.create(store, "task", {"title": "Next", "status": "next"})
            with self.assertRaisesRegex(InputError, "dedicated start workflow"):
                store.plan_update_entity(
                    task.entity_id,
                    task.content_hash,
                    {"status": "doing"},
                    None,
                )

            held_target = self.create(
                store, "project", {"title": "Not started", "status": "not_started"}
            )
            doing_target = self.create(
                store, "project", {"title": "Doing", "status": "doing"}
            )
            doing = self.create(
                store, "task", {"title": "Already doing", "status": "doing"}
            )
            with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                store.plan_update_entity(
                    doing.entity_id,
                    doing.content_hash,
                    {"project_id": held_target.entity_id},
                    None,
                )

            edited = store.apply_mutation_plan(
                store.plan_update_entity(
                    doing.entity_id,
                    doing.content_hash,
                    {"title": "Still doing", "project_id": doing_target.entity_id},
                    None,
                )
            )
            self.assertEqual(edited.frontmatter["status"], "doing")
            self.assertEqual(edited.frontmatter["project_id"], doing_target.entity_id)

    def test_low_level_store_create_keeps_doing_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create(store, "project", {"title": "Project"})
            created = self.create(
                store,
                "task",
                {
                    "title": "Low level",
                    "status": "doing",
                    "project_id": project.entity_id,
                },
            )
            self.assertEqual(created.frontmatter["status"], "doing")
            self.assertEqual(created.frontmatter["project_id"], project.entity_id)

    def test_doing_task_mutation_relink_apply_rechecks_latest_project_status(self) -> None:
        for latest_status in (
            "not_started",
            "on_hold",
            "completed",
            "dropped",
            "active",
        ):
            with (
                self.subTest(latest_status=latest_status),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                project = self.create(
                    store, "project", {"title": "Project", "status": "doing"}
                )
                task = self.create(
                    store, "task", {"title": "Doing", "status": "doing"}
                )
                relink = store.plan_update_entity(
                    task.entity_id,
                    task.content_hash,
                    {"project_id": project.entity_id},
                    None,
                )
                if latest_status == "active":
                    StoreTest.write(
                        root / project.relative_path,
                        StoreTest.document(
                            project.entity_id,
                            "project",
                            project.body,
                            status="active",
                        ),
                    )
                else:
                    store.apply_mutation_plan(
                        store.plan_update_entity(
                            project.entity_id,
                            project.content_hash,
                            {"status": latest_status},
                            None,
                        )
                    )

                with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                    store.apply_mutation_plan(relink)
                self.assertNotIn(
                    "project_id", store.get_entity(task.entity_id).frontmatter
                )

    def test_doing_task_workflow_relink_uses_projected_or_latest_project_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create(
                store, "project", {"title": "Project", "status": "doing"}
            )
            dependency = self.create(
                store, "task", {"title": "Done dependency", "status": "done"}
            )
            task = self.create(
                store, "task", {"title": "Doing", "status": "doing"}
            )
            relink = store.plan_update_entity(
                task.entity_id,
                task.content_hash,
                {
                    "project_id": project.entity_id,
                    "depends_on": f"[{dependency.entity_id}]",
                },
                None,
            )
            self.assertIsInstance(relink, WorkflowPlan)
            store.apply_mutation_plan(
                store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"status": "on_hold"},
                    None,
                )
            )

            with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                store.apply_task_workflow(relink)
            self.assertNotIn("project_id", store.get_entity(task.entity_id).frontmatter)

    def test_create_rejects_invalid_fields_links_and_collisions_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            goal = self.create(store, "goal", {"title": "Existing goal"})
            baseline = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md"))
            cases = (
                ("missing title", "task", {}, "Body", None),
                ("empty title", "task", {"title": "  "}, "Body", None),
                ("immutable", "task", {"title": "X", "id": "client"}, "Body", None),
                ("forbidden", "task", {"title": "X", "priority": "high"}, "Body", None),
                ("unknown", "task", {"title": "X", "extra": "value"}, "Body", None),
                ("non-string field", "task", {"title": 3}, "Body", None),  # type: ignore[dict-item]
                ("non-string body", "task", {"title": "X"}, 3, None),  # type: ignore[arg-type]
                ("dangling project", "task", {"title": "X", "project_id": "project-20260717-999"}, "Body", None),
                ("wrong link type", "task", {"title": "X", "project_id": goal.entity_id}, "Body", None),
                ("missing review kind", "review", {"period_start": "2026-07-17"}, "Body", None),
                ("invalid review kind", "review", {"period_start": "2026-07-17"}, "Body", "monthly"),
                ("review status", "review", {"period_start": "2026-07-17", "status": "active"}, "Body", "daily"),
            )
            for name, entity_type, fields, body, review_kind in cases:
                with self.subTest(name=name):
                    with self.assertRaises(InputError):
                        self.create(store, entity_type, fields, body, review_kind)
                    self.assertEqual(
                        sorted(path.relative_to(root).as_posix() for path in root.rglob("*.md")),
                        baseline,
                    )

            for name, entity_type, fields, review_kind in (
                ("invalid status", "task", {"title": "X", "status": "active"}, None),
                ("invalid period", "review", {"period_start": "2026-02-30"}, "daily"),
            ):
                with self.subTest(name=name):
                    with self.assertRaises(SchemaError):
                        self.create(
                            store,
                            entity_type,
                            fields,
                            "Body",
                            review_kind,
                        )
                    self.assertEqual(
                        sorted(
                            path.relative_to(root).as_posix()
                            for path in root.rglob("*.md")
                        ),
                        baseline,
                    )

            collision = root / "inbox/task-20260717-001.md"
            collision.write_bytes(b"occupied")
            with mock.patch.object(store, "next_entity_id", return_value="task-20260717-001"):
                with self.assertRaises(InputError):
                    self.create(store, "task", {"title": "Collision"})
            self.assertEqual(collision.read_bytes(), b"occupied")

    def test_create_validates_confirmed_project_and_goal_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            goal = self.create(store, "goal", {"title": "Goal"})
            project = self.create(
                store,
                "project",
                {"title": "Project", "goal_id": goal.entity_id},
            )
            task = self.create(
                store,
                "task",
                {"title": "Action", "status": "next", "project_id": project.entity_id},
            )

            self.assertEqual(project.frontmatter["goal_id"], goal.entity_id)
            self.assertEqual(task.frontmatter["project_id"], project.entity_id)
            self.assertEqual(task.relative_path, f"tasks/{task.entity_id}.md")

    def test_concurrent_creates_generate_unique_ids_inside_common_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            context = multiprocessing.get_context("fork")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(target=_create_task_in_process, args=(str(root), start, results))
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start.set()
            returned = [results.get(timeout=5.0) for _ in processes]
            for process in processes:
                process.join(5.0)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual([error for _, _, error in returned], [None, None])
            self.assertEqual(len({entity_id for entity_id, _, _ in returned}), 2)
            self.assertEqual(validate_repository(root), [])

    def test_update_merges_fields_replaces_body_and_preserves_immutable_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store,
                "project",
                {"title": "Original", "status": "on_hold"},
                "Old body",
            )
            later = self.FIXED_NOW + datetime.timedelta(hours=1)

            with mock.patch("webapp.store.current_time", return_value=later):
                updated = store.update_entity(
                    original.entity_id,
                    original.content_hash,
                    {"title": "Updated", "status": "doing"},
                    "New body\n\nDetails",
                )

            self.assertEqual(updated.frontmatter["id"], original.frontmatter["id"])
            self.assertEqual(updated.frontmatter["type"], original.frontmatter["type"])
            self.assertEqual(updated.frontmatter["created_at"], original.frontmatter["created_at"])
            self.assertEqual(updated.frontmatter["updated_at"], "2026-07-17T13:34:56+09:00")
            self.assertEqual(updated.body, "\nNew body\n\nDetails\n")
            self.assertEqual(store.get_entity(original.entity_id), updated)

    def test_update_none_body_preserves_exact_body_and_empty_optional_removes_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            goal = self.create(store, "goal", {"title": "Goal"})
            original = self.create(
                store,
                "project",
                {"title": "Project", "goal_id": goal.entity_id},
                "Body without canonical concern",
            )
            exact_body = original.body

            updated = store.update_entity(
                original.entity_id,
                original.content_hash,
                {"goal_id": ""},
                None,
            )

            self.assertNotIn("goal_id", updated.frontmatter)
            self.assertEqual(updated.body, exact_body)

    def test_task_status_moves_between_inbox_and_tasks_and_collision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            inbox = self.create(store, "task", {"title": "Move me"})

            next_entity = store.update_entity(
                inbox.entity_id, inbox.content_hash, {"status": "next"}, None
            )
            self.assertEqual(next_entity.relative_path, f"tasks/{inbox.entity_id}.md")
            self.assertFalse((root / inbox.relative_path).exists())

            inbox_again = store.update_entity(
                next_entity.entity_id,
                next_entity.content_hash,
                {"status": "inbox"},
                None,
            )
            self.assertEqual(inbox_again.relative_path, f"inbox/{inbox.entity_id}.md")
            self.assertFalse((root / next_entity.relative_path).exists())

            occupied = root / "tasks" / inbox.entity_id
            occupied = occupied.with_suffix(".md")
            StoreTest.write(
                occupied,
                StoreTest.document("task-20260717-777", "task", "Occupied\n", status="next"),
            )
            latest = store.get_entity(inbox.entity_id)
            with self.assertRaises(InputError):
                store.update_entity(latest.entity_id, latest.content_hash, {"status": "next"}, None)
            self.assertEqual((root / latest.relative_path).read_bytes(), (root / inbox_again.relative_path).read_bytes())
            self.assertIn(b"Occupied", occupied.read_bytes())

    def test_update_rejects_stale_hash_and_invalid_patch_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "task", {"title": "Original", "status": "next"})
            path = root / original.relative_path
            latest_bytes = path.read_bytes().replace(b"Original", b"External")
            path.write_bytes(latest_bytes)

            with self.assertRaises(ConflictError):
                store.update_entity(original.entity_id, original.content_hash, {"title": "Overwrite"}, None)
            self.assertEqual(path.read_bytes(), latest_bytes)

            latest = store.get_entity(original.entity_id)
            for fields in (
                {"id": "changed"},
                {"type": "goal"},
                {"created_at": "2026-01-01T00:00:00+09:00"},
                {"updated_at": "2026-01-01T00:00:00+09:00"},
                {"priority": "high"},
                {"extra": "value"},
                {"title": ""},
            ):
                with self.subTest(fields=fields):
                    with self.assertRaises(InputError):
                        store.update_entity(latest.entity_id, latest.content_hash, fields, None)
                    self.assertEqual(path.read_bytes(), latest_bytes)

            with self.assertRaises(SchemaError):
                store.update_entity(
                    latest.entity_id,
                    latest.content_hash,
                    {"status": "active"},
                    None,
                )
            self.assertEqual(path.read_bytes(), latest_bytes)

    def test_new_validator_error_rolls_back_exact_bytes_and_reports_only_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "review", {"period_start": "2026-07-17"}, review_kind="daily")
            path = root / original.relative_path
            before = path.read_bytes()
            (root / "tasks/bad.md").write_text(
                "not frontmatter\n", encoding="utf-8"
            )
            existing = "tasks/bad.md: missing opening ---"
            introduced = (
                f"{original.relative_path}: invalid date for period_start: bad"
            )

            with self.assertRaises(SchemaError) as raised:
                store.update_entity(
                    original.entity_id,
                    original.content_hash,
                    {"period_start": "bad"},
                    None,
                )

            self.assertEqual(raised.exception.errors, [introduced])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(validate_repository(root), [existing])

    def test_preexisting_unrelated_validator_error_does_not_block_valid_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            (root / "tasks/bad.md").write_text("not frontmatter\n", encoding="utf-8")
            before_errors = validate_repository(root)

            entity = self.create(Store(root), "goal", {"title": "Valid goal"})

            self.assertTrue(before_errors)
            self.assertEqual(validate_repository(root), before_errors)
            self.assertEqual(entity.relative_path, f"goals/{entity.entity_id}.md")

    def test_archive_preserves_exact_bytes_and_uses_deterministic_collision_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "task", {"title": "Archive", "status": "next"}, "Exact body")
            source = root / original.relative_path
            exact = source.read_bytes()
            occupied = root / "archive" / source.name
            occupied.write_bytes(
                StoreTest.document(
                    "task-20260717-777", "task", "Occupied\n", status="done"
                )
            )

            archived = store.archive_entity(original.entity_id, original.content_hash)

            self.assertEqual(archived.relative_path, f"archive/{source.stem}-2.md")
            self.assertEqual((root / archived.relative_path).read_bytes(), exact)
            self.assertIn(b"Occupied", occupied.read_bytes())
            self.assertFalse(source.exists())
            self.assertEqual(archived.frontmatter, original.frontmatter)

    def test_create_move_and_archive_races_never_overwrite_noncooperating_file(self) -> None:
        collision_bytes = b"noncooperating writer\n"

        for operation_kind in ("create", "move", "archive"):
            with self.subTest(operation_kind=operation_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    source: pathlib.Path | None = None
                    original_bytes: bytes | None = None
                    if operation_kind == "move":
                        original = self.create(
                            store,
                            "task",
                            {"title": "Move race", "status": "next"},
                        )
                        source = root / original.relative_path
                        destination = root / "inbox" / source.name
                        original_bytes = source.read_bytes()
                    elif operation_kind == "archive":
                        original = self.create(
                            store,
                            "task",
                            {"title": "Archive race", "status": "next"},
                        )
                        source = root / original.relative_path
                        destination = root / "archive" / source.name
                        original_bytes = source.read_bytes()
                    else:
                        original = None
                        destination = root / "inbox/task-20260717-001.md"

                    real_link = os.link

                    def collide_then_link(
                        source_name: str,
                        destination_name: str,
                        *,
                        src_dir_fd: int | None = None,
                        dst_dir_fd: int | None = None,
                        follow_symlinks: bool = True,
                    ) -> None:
                        descriptor = os.open(
                            destination_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=dst_dir_fd,
                        )
                        try:
                            os.write(descriptor, collision_bytes)
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                        real_link(
                            source_name,
                            destination_name,
                            src_dir_fd=src_dir_fd,
                            dst_dir_fd=dst_dir_fd,
                            follow_symlinks=follow_symlinks,
                        )

                    with mock.patch(
                        "webapp.store.os.link", side_effect=collide_then_link
                    ):
                        with self.assertRaises(InputError):
                            if operation_kind == "create":
                                with mock.patch(
                                    "webapp.store.current_time",
                                    return_value=self.FIXED_NOW,
                                ):
                                    store.create_entity(
                                        "task", {"title": "Create race"}, "Body"
                                    )
                            elif operation_kind == "move":
                                store.update_entity(
                                    original.entity_id,
                                    original.content_hash,
                                    {"status": "inbox"},
                                    None,
                                )
                            else:
                                store.archive_entity(
                                    original.entity_id, original.content_hash
                                )

                    self.assertEqual(destination.read_bytes(), collision_bytes)
                    if source is not None:
                        self.assertEqual(source.read_bytes(), original_bytes)
                    self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_move_and_archive_keep_destination_when_source_restore_fails(self) -> None:
        for operation_kind in ("move", "archive"):
            with self.subTest(operation_kind=operation_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    original = self.create(
                        store,
                        "task",
                        {"title": operation_kind, "status": "next"},
                        "Exact body",
                    )
                    source = root / original.relative_path
                    source_bytes = source.read_bytes()
                    destination = (
                        root / "inbox" / source.name
                        if operation_kind == "move"
                        else root / "archive" / source.name
                    )
                    later = self.FIXED_NOW + datetime.timedelta(hours=1)
                    if operation_kind == "move":
                        expected_frontmatter = dict(original.frontmatter)
                        expected_frontmatter["status"] = "inbox"
                        expected_frontmatter["updated_at"] = later.isoformat(
                            timespec="seconds"
                        )
                        expected_destination = (
                            serialize_frontmatter(expected_frontmatter, "task")
                            + original.body
                        ).encode("utf-8")
                    else:
                        expected_destination = source_bytes
                    real_quarantine_delete = (
                        store._quarantine_delete_owned_path
                    )

                    def delete_source_then_fail(
                        path: pathlib.Path,
                        expected_token,
                        *,
                        ownership_kind: str,
                    ) -> None:
                        real_quarantine_delete(
                            path,
                            expected_token,
                            ownership_kind=ownership_kind,
                        )
                        if path == source:
                            raise OSError("primary source quarantine failure")

                    def fail_source_restore(
                        path: pathlib.Path, data: bytes
                    ) -> None:
                        if path == source:
                            raise OSError("source restore failure")
                        atomic_write(path, data)

                    with (
                        mock.patch(
                            "webapp.store.Store._quarantine_delete_owned_path",
                            side_effect=delete_source_then_fail,
                        ),
                        mock.patch(
                            "webapp.store.atomic_write",
                            side_effect=fail_source_restore,
                        ),
                        mock.patch(
                            "webapp.store.current_time", return_value=later
                        ),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "source restore failure"
                        ) as raised:
                            if operation_kind == "move":
                                store.update_entity(
                                    original.entity_id,
                                    original.content_hash,
                                    {"status": "inbox"},
                                    None,
                                )
                            else:
                                store.archive_entity(
                                    original.entity_id, original.content_hash
                                )

                    self.assertIsInstance(raised.exception.__cause__, OSError)
                    self.assertIn(
                        "primary source quarantine failure",
                        str(raised.exception.__cause__),
                    )
                    self.assertFalse(source.exists())
                    self.assertEqual(
                        destination.read_bytes(), expected_destination
                    )
                    self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_move_and_archive_keep_both_copies_when_destination_cleanup_fails(self) -> None:
        for operation_kind in ("move", "archive"):
            with self.subTest(operation_kind=operation_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(pathlib.Path(temporary_directory))
                    store = Store(root)
                    original = self.create(
                        store,
                        "task",
                        {"title": operation_kind, "status": "next"},
                        "Exact body",
                    )
                    source = root / original.relative_path
                    source_bytes = source.read_bytes()
                    destination = (
                        root / "inbox" / source.name
                        if operation_kind == "move"
                        else root / "archive" / source.name
                    )
                    later = self.FIXED_NOW + datetime.timedelta(hours=1)
                    if operation_kind == "move":
                        expected_frontmatter = dict(original.frontmatter)
                        expected_frontmatter["status"] = "inbox"
                        expected_frontmatter["updated_at"] = later.isoformat(
                            timespec="seconds"
                        )
                        expected_destination = (
                            serialize_frontmatter(expected_frontmatter, "task")
                            + original.body
                        ).encode("utf-8")
                    else:
                        expected_destination = source_bytes
                    real_safe_unlink = safe_unlink

                    def fail_by_stage(
                        path: pathlib.Path, repository_root: pathlib.Path
                    ) -> None:
                        if path.parent == source.parent:
                            real_safe_unlink(path, repository_root)
                            raise OSError("primary post-quarantine failure")
                        if path.parent == destination.parent:
                            real_safe_unlink(path, repository_root)
                            raise OSError("destination cleanup failure")
                        self.fail(f"unexpected unlink path: {path}")

                    with (
                        mock.patch(
                            "webapp.store.safe_unlink",
                            side_effect=fail_by_stage,
                        ),
                        mock.patch(
                            "webapp.store.current_time", return_value=later
                        ),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "destination cleanup failure"
                        ) as raised:
                            if operation_kind == "move":
                                store.update_entity(
                                    original.entity_id,
                                    original.content_hash,
                                    {"status": "inbox"},
                                    None,
                                )
                            else:
                                store.archive_entity(
                                    original.entity_id, original.content_hash
                                )

                    self.assertIsInstance(
                        raised.exception.__cause__, RuntimeError
                    )
                    self.assertIsInstance(
                        raised.exception.__cause__.__cause__, OSError
                    )
                    self.assertIn(
                        "primary post-quarantine failure",
                        str(raised.exception.__cause__),
                    )
                    self.assertEqual(source.read_bytes(), source_bytes)
                    preserved = list(destination.parent.glob("*.quarantine"))
                    self.assertEqual(len(preserved), 1)
                    self.assertEqual(
                        preserved[0].read_bytes(), expected_destination
                    )
                    self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_move_and_archive_failure_matrix_restores_exact_prior_state(self) -> None:
        for operation_kind in ("move", "archive"):
            for failure_stage in ("pre_unlink", "post_unlink", "secure_reread"):
                with self.subTest(
                    operation_kind=operation_kind, failure_stage=failure_stage
                ):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        root = self.make_repository(
                            pathlib.Path(temporary_directory)
                        )
                        store = Store(root)
                        original = self.create(
                            store,
                            "task",
                            {"title": operation_kind, "status": "next"},
                            "Exact body",
                        )
                        source = root / original.relative_path
                        source_bytes = source.read_bytes()
                        destination = (
                            root / "inbox" / source.name
                            if operation_kind == "move"
                            else root / "archive" / source.name
                        )
                        real_quarantine_delete = (
                            store._quarantine_delete_owned_path
                        )
                        real_read_entity = store._read_entity

                        def fail_quarantine_stage(
                            path: pathlib.Path,
                            expected_token,
                            *,
                            ownership_kind: str,
                        ) -> None:
                            if path == source and failure_stage == "pre_unlink":
                                raise OSError("pre-unlink failure")
                            real_quarantine_delete(
                                path,
                                expected_token,
                                ownership_kind=ownership_kind,
                            )
                            if path == source and failure_stage == "post_unlink":
                                raise OSError("post-unlink failure")

                        def fail_result_reread(
                            path: pathlib.Path,
                        ):
                            if (
                                failure_stage == "secure_reread"
                                and path == destination
                                and destination.exists()
                            ):
                                raise OSError("secure reread failure")
                            return real_read_entity(path)

                        with (
                            mock.patch(
                                "webapp.store.Store._quarantine_delete_owned_path",
                                side_effect=fail_quarantine_stage,
                            ),
                            mock.patch.object(
                                store,
                                "_read_entity",
                                side_effect=fail_result_reread,
                            ),
                        ):
                            with self.assertRaisesRegex(
                                OSError,
                                failure_stage.replace("_", "[- ]"),
                            ):
                                if operation_kind == "move":
                                    store.update_entity(
                                        original.entity_id,
                                        original.content_hash,
                                        {"status": "inbox"},
                                        None,
                                    )
                                else:
                                    store.archive_entity(
                                        original.entity_id,
                                        original.content_hash,
                                    )

                        self.assertEqual(source.read_bytes(), source_bytes)
                        self.assertFalse(destination.exists())
                        self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_rollback_never_deletes_destination_replaced_by_external_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store,
                "task",
                {"title": "Ownership race", "status": "next"},
            )
            source = root / original.relative_path
            source_bytes = source.read_bytes()
            destination = root / "inbox" / source.name
            external_bytes = b"external replacement\n"
            real_quarantine_delete = store._quarantine_delete_owned_path

            def replace_destination_before_source_unlink(
                path: pathlib.Path,
                expected_token,
                *,
                ownership_kind: str,
            ) -> None:
                if path == source:
                    replacement = root / "external-winner"
                    replacement.write_bytes(external_bytes)
                    os.replace(replacement, destination)
                    raise OSError("primary source quarantine failure")
                real_quarantine_delete(
                    path,
                    expected_token,
                    ownership_kind=ownership_kind,
                )

            with mock.patch(
                "webapp.store.Store._quarantine_delete_owned_path",
                side_effect=replace_destination_before_source_unlink,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "publication token mismatch"
                ) as raised:
                    store.update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"status": "inbox"},
                        None,
                    )

            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn(
                "primary source quarantine failure", str(raised.exception.__cause__)
            )
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(destination.read_bytes(), external_bytes)
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_identical_bytes_external_inode_survives_destination_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store,
                "task",
                {"title": "Identical inode race", "status": "next"},
            )
            source = root / original.relative_path
            source_bytes = source.read_bytes()
            destination = root / "inbox" / source.name
            real_atomic_create = __import__(
                "webapp.store", fromlist=["atomic_create_no_replace"]
            ).atomic_create_no_replace
            real_read_entity = store._read_entity
            external_inode: tuple[int, int] | None = None

            def publish_then_replace_with_identical_bytes(
                path: pathlib.Path, data: bytes
            ):
                nonlocal external_inode
                token = real_atomic_create(path, data)
                replacement = root / "external-identical"
                replacement.write_bytes(data)
                os.replace(replacement, path)
                current = os.stat(path, follow_symlinks=False)
                external_inode = (current.st_dev, current.st_ino)
                return token

            def fail_result_reread(path: pathlib.Path):
                if path == destination and destination.exists():
                    raise OSError("post-write secure reread failure")
                return real_read_entity(path)

            with (
                mock.patch(
                    "webapp.store.atomic_create_no_replace",
                    side_effect=publish_then_replace_with_identical_bytes,
                ),
                mock.patch.object(
                    store, "_read_entity", side_effect=fail_result_reread
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "publication token mismatch"
                ) as raised:
                    store.update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"status": "inbox"},
                        None,
                    )

            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(destination.is_file())
            current = os.stat(destination, follow_symlinks=False)
            self.assertEqual((current.st_dev, current.st_ino), external_inode)
            self.assertEqual(list(root.rglob("*.quarantine")), [])

    def test_destination_replacement_at_quarantine_rename_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store,
                "task",
                {"title": "Late destination race", "status": "next"},
            )
            source = root / original.relative_path
            source_bytes = source.read_bytes()
            destination = root / "inbox" / source.name
            external_candidate = root / "external-destination-candidate"
            external_bytes = b"late external winner\n"
            external_candidate.write_bytes(external_bytes)
            external_stat = os.stat(external_candidate, follow_symlinks=False)
            real_read_entity = store._read_entity
            replaced = False

            def replace_at_rename(
                source_path: pathlib.Path,
                destination_path: pathlib.Path,
                repository_root: pathlib.Path,
            ) -> None:
                nonlocal replaced
                if source_path == destination and not replaced:
                    os.replace(external_candidate, destination)
                    replaced = True
                os.rename(source_path, destination_path)

            def fail_result_reread(path: pathlib.Path):
                if path == destination and destination.exists():
                    raise OSError("post-write secure reread failure")
                return real_read_entity(path)

            with (
                mock.patch(
                    "webapp.store._rename_no_replace",
                    create=True,
                    side_effect=replace_at_rename,
                ),
                mock.patch.object(
                    store, "_read_entity", side_effect=fail_result_reread
                ),
            ):
                with self.assertRaises(RuntimeError):
                    store.update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"status": "inbox"},
                        None,
                    )

            self.assertTrue(replaced)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(destination.read_bytes(), external_bytes)
            current = os.stat(destination, follow_symlinks=False)
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (external_stat.st_dev, external_stat.st_ino),
            )

    def test_blocked_destination_restore_preserves_live_winner_and_quarantine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store,
                "task",
                {"title": "Blocked restore race", "status": "next"},
            )
            source = root / original.relative_path
            source_bytes = source.read_bytes()
            destination = root / "inbox" / source.name
            external_candidate = root / "external-destination"
            external_bytes = b"external quarantined winner\n"
            newest_bytes = b"newest live winner\n"
            external_candidate.write_bytes(external_bytes)
            real_read_entity = store._read_entity
            quarantine: pathlib.Path | None = None

            def block_external_restore(
                source_path: pathlib.Path,
                destination_path: pathlib.Path,
                repository_root: pathlib.Path,
            ) -> None:
                nonlocal quarantine
                if source_path == destination and quarantine is None:
                    os.replace(external_candidate, destination)
                    quarantine = destination_path
                    os.rename(source_path, destination_path)
                    return
                if quarantine is not None and source_path == quarantine:
                    destination.write_bytes(newest_bytes)
                    raise FileExistsError("newer live winner")
                os.rename(source_path, destination_path)

            def fail_result_reread(path: pathlib.Path):
                if path == destination and destination.exists():
                    raise OSError("post-write secure reread failure")
                return real_read_entity(path)

            with (
                mock.patch(
                    "webapp.store._rename_no_replace",
                    side_effect=block_external_restore,
                ),
                mock.patch.object(
                    store, "_read_entity", side_effect=fail_result_reread
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "preserved quarantine path"
                ):
                    store.update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"status": "inbox"},
                        None,
                    )

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(destination.read_bytes(), newest_bytes)
            self.assertIsNotNone(quarantine)
            self.assertEqual(quarantine.read_bytes(), external_bytes)

    def test_rename_post_effect_failures_restore_external_winner(self) -> None:
        for failure_stage in ("fsync", "close"):
            with self.subTest(failure_stage=failure_stage):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = self.make_repository(
                        pathlib.Path(temporary_directory)
                    )
                    store = Store(root)
                    owned = root / "tasks" / "owned-token"
                    owned.write_bytes(b"owned inode\n")
                    _, expected_token = store._read_regular_file_with_token(
                        owned
                    )
                    target = root / "tasks" / "external-winner"
                    external_bytes = b"canonical external winner\n"
                    target.write_bytes(external_bytes)
                    external_stat = os.stat(target, follow_symlinks=False)
                    real_fsync = os.fsync
                    real_close = os.close
                    failed = False

                    def fail_first_fsync(descriptor: int) -> None:
                        nonlocal failed
                        if not failed:
                            failed = True
                            raise OSError("rename post-effect fsync failure")
                        real_fsync(descriptor)

                    def close_then_fail_once(descriptor: int) -> None:
                        nonlocal failed
                        real_close(descriptor)
                        if not failed:
                            failed = True
                            raise OSError("rename post-effect close failure")

                    patcher = (
                        mock.patch(
                            "webapp.store.os.fsync",
                            side_effect=fail_first_fsync,
                        )
                        if failure_stage == "fsync"
                        else mock.patch(
                            "webapp.store.os.close",
                            side_effect=close_then_fail_once,
                        )
                    )
                    with patcher:
                        with self.assertRaisesRegex(
                            RuntimeError, "post-effect"
                        ) as raised:
                            store._quarantine_delete_owned_path(
                                target,
                                expected_token,
                                ownership_kind="publication",
                            )

                    self.assertTrue(failed)
                    self.assertIn(
                        f"rename post-effect {failure_stage} failure",
                        str(raised.exception.__cause__),
                    )
                    self.assertEqual(target.read_bytes(), external_bytes)
                    current = os.stat(target, follow_symlinks=False)
                    self.assertEqual(
                        (current.st_dev, current.st_ino),
                        (external_stat.st_dev, external_stat.st_ino),
                    )
                    self.assertEqual(list(root.rglob("*.quarantine")), [])

    def test_publication_close_failure_keeps_token_for_exact_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            destination = root / "inbox" / "task-20260717-001.md"
            real_open_parent = __import__(
                "webapp.store",
                fromlist=["_open_verified_atomic_create_parent"],
            )._open_verified_atomic_create_parent
            real_close = os.close
            atomic_parent_descriptor: int | None = None
            failed = False

            def capture_atomic_parent(*args: object) -> int:
                nonlocal atomic_parent_descriptor
                descriptor = real_open_parent(*args)
                atomic_parent_descriptor = descriptor
                return descriptor

            def close_parent_then_fail(descriptor: int) -> None:
                nonlocal failed
                real_close(descriptor)
                if descriptor == atomic_parent_descriptor and not failed:
                    failed = True
                    raise OSError("publication parent close failure")

            with (
                mock.patch(
                    "webapp.store._open_verified_atomic_create_parent",
                    side_effect=capture_atomic_parent,
                ),
                mock.patch(
                    "webapp.store.os.close",
                    side_effect=close_parent_then_fail,
                ),
                mock.patch(
                    "webapp.store.current_time", return_value=self.FIXED_NOW
                ),
            ):
                with self.assertRaisesRegex(
                    OSError, "publication parent close failure"
                ):
                    Store(root).create_entity(
                        "task", {"title": "Close rollback"}, "Body"
                    )

            self.assertTrue(failed)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.rglob("*.quarantine")), [])
            self.assertEqual(list(root.rglob("*.tmp")), [])
            self.assertEqual(validate_repository(root), [])

    def test_source_replacement_before_quarantine_delete_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(
                store,
                "task",
                {"title": "Late source race", "status": "next"},
            )
            source = root / original.relative_path
            destination = root / "inbox" / source.name
            external_candidate = root / "external-source-candidate"
            external_bytes = source.read_bytes()
            external_candidate.write_bytes(external_bytes)
            external_stat = os.stat(external_candidate, follow_symlinks=False)
            replaced = False

            def replace_source_at_rename(
                source_path: pathlib.Path,
                destination_path: pathlib.Path,
                repository_root: pathlib.Path,
            ) -> None:
                nonlocal replaced
                if source_path == source and not replaced:
                    os.replace(external_candidate, source)
                    replaced = True
                os.rename(source_path, destination_path)

            with mock.patch(
                "webapp.store._rename_no_replace",
                create=True,
                side_effect=replace_source_at_rename,
            ):
                with self.assertRaisesRegex(
                    InputError, "source ownership changed"
                ):
                    store.update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"status": "inbox"},
                        None,
                    )

            self.assertTrue(replaced)
            self.assertEqual(source.read_bytes(), external_bytes)
            current = os.stat(source, follow_symlinks=False)
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (external_stat.st_dev, external_stat.st_ino),
            )
            self.assertFalse(destination.exists())

    def test_atomic_create_reports_write_error_plus_residual_temp_cleanup(self) -> None:
        self._assert_atomic_create_temp_cleanup_failure("write")

    def test_atomic_create_reports_link_error_plus_residual_temp_cleanup(self) -> None:
        self._assert_atomic_create_temp_cleanup_failure("link")

    def _assert_atomic_create_temp_cleanup_failure(self, stage: str) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            real_fdopen = os.fdopen
            real_unlink = os.unlink

            class FailingWriter:
                def __init__(self, descriptor: int, mode: str) -> None:
                    self._file = real_fdopen(descriptor, mode)

                def __enter__(self):
                    self._file.__enter__()
                    return self

                def __exit__(self, *args: object):
                    return self._file.__exit__(*args)

                def write(self, data: bytes) -> int:
                    raise OSError("primary write failure")

            def leave_temporary(
                target: str,
                *,
                dir_fd: int | None = None,
            ) -> None:
                if str(target).endswith(".tmp"):
                    raise OSError("temporary unlink failure")
                real_unlink(target, dir_fd=dir_fd)

            patches = [
                mock.patch("webapp.store.os.unlink", side_effect=leave_temporary)
            ]
            if stage == "write":
                patches.append(
                    mock.patch(
                        "webapp.store.os.fdopen",
                        side_effect=lambda descriptor, mode: FailingWriter(
                            descriptor, mode
                        ),
                    )
                )
            else:
                patches.append(
                    mock.patch(
                        "webapp.store.os.link",
                        side_effect=OSError("primary link failure"),
                    )
                )

            with patches[0], patches[1]:
                with self.assertRaisesRegex(
                    RuntimeError, "residual temporary path"
                ) as raised:
                    with mock.patch(
                        "webapp.store.current_time", return_value=self.FIXED_NOW
                    ):
                        Store(root).create_entity(
                            "task", {"title": "Temp cleanup"}, "Body"
                        )

            temporary_files = list((root / "inbox").glob(".*.tmp"))
            self.assertEqual(len(temporary_files), 1)
            self.assertIn(str(temporary_files[0]), str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn(f"primary {stage} failure", str(raised.exception.__cause__))

    def test_move_failures_restore_every_touched_path_without_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "task", {"title": "Move", "status": "next"})
            source = root / original.relative_path
            destination = root / "inbox" / source.name
            exact = source.read_bytes()
            with mock.patch(
                "webapp.store.atomic_create_no_replace",
                side_effect=OSError("simulated atomic failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated atomic failure"):
                    store.update_entity(original.entity_id, original.content_hash, {"status": "inbox"}, None)

            self.assertEqual(source.read_bytes(), exact)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.rglob("*.tmp")), [])

            real_fsync = os.fsync
            fsync_calls = 0

            def fail_source_directory_fsync(descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 3:
                    raise OSError("simulated unlink fsync failure")
                real_fsync(descriptor)

            latest = store.get_entity(original.entity_id)
            with mock.patch("webapp.store.os.fsync", side_effect=fail_source_directory_fsync):
                with self.assertRaisesRegex(OSError, "simulated unlink fsync failure"):
                    store.update_entity(latest.entity_id, latest.content_hash, {"status": "inbox"}, None)

            self.assertEqual(source.read_bytes(), exact)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_rollback_failure_raises_clear_runtime_error_chained_from_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            store = Store(root)
            original = self.create(store, "goal", {"title": "Original"})
            real_validate = validate_repository
            validate_calls = 0
            rename_calls = 0

            def validation_failure(repository: pathlib.Path) -> list[str]:
                nonlocal validate_calls
                validate_calls += 1
                if validate_calls == 2:
                    return ["goals/fake.md: primary validation failure"]
                return real_validate(repository)

            def fail_restore_rename(
                source: pathlib.Path,
                destination: pathlib.Path,
                repository: pathlib.Path,
            ) -> None:
                nonlocal rename_calls
                rename_calls += 1
                if rename_calls == 3:
                    raise OSError("rollback rename failure")
                _rename_no_replace(source, destination, repository)

            with (
                mock.patch(
                    "webapp.store.validate_repository",
                    side_effect=validation_failure,
                ),
                mock.patch(
                    "webapp.store._rename_no_replace",
                    side_effect=fail_restore_rename,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback failed") as raised:
                    store.update_entity(
                        original.entity_id,
                        original.content_hash,
                        {"title": "Changed"},
                        None,
                    )

            self.assertIsInstance(raised.exception.__cause__, SchemaError)
            self.assertIn("primary validation failure", str(raised.exception.__cause__))

    def test_repository_errors_returns_sorted_authoritative_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            (root / "tasks/z.md").write_text("bad\n", encoding="utf-8")
            (root / "goals/a.md").write_text("bad\n", encoding="utf-8")

            errors = Store(root).repository_errors()

            self.assertEqual(errors, sorted(validate_repository(root)))

    def test_safe_unlink_preserves_primary_fsync_error_when_close_also_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_repository(pathlib.Path(temporary_directory))
            target = root / "tasks/entity.md"
            target.write_bytes(b"content")
            real_close = os.close

            def close_then_fail(descriptor: int) -> None:
                real_close(descriptor)
                raise OSError("secondary close failure")

            with (
                mock.patch(
                    "webapp.store.os.fsync",
                    side_effect=OSError("primary fsync failure"),
                ),
                mock.patch("webapp.store.os.close", side_effect=close_then_fail),
            ):
                with self.assertRaisesRegex(OSError, "primary fsync failure"):
                    safe_unlink(target, root)


class AtomicWriteTest(unittest.TestCase):
    def test_creates_and_replaces_exact_bytes_and_fsyncs_file_and_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = pathlib.Path(temporary_directory)
            path = parent / "entity.md"
            real_fsync = os.fsync
            real_replace = os.replace
            synced_modes: list[int] = []
            events: list[str] = []

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                synced_modes.append(mode)
                events.append(
                    "file_fsync" if stat.S_ISREG(mode) else "parent_fsync"
                )
                real_fsync(descriptor)

            def record_replace(*args: object, **kwargs: object) -> None:
                events.append("replace")
                real_replace(*args, **kwargs)

            with (
                mock.patch("webapp.store.os.fsync", side_effect=record_fsync),
                mock.patch("webapp.store.os.replace", side_effect=record_replace),
            ):
                atomic_write(path, b"first\x00bytes")
                atomic_write(path, b"replacement\nbytes")

            self.assertEqual(path.read_bytes(), b"replacement\nbytes")
            self.assertEqual([entry.name for entry in parent.iterdir()], [path.name])
            self.assertEqual(sum(stat.S_ISREG(mode) for mode in synced_modes), 2)
            self.assertEqual(sum(stat.S_ISDIR(mode) for mode in synced_modes), 2)
            self.assertEqual(
                events,
                ["file_fsync", "replace", "parent_fsync"] * 2,
            )

    def test_file_fsync_failure_preserves_original_and_removes_temp_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = pathlib.Path(temporary_directory)
            path = parent / "entity.md"
            path.write_bytes(b"original")

            with mock.patch(
                "webapp.store.os.fsync", side_effect=OSError("simulated failure")
            ):
                with self.assertRaises(OSError):
                    atomic_write(path, b"replacement")

            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual([entry.name for entry in parent.iterdir()], [path.name])

    def test_parent_fsync_retries_on_the_same_secure_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = pathlib.Path(temporary_directory)
            path = parent / "entity.md"
            real_fsync = os.fsync
            directory_descriptors: list[int] = []

            def fail_first_parent_fsync(descriptor: int) -> None:
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    directory_descriptors.append(descriptor)
                    if len(directory_descriptors) == 1:
                        raise OSError("first parent fsync failed")
                real_fsync(descriptor)

            with mock.patch(
                "webapp.store.os.fsync", side_effect=fail_first_parent_fsync
            ):
                reported_error: BaseException | None = None
                try:
                    atomic_write(path, b"durable\n")
                except BaseException as error:
                    reported_error = error

            self.assertIsNone(reported_error)
            self.assertEqual(path.read_bytes(), b"durable\n")
            self.assertEqual(len(directory_descriptors), 2)
            self.assertEqual(
                directory_descriptors[0], directory_descriptors[1]
            )

    def test_parent_fsync_retry_failure_never_claims_durability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = pathlib.Path(temporary_directory)
            path = parent / "entity.md"
            path.write_bytes(b"original\n")
            real_fsync = os.fsync
            directory_descriptors: list[int] = []

            def fail_both_parent_fsyncs(descriptor: int) -> None:
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    directory_descriptors.append(descriptor)
                    raise OSError("parent durability unknown")
                real_fsync(descriptor)

            with mock.patch(
                "webapp.store.os.fsync", side_effect=fail_both_parent_fsyncs
            ):
                with self.assertRaisesRegex(OSError, "parent durability unknown"):
                    atomic_write(path, b"replacement\n")

            self.assertEqual(path.read_bytes(), b"replacement\n")
            self.assertEqual(len(directory_descriptors), 2)
            self.assertEqual(
                directory_descriptors[0], directory_descriptors[1]
            )

    def test_write_failure_preserves_original_and_removes_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = pathlib.Path(temporary_directory)
            path = parent / "entity.md"
            path.write_bytes(b"original")
            real_fdopen = os.fdopen

            class FailingWriter:
                def __init__(self, descriptor: int, mode: str) -> None:
                    self._file = real_fdopen(descriptor, mode)

                def __enter__(self) -> "FailingWriter":
                    self._file.__enter__()
                    return self

                def __exit__(
                    self,
                    exc_type: type[BaseException] | None,
                    exc_value: BaseException | None,
                    traceback: object,
                ) -> bool | None:
                    return self._file.__exit__(exc_type, exc_value, traceback)

                def write(self, data: bytes) -> int:
                    raise OSError("simulated write failure")

            with mock.patch(
                "webapp.store.os.fdopen",
                side_effect=lambda descriptor, mode: FailingWriter(
                    descriptor, mode
                ),
            ):
                with self.assertRaisesRegex(OSError, "simulated write failure"):
                    atomic_write(path, b"replacement")

            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual([entry.name for entry in parent.iterdir()], [path.name])

    def test_cleanup_failures_preserve_primary_error_and_attempt_all_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = pathlib.Path(temporary_directory)
            path = parent / "entity.md"
            path.write_bytes(b"original")
            real_mkstemp = tempfile.mkstemp
            real_close = os.close
            real_unlink = os.unlink
            temporary_descriptor = -1
            close_calls: list[int] = []
            unlink_calls: list[str] = []

            def tracked_mkstemp(
                *args: object, **kwargs: object
            ) -> tuple[int, str]:
                nonlocal temporary_descriptor
                temporary_descriptor, temporary_path = real_mkstemp(
                    *args, **kwargs
                )
                return temporary_descriptor, temporary_path

            def close_with_temp_failure(descriptor: int) -> None:
                close_calls.append(descriptor)
                real_close(descriptor)
                if descriptor == temporary_descriptor:
                    raise OSError("simulated temp close failure")

            def unlink_then_fail(
                target: str,
                *,
                dir_fd: int | None = None,
            ) -> None:
                unlink_calls.append(target)
                real_unlink(target, dir_fd=dir_fd)
                raise OSError("simulated unlink failure")

            with (
                mock.patch(
                    "webapp.store.tempfile.mkstemp", side_effect=tracked_mkstemp
                ),
                mock.patch(
                    "webapp.store.os.fdopen",
                    side_effect=OSError("primary fdopen failure"),
                ),
                mock.patch(
                    "webapp.store.os.close", side_effect=close_with_temp_failure
                ),
                mock.patch("webapp.store.os.unlink", side_effect=unlink_then_fail),
            ):
                with self.assertRaisesRegex(OSError, "primary fdopen failure"):
                    atomic_write(path, b"replacement")

            self.assertEqual(len(unlink_calls), 1)
            self.assertIn(temporary_descriptor, close_calls)
            self.assertTrue(
                any(descriptor != temporary_descriptor for descriptor in close_calls)
            )
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual([entry.name for entry in parent.iterdir()], [path.name])

    def test_refuses_symlink_destination_and_invalid_parents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = pathlib.Path(temporary_directory)
            outside = base / "outside.md"
            outside.write_bytes(b"outside")
            symlink = base / "destination.md"
            symlink.symlink_to(outside)
            parent_file = base / "parent-file"
            parent_file.write_bytes(b"not a directory")

            for path in (
                symlink,
                base / "missing" / "entity.md",
                parent_file / "entity.md",
            ):
                with self.subTest(path=path):
                    with self.assertRaises(InputError):
                        atomic_write(path, b"replacement")

            self.assertEqual(outside.read_bytes(), b"outside")

    def test_parent_replacement_during_temp_creation_is_refused_without_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = pathlib.Path(temporary_directory)
            parent = base / "entities"
            parent.mkdir()
            path = parent / "entity.md"
            path.write_bytes(b"original")
            moved_parent = base / "moved-entities"
            real_mkstemp = tempfile.mkstemp

            def replace_parent_before_mkstemp(
                *args: object, **kwargs: object
            ) -> tuple[int, str]:
                parent.rename(moved_parent)
                parent.mkdir()
                return real_mkstemp(*args, **kwargs)

            with mock.patch(
                "webapp.store.tempfile.mkstemp",
                side_effect=replace_parent_before_mkstemp,
            ):
                with self.assertRaises(InputError):
                    atomic_write(path, b"replacement")

            self.assertEqual((moved_parent / path.name).read_bytes(), b"original")
            self.assertEqual(list(parent.iterdir()), [])
            self.assertEqual(
                [entry.name for entry in moved_parent.iterdir()], [path.name]
            )


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "O_NOFOLLOW")
    and pathlib.Path("/proc/self/fd").is_dir(),
    "mutation locking requires Linux /proc and O_NOFOLLOW",
)
class StoreMutationSafetyTest(unittest.TestCase):
    def test_root_flock_survives_lock_path_replacement_during_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            context = multiprocessing.get_context("fork")
            first_entered = context.Event()
            release_first = context.Event()
            second_entered = context.Event()
            release_second = context.Event()
            attempting = context.Event()
            first = context.Process(
                target=_hold_store_mutation_lock,
                args=(str(root), first_entered, release_first),
            )
            second = context.Process(
                target=_hold_store_mutation_lock,
                args=(str(root), second_entered, release_second, attempting),
            )
            first.start()
            try:
                self.assertTrue(first_entered.wait(2.0))
                lock_path = root / ".webapp.lock"
                lock_path.rename(root / "original.lock")
                lock_path.write_bytes(b"replacement")
                second.start()
                self.assertTrue(attempting.wait(2.0))
                self.assertFalse(second_entered.wait(0.2))
                release_first.set()
                self.assertTrue(second_entered.wait(2.0))
                release_second.set()
                first.join(2.0)
                second.join(2.0)
            finally:
                release_first.set()
                release_second.set()
                for process in (first, second):
                    if process.pid is not None and process.is_alive():
                        process.terminate()
                    if process.pid is not None:
                        process.join(2.0)

            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)

    def test_lock_path_replaced_while_waiting_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            lock_path = root / ".webapp.lock"
            store = Store(root)
            context = multiprocessing.get_context("fork")
            holder_ready = context.Event()
            release_holder = context.Event()
            holder = context.Process(
                target=_hold_repository_lock,
                args=(str(lock_path), holder_ready, release_holder),
            )
            holder.start()
            contender_opened = threading.Event()
            contender_entered = threading.Event()
            errors: list[BaseException] = []
            real_open_lock_file = store._open_lock_file

            def tracked_open_lock_file() -> int:
                descriptor = real_open_lock_file()
                contender_opened.set()
                return descriptor

            def contend() -> None:
                try:
                    with store.mutation_lock():
                        contender_entered.set()
                except BaseException as error:
                    errors.append(error)

            contender = threading.Thread(target=contend)
            try:
                self.assertTrue(holder_ready.wait(2.0))
                with mock.patch.object(
                    store, "_open_lock_file", side_effect=tracked_open_lock_file
                ):
                    contender.start()
                    self.assertTrue(contender_opened.wait(1.0))
                    lock_path.rename(root / "original.lock")
                    lock_path.write_bytes(b"replacement")
                    release_holder.set()
                    contender.join(2.0)
            finally:
                release_holder.set()
                holder.join(2.0)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(2.0)
                if contender.is_alive():
                    contender.join(2.0)

            self.assertFalse(contender.is_alive())
            self.assertFalse(contender_entered.is_set())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], InputError)

    def test_separate_process_holder_causes_timeout_then_release_allows_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            store = Store(root)
            context = multiprocessing.get_context("fork")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_repository_lock,
                args=(str(root / ".webapp.lock"), ready, release),
            )
            process.start()
            try:
                self.assertTrue(ready.wait(2.0))
                started = time.monotonic()
                with (
                    mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.15),
                    mock.patch("webapp.store.LOCK_RETRY_INTERVAL_SECONDS", 0.01),
                ):
                    with self.assertRaises(StoreLockTimeout):
                        with store.mutation_lock():
                            self.fail("contender entered while child held flock")
                self.assertGreaterEqual(time.monotonic() - started, 0.12)
            finally:
                release.set()
                process.join(2.0)
                if process.is_alive():
                    process.terminate()
                    process.join(2.0)

            self.assertEqual(process.exitcode, 0)
            with store.mutation_lock():
                pass

    def test_two_threads_cannot_enter_lock_body_simultaneously(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()
            errors: list[BaseException] = []

            def first_worker() -> None:
                try:
                    with store.mutation_lock():
                        first_entered.set()
                        release_first.wait(2.0)
                except BaseException as error:
                    errors.append(error)

            def second_worker() -> None:
                try:
                    with store.mutation_lock():
                        second_entered.set()
                except BaseException as error:
                    errors.append(error)

            first = threading.Thread(target=first_worker)
            second = threading.Thread(target=second_worker)
            first.start()
            self.assertTrue(first_entered.wait(1.0))
            second.start()
            self.assertFalse(second_entered.wait(0.1))
            release_first.set()
            first.join(2.0)
            second.join(2.0)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_entered.is_set())
            self.assertEqual(errors, [])

    def test_exception_releases_both_lock_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))

            with self.assertRaisesRegex(RuntimeError, "inside lock"):
                with store.mutation_lock():
                    raise RuntimeError("inside lock")

            with store.mutation_lock():
                pass

    def test_interrupted_flock_honors_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))
            real_flock = fcntl.flock
            interrupted = False

            def interrupt_once(descriptor: int, operation: int) -> None:
                nonlocal interrupted
                if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not interrupted:
                    interrupted = True
                    raise InterruptedError("simulated signal")
                real_flock(descriptor, operation)

            with (
                mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.0),
                mock.patch("webapp.store.fcntl.flock", side_effect=interrupt_once),
            ):
                with self.assertRaises(StoreLockTimeout):
                    with store.mutation_lock():
                        self.fail("interrupted acquisition ignored its deadline")

    def test_interrupted_flock_uses_retry_sleep_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))
            real_flock = fcntl.flock
            real_sleep = time.sleep
            interrupted = False
            sleep_calls: list[float] = []

            def interrupt_once(descriptor: int, operation: int) -> None:
                nonlocal interrupted
                if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not interrupted:
                    interrupted = True
                    raise InterruptedError("simulated signal")
                real_flock(descriptor, operation)

            def record_sleep(seconds: float) -> None:
                sleep_calls.append(seconds)
                real_sleep(seconds)

            with (
                mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.2),
                mock.patch("webapp.store.LOCK_RETRY_INTERVAL_SECONDS", 0.01),
                mock.patch("webapp.store.fcntl.flock", side_effect=interrupt_once),
                mock.patch("webapp.store.time.sleep", side_effect=record_sleep),
                store.mutation_lock(),
            ):
                pass

            self.assertEqual(len(sleep_calls), 1)
            self.assertGreater(sleep_calls[0], 0.0)
            self.assertLessEqual(sleep_calls[0], 0.01)

    def test_unlock_failure_still_closes_fd_and_releases_thread_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))
            real_flock = fcntl.flock

            def fail_unlock(descriptor: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    raise OSError("simulated unlock failure")
                real_flock(descriptor, operation)

            with mock.patch("webapp.store.fcntl.flock", side_effect=fail_unlock):
                with self.assertRaisesRegex(OSError, "simulated unlock failure"):
                    with store.mutation_lock():
                        pass

            with (
                mock.patch("webapp.store.LOCK_TIMEOUT_SECONDS", 0.1),
                store.mutation_lock(),
            ):
                pass

    def test_symlink_lock_file_is_refused_and_thread_lock_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            outside = root / "outside.lock"
            outside.write_bytes(b"unchanged")
            lock_path = root / ".webapp.lock"
            lock_path.symlink_to(outside)
            store = Store(root)

            with self.assertRaises(InputError):
                with store.mutation_lock():
                    self.fail("symlink lock entered")

            lock_path.unlink()
            with store.mutation_lock():
                pass
            self.assertEqual(outside.read_bytes(), b"unchanged")

    def test_require_current_returns_match_and_conflict_carries_latest_entity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            path = root / "tasks/entity.md"
            entity_id = "task-20260717-001"
            StoreTest.write(
                path,
                StoreTest.document(entity_id, "task", "Original\n", status="next"),
            )
            store = Store(root)
            original = store.get_entity(entity_id)

            with store.mutation_lock():
                self.assertEqual(
                    store.require_current(entity_id, original.content_hash), original
                )

            StoreTest.write(
                path,
                StoreTest.document(entity_id, "task", "Latest\n", status="next"),
            )
            with store.mutation_lock():
                with self.assertRaises(ConflictError) as raised:
                    store.require_current(entity_id, original.content_hash)

            self.assertEqual(raised.exception.current.body, "Latest\n")
            self.assertNotEqual(
                raised.exception.current.content_hash, original.content_hash
            )

    def test_require_current_rejects_malformed_hash_and_preserves_not_found(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(pathlib.Path(temporary_directory))

            for base_hash in (
                "a" * 63,
                "A" * 64,
                "g" * 64,
                "0" * 64 + "0",
            ):
                with self.subTest(base_hash=base_hash):
                    with self.assertRaises(InputError):
                        store.require_current("task-20260717-001", base_hash)

            with self.assertRaises(NotFoundError):
                store.require_current("task-20260717-999", "0" * 64)


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "O_NOFOLLOW")
    and pathlib.Path("/proc/self/fd").is_dir(),
    "secure mutations require Linux /proc and O_NOFOLLOW",
)
class StoreTaskWorkflowTest(unittest.TestCase):
    NOW = datetime.datetime(
        2026, 7, 19, 9, 8, 7,
        tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
    )

    def make_repository(self, base: pathlib.Path) -> pathlib.Path:
        root = base / "repository"
        for directory in (
            "inbox", "tasks", "purposes", "visions", "areas",
            "projects", "goals", "reviews/daily",
            "reviews/weekly", "archive",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        return root

    def create_task(
        self,
        store: Store,
        title: str,
        status: str,
        **fields: str,
    ):
        with mock.patch("webapp.store.current_time", return_value=self.NOW):
            return store.create_entity(
                "task", {"title": title, "status": status, **fields}, "Task body"
            )

    def create_legacy_task(
        self,
        store: Store,
        title: str,
        status: str,
        **fields: str,
    ):
        """Seed a pre-Release-A Calendar Task without using generic writes."""
        entity_id = store.next_entity_id("task")
        frontmatter = {
            "id": entity_id,
            "type": "task",
            "title": title,
            "status": status,
            "created_at": self.NOW.isoformat(),
            "updated_at": self.NOW.isoformat(),
            **fields,
        }
        relative_dir = "inbox" if status == "inbox" else "tasks"
        StoreTest.write(
            store._root / relative_dir / f"{entity_id}.md",
            serialize_frontmatter(frontmatter, "task") + "Task body",
        )
        return store.get_entity(entity_id)

    def create_project(self, store: Store, title: str = "Project"):
        with mock.patch("webapp.store.current_time", return_value=self.NOW):
            return store.create_entity("project", {"title": title}, "Project body")

    def test_task_start_preview_projects_cached_documents_without_temp_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            store.repository_document_map()
            validation_calls = 0

            def count_document_validation(documents):
                nonlocal validation_calls
                validation_calls += 1
                return validate_document_map(documents)

            with (
                mock.patch(
                    "webapp.store.tempfile.TemporaryDirectory",
                    side_effect=AssertionError("Task preview must not materialize a tree"),
                ),
                mock.patch(
                    "webapp.store.validate_repository",
                    side_effect=AssertionError("Task preview must use document-map validation"),
                ),
                mock.patch(
                    "webapp.store.validate_document_map",
                    side_effect=count_document_validation,
                ),
                mock.patch("webapp.store.current_time", return_value=self.NOW),
            ):
                plan = store.plan_task_start(target.entity_id, target.content_hash)

            self.assertEqual([effect.role for effect in plan.effects], ["started"])
            self.assertEqual(validation_calls, 1)

    def test_explicit_switch_boundary_preserves_processing_metadata_time(self) -> None:
        """Catches an event boundary leaking into updated/project metadata."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            active = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-18T22:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(
                store, "Target", "next", project_id=project.entity_id
            )
            event_timestamp = "2026-07-19T07:45:30+09:00"

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                    active_work_ended_at=event_timestamp,
                )

            by_role = {effect.role: effect.planned_entity for effect in plan.effects}
            self.assertEqual(
                by_role["completed"].frontmatter["work_ended_at"], event_timestamp
            )
            self.assertEqual(
                by_role["started"].frontmatter["work_started_at"], event_timestamp
            )
            for role in ("completed", "project_started", "started"):
                self.assertEqual(
                    by_role[role].frontmatter["updated_at"],
                    self.NOW.isoformat(timespec="seconds"),
                )
            self.assertEqual(
                dict(plan.operation_inputs)["timestamp"],
                self.NOW.isoformat(timespec="seconds"),
            )
            self.assertEqual(
                dict(plan.operation_inputs)["work_event_timestamp"], event_timestamp
            )

    def test_interrupt_create_and_start_uses_now_for_continuation_identity(self) -> None:
        """Catches a historical work boundary backdating the continuation record."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-18T22:00:00+09:00",
                resume_status="next",
            )
            event_timestamp = "2026-07-18T23:30:00+09:00"

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_create_and_start(
                    "Fresh",
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="interrupt",
                    active_work_ended_at=event_timestamp,
                )

            by_role = {effect.role: effect.planned_entity for effect in plan.effects}
            continuation = by_role["continuation"]
            self.assertTrue(continuation.entity_id.startswith("task-20260719-"))
            self.assertEqual(continuation.frontmatter["action_date"], "2026-07-19")
            self.assertEqual(
                continuation.frontmatter["created_at"],
                self.NOW.isoformat(timespec="seconds"),
            )
            self.assertEqual(
                by_role["started"].frontmatter["work_started_at"], event_timestamp
            )

    def test_task_start_apply_refreshes_and_validates_cached_documents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(target.entity_id, target.content_hash)
            real_document_snapshot = store._repository_document_snapshot
            refresh_calls = 0

            def count_refresh():
                nonlocal refresh_calls
                refresh_calls += 1
                return real_document_snapshot()

            with (
                mock.patch.object(
                    store, "_repository_document_snapshot", side_effect=count_refresh
                ),
                mock.patch(
                    "webapp.store.validate_repository",
                    side_effect=AssertionError("Task apply must use document-map validation"),
                ),
            ):
                results = store.apply_task_workflow(plan)

            self.assertEqual(results, (plan.effects[0].planned_entity,))
            self.assertGreaterEqual(refresh_calls, 2)

    def test_successful_apply_carries_projected_validation_to_next_preview(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            first = self.create_task(store, "First", "next")
            second = self.create_task(store, "Second", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                first_plan = store.plan_task_start(
                    first.entity_id, first.content_hash
                )
            started = store.apply_task_workflow(first_plan)[0]
            validation_calls = 0
            real_validate = validate_document_map

            def count_validation(documents):
                nonlocal validation_calls
                validation_calls += 1
                return real_validate(documents)

            with (
                mock.patch(
                    "webapp.store.validate_document_map",
                    side_effect=count_validation,
                ),
                mock.patch("webapp.store.current_time", return_value=self.NOW),
            ):
                store.plan_task_start(
                    second.entity_id,
                    second.content_hash,
                    active_entity_id=started.entity_id,
                    active_base_hash=started.content_hash,
                    resolution="complete",
                )

            self.assertEqual(validation_calls, 0)

    def test_validated_clean_baseline_uses_bounded_fast_projection_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            self.assertEqual(store.repository_errors(), [])

            with (
                mock.patch(
                    "webapp.store.validate_document_map",
                    side_effect=AssertionError(
                        "clean fast projection must not reparse the repository"
                    ),
                ),
                mock.patch("webapp.store.current_time", return_value=self.NOW),
            ):
                plan = store.plan_task_start(
                    target.entity_id, target.content_hash
                )

            self.assertEqual([effect.role for effect in plan.effects], ["started"])

    def test_fast_task_workflows_accept_archived_tasks_linked_to_closed_project(
        self,
    ) -> None:
        cases = (
            (action, project_status, archived_status)
            for action in ("start", "complete", "interrupt", "create_and_start")
            for project_status in ("completed", "dropped")
            for archived_status in ("planned", "doing")
        )
        for action, project_status, archived_status in cases:
            with self.subTest(action=action, project_status=project_status, archived_status=archived_status), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                project = self.create_project(store)
                archived_task = self.create_task(
                    store, "Historic", "planned", project_id=project.entity_id
                )
                archived_path = root / "archive" / pathlib.Path(archived_task.relative_path).name
                (root / archived_task.relative_path).rename(archived_path)
                if archived_status == "doing":
                    archived_fields = dict(archived_task.frontmatter)
                    archived_fields.update({
                        "status": "doing",
                        "work_started_at": "2026-07-19T08:00:00+09:00",
                        "resume_status": "next",
                    })
                    StoreTest.write(
                        archived_path,
                        serialize_frontmatter(archived_fields, "task") + archived_task.body,
                    )
                project_fields = dict(project.frontmatter)
                project_fields["status"] = project_status
                StoreTest.write(
                    root / project.relative_path,
                    serialize_frontmatter(project_fields, "project") + project.body,
                )
                store = Store(root)
                task = None
                if action == "start":
                    task = self.create_task(store, "Target", "next")
                elif action in {"complete", "interrupt"}:
                    task = self.create_task(
                        store,
                        "Current",
                        "doing",
                        work_started_at="2026-07-19T08:00:00+09:00",
                        resume_status="next",
                    )
                self.assertEqual(validate_repository(root), [])

                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    if action == "start":
                        plan = store.plan_task_start(task.entity_id, task.content_hash)
                    elif action == "complete":
                        plan = store.plan_task_complete(task.entity_id, task.content_hash)
                    elif action == "interrupt":
                        plan = store.plan_task_interrupt(task.entity_id, task.content_hash)
                    else:
                        plan = store.plan_task_create_and_start("Fresh")
                    store.apply_task_workflow(plan)

                self.assertEqual(validate_repository(root), [])

    def test_fast_task_start_still_rejects_active_task_of_completed_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            target = self.create_task(
                store, "Target", "next", project_id=project.entity_id
            )
            project_fields = dict(project.frontmatter)
            project_fields["status"] = "completed"
            StoreTest.write(
                root / project.relative_path,
                serialize_frontmatter(project_fields, "project") + project.body,
            )
            store = Store(root)
            self.assertEqual(validate_repository(root), [])

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaisesRegex(ProjectOperationError, "project_not_executable"):
                    store.plan_task_start(target.entity_id, target.content_hash)
                with self.assertRaisesRegex(SchemaError, "project_id is not executable"):
                    store.plan_task_create_and_start("Fresh")

    def test_external_edit_invalidates_carried_validation_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(store, "Active", "next")
            unrelated = self.create_task(store, "Before", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(active.entity_id, active.content_hash)
            started = store.apply_task_workflow(plan)[0]
            unrelated_path = root / unrelated.relative_path
            unrelated_path.write_bytes(
                unrelated_path.read_bytes().replace(b"Before", b"After!")
            )
            store.get_entity(unrelated.entity_id)
            validation_calls = 0
            real_validate = validate_document_map

            def count_validation(documents):
                nonlocal validation_calls
                validation_calls += 1
                return real_validate(documents)

            with (
                mock.patch(
                    "webapp.store.validate_document_map",
                    side_effect=count_validation,
                ),
                mock.patch("webapp.store.current_time", return_value=self.NOW),
            ):
                store.plan_task_start(started.entity_id, started.content_hash)

            self.assertEqual(validation_calls, 1)

    def test_start_preview_reads_each_entity_only_a_constant_number_of_times(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            timestamp = self.NOW.isoformat()

            def write_task(
                entity_id: str, title: str, status: str, **fields: str
            ) -> None:
                frontmatter = {
                    "id": entity_id,
                    "type": "task",
                    "title": title,
                    "status": status,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    **fields,
                }
                StoreTest.write(
                    root / "tasks" / f"{entity_id}.md",
                    serialize_frontmatter(frontmatter, "task") + "Task body",
                )

            write_task(
                "task-active-indexed-preview",
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            write_task("task-target-indexed-preview", "Target", "next")
            for index in range(30):
                write_task(
                    f"task-dependent-indexed-preview-{index:03d}",
                    f"Dependent {index}",
                    "waiting",
                    depends_on="[task-active-indexed-preview]",
                )

            active = store.get_entity("task-active-indexed-preview")
            target = store.get_entity("task-target-indexed-preview")
            entity_count = len(store.list_entities())
            read_count = 0
            real_read_entity = store._read_entity

            def count_read_entity(path: pathlib.Path):
                nonlocal read_count
                read_count += 1
                return real_read_entity(path)

            with (
                mock.patch.object(
                    store, "_read_entity", side_effect=count_read_entity
                ),
                mock.patch("webapp.store.current_time", return_value=self.NOW),
            ):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                )

            self.assertEqual(
                [effect.role for effect in plan.effects].count(
                    "dependency_released"
                ),
                30,
            )
            self.assertLessEqual(read_count, entity_count * 4)

    def test_interrupting_start_preview_reads_each_entity_only_a_constant_number_of_times(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            timestamp = self.NOW.isoformat()

            def write_task(
                entity_id: str, title: str, status: str, **fields: str
            ) -> None:
                frontmatter = {
                    "id": entity_id,
                    "type": "task",
                    "title": title,
                    "status": status,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    **fields,
                }
                StoreTest.write(
                    root / "tasks" / f"{entity_id}.md",
                    serialize_frontmatter(frontmatter, "task") + "Task body",
                )

            write_task(
                "task-active-interrupt-indexed-preview",
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            write_task(
                "task-target-interrupt-indexed-preview", "Target", "next"
            )
            for index in range(30):
                write_task(
                    f"task-dependent-interrupt-indexed-preview-{index:03d}",
                    f"Dependent {index}",
                    "waiting",
                    depends_on="[task-active-interrupt-indexed-preview]",
                )

            active = store.get_entity(
                "task-active-interrupt-indexed-preview"
            )
            target = store.get_entity(
                "task-target-interrupt-indexed-preview"
            )
            entity_count = len(store.list_entities())
            read_count = 0
            real_read_entity = store._read_entity

            def count_read_entity(path: pathlib.Path):
                nonlocal read_count
                read_count += 1
                return real_read_entity(path)

            with (
                mock.patch.object(
                    store, "_read_entity", side_effect=count_read_entity
                ),
                mock.patch("webapp.store.current_time", return_value=self.NOW),
            ):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="interrupt",
                )

            self.assertEqual(
                [effect.role for effect in plan.effects].count(
                    "dependency_retargeted"
                ),
                30,
            )
            self.assertLessEqual(read_count, entity_count * 4)

    def test_indexed_start_preserves_dependency_lookup_fail_closed_contracts(
        self,
    ) -> None:
        cases = (
            ("unknown", "unknown depends_on Task"),
            ("archived_unfinished", "archived unfinished Tasks"),
            ("duplicate", "duplicate entity id"),
        )
        for case, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                timestamp = self.NOW.isoformat()

                def write_task(path: pathlib.Path, entity_id: str, status: str) -> None:
                    frontmatter = {
                        "id": entity_id,
                        "type": "task",
                        "title": entity_id,
                        "status": status,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                    StoreTest.write(
                        path,
                        serialize_frontmatter(frontmatter, "task") + "Task body",
                    )

                dependency_id = "task-indexed-dependency"
                if case == "archived_unfinished":
                    write_task(
                        root / "archive/dependency.md",
                        dependency_id,
                        "next",
                    )
                elif case == "duplicate":
                    write_task(
                        root / "tasks/dependency.md",
                        dependency_id,
                        "done",
                    )
                    write_task(
                        root / "archive/dependency.md",
                        dependency_id,
                        "done",
                    )

                target_id = "task-indexed-target"
                target_frontmatter = {
                    "id": target_id,
                    "type": "task",
                    "title": "Target",
                    "status": "next",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "depends_on": f"[{dependency_id}]",
                }
                StoreTest.write(
                    root / f"tasks/{target_id}.md",
                    serialize_frontmatter(target_frontmatter, "task") + "Task body",
                )
                target = store.get_entity(target_id)

                with self.assertRaisesRegex(InputError, expected):
                    store.plan_task_start(target.entity_id, target.content_hash)

    def test_project_task_tree_create_builds_parallel_roots_and_planned_descendants(self) -> None:
        """Catches tree mode collapsing a forest or representing descendants as Waiting."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_plan_create(
                    project.entity_id,
                    project.content_hash,
                    [
                        {"key": "root-a", "title": "Root A", "parent_key": None},
                        {"key": "child", "title": "Child", "parent_key": "root-a"},
                        {"key": "root-b", "title": "Root B", "parent_key": None},
                    ],
                    mode="tree",
                )

            proposed = [effect.planned_entity.frontmatter for effect in plan.effects]
            self.assertEqual([item["title"] for item in proposed], ["Root A", "Child", "Root B"])
            self.assertEqual([item["status"] for item in proposed], ["next", "planned", "next"])
            self.assertEqual([item["project_position"] for item in proposed], ["1", "1", "2"])
            self.assertNotIn("depends_on", proposed[0])
            self.assertEqual(proposed[1]["depends_on"], f"[{proposed[0]['id']}]")
            self.assertNotIn("depends_on", proposed[2])

            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_project_task_tree_create_keeps_an_explicit_planned_root_and_child_planned(self) -> None:
        """Catches Project Support roots losing their explicit planned state."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_plan_create(
                    project.entity_id,
                    project.content_hash,
                    [
                        {"key": "root", "title": "Root", "parent_key": None, "status": "planned"},
                        {"key": "child", "title": "Child", "parent_key": "root"},
                    ],
                    mode="tree",
                )

            proposed = [effect.planned_entity.frontmatter for effect in plan.effects]
            self.assertEqual([item["status"] for item in proposed], ["planned", "planned"])
            self.assertNotIn("depends_on", proposed[0])
            self.assertEqual(proposed[1]["depends_on"], f"[{proposed[0]['id']}]")

    def test_project_task_tree_create_keeps_an_explicit_next_root_and_child_planned(self) -> None:
        """Catches Store rejecting the explicit Next root allowed by the tree contract."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_plan_create(
                    project.entity_id,
                    project.content_hash,
                    [
                        {"key": "root", "title": "Root", "parent_key": None, "status": "next"},
                        {"key": "child", "title": "Child", "parent_key": "root"},
                    ],
                    mode="tree",
                )

            proposed = [effect.planned_entity.frontmatter for effect in plan.effects]
            self.assertEqual([item["status"] for item in proposed], ["next", "planned"])

    def test_project_task_plan_update_atomically_updates_creates_and_archives(self) -> None:
        """Catches the full-tree editor splitting one save into partial mutations."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            first = self.create_task(
                store,
                "First",
                "next",
                project_id=project.entity_id,
                project_position="1",
                action_date="2026-07-19",
            )
            archived = self.create_task(
                store,
                "Archive me",
                "planned",
                project_id=project.entity_id,
                project_position="2",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_plan_update(
                    project.entity_id,
                    project.content_hash,
                    [
                        {
                            "key": first.entity_id,
                            "id": first.entity_id,
                            "base_hash": first.content_hash,
                            "title": "First renamed",
                            "parent_key": None,
                            "status": "planned",
                        },
                        {
                            "key": "client-new",
                            "title": "New child",
                            "parent_key": first.entity_id,
                            "status": "planned",
                        },
                    ],
                    [{"id": archived.entity_id, "base_hash": archived.content_hash}],
                )

            self.assertEqual(plan.action, "project_task_plan_update")
            self.assertEqual(
                [effect.role for effect in plan.effects],
                ["task_updated", "task_created", "task_archived"],
            )
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            real_publish = store._publish_no_replace
            publish_calls = 0

            def fail_second_publish(*args, **kwargs):
                nonlocal publish_calls
                publish_calls += 1
                if publish_calls == 2:
                    raise OSError("Project Task plan publish failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(
                store, "_publish_no_replace", side_effect=fail_second_publish
            ):
                with self.assertRaisesRegex(OSError, "plan publish failure"):
                    store.apply_task_workflow(plan)
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                before,
            )
            self.assertFalse(store.mutation_recovery_required())

            results = store.apply_task_workflow(plan)
            self.assertEqual(results[0].frontmatter["title"], "First renamed")
            self.assertEqual(results[0].frontmatter["status"], "planned")
            self.assertEqual(results[0].frontmatter["project_position"], "1")
            self.assertNotIn("action_date", results[0].frontmatter)
            self.assertEqual(
                results[1].frontmatter["depends_on"],
                f"[{first.entity_id}]",
            )
            self.assertEqual(results[1].frontmatter["project_position"], "1")
            self.assertTrue(results[2].relative_path.startswith("archive/"))
            self.assertEqual(validate_repository(root), [])

    def test_project_task_plan_update_promotes_archived_children_and_preserves_secondary_dependencies(self) -> None:
        """Catches archive promotion dropping later prerequisite dependencies."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            prerequisite = self.create_task(
                store, "External prerequisite", "done", project_id=project.entity_id
            )
            root_task = self.create_task(
                store, "Root", "next", project_id=project.entity_id,
                project_position="1",
            )
            parent = self.create_task(
                store, "Parent", "planned", project_id=project.entity_id,
                depends_on=f"[{root_task.entity_id}]", project_position="1",
            )
            child = self.create_task(
                store, "Child", "planned", project_id=project.entity_id,
                depends_on=f"[{parent.entity_id}, {prerequisite.entity_id}]",
                project_position="1",
            )

            plan = store.plan_project_task_plan_update(
                project.entity_id,
                project.content_hash,
                [
                    {"key": root_task.entity_id, "id": root_task.entity_id,
                     "base_hash": root_task.content_hash, "title": "Root",
                     "parent_key": None, "status": "next"},
                    {"key": child.entity_id, "id": child.entity_id,
                     "base_hash": child.content_hash, "title": "Child",
                     "parent_key": root_task.entity_id, "status": "planned"},
                ],
                [{"id": parent.entity_id, "base_hash": parent.content_hash}],
            )

            self.assertEqual(
                [effect.role for effect in plan.effects],
                ["task_updated", "task_archived"],
            )
            store.apply_task_workflow(plan)
            promoted = store.get_entity(child.entity_id)
            self.assertEqual(
                promoted.frontmatter["depends_on"],
                f"[{root_task.entity_id}, {prerequisite.entity_id}]",
            )
            self.assertEqual(promoted.frontmatter["project_position"], "1")


    def test_task_archive_reconnects_direct_child_to_parent_and_preserves_secondary_fields(self) -> None:
        """Catches middle-node archive disconnecting its direct Project-tree child."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            ancestor = self.create_task(
                store, "Ancestor", "next", project_id=project.entity_id,
                project_position="1",
            )
            archived = self.create_task(
                store, "Middle", "planned", project_id=project.entity_id,
                depends_on=f"[{ancestor.entity_id}]", project_position="1",
            )
            prerequisite = self.create_task(store, "Prerequisite", "done")
            child = self.create_task(
                store, "Child", "planned", project_id=project.entity_id,
                depends_on=f"[{archived.entity_id}, {ancestor.entity_id}, {prerequisite.entity_id}]",
                project_position="1",
            )
            child = store.update_entity(
                child.entity_id, child.content_hash, {}, "Preserved body\n"
            )
            other_project = self.create_project(store, "Other Project")
            outside = self.create_task(
                store, "Outside", "waiting", project_id=other_project.entity_id,
                depends_on=f"[{archived.entity_id}]",
            )
            other_gate = self.create_task(store, "Other gate", "next")
            secondary = self.create_task(
                store, "Secondary", "waiting", project_id=project.entity_id,
                depends_on=f"[{other_gate.entity_id}, {archived.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )

            self.assertIsInstance(plan, WorkflowPlan)
            assert isinstance(plan, WorkflowPlan)
            self.assertEqual(plan.action, "archive")
            self.assertEqual(plan.effects[0].role, "task_archived")
            self.assertEqual(plan.effects[0].entity_id, archived.entity_id)
            self.assertEqual(
                [effect.entity_id for effect in plan.effects[1:]],
                sorted([child.entity_id, outside.entity_id, secondary.entity_id]),
            )
            by_id = {effect.entity_id: effect for effect in plan.effects}
            proposed = by_id[child.entity_id].planned_entity
            self.assertEqual(
                proposed.frontmatter["depends_on"],
                f"[{ancestor.entity_id}, {prerequisite.entity_id}]",
            )
            self.assertEqual(proposed.frontmatter["status"], "planned")
            self.assertEqual(proposed.body, child.body)
            outside_effect = by_id[outside.entity_id]
            self.assertEqual(outside_effect.role, "dependency_released")
            self.assertNotIn(
                "depends_on", outside_effect.planned_entity.frontmatter
            )
            secondary_effect = by_id[secondary.entity_id]
            self.assertEqual(secondary_effect.role, "dependency_retargeted")
            self.assertEqual(
                secondary_effect.planned_entity.frontmatter["depends_on"],
                f"[{other_gate.entity_id}]",
            )

            store.apply_task_workflow(plan)

            self.assertTrue(
                store.get_entity(archived.entity_id).relative_path.startswith("archive/")
            )
            self.assertEqual(
                store.get_entity(child.entity_id).frontmatter["depends_on"],
                f"[{ancestor.entity_id}, {prerequisite.entity_id}]",
            )
            self.assertEqual(validate_repository(root), [])

    def test_task_archive_promotes_root_children_and_only_removes_nonstructural_references(self) -> None:
        """Catches root archive inheriting a parent across Projects or dropping siblings."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            other_project = self.create_project(store, "Other")
            archived = self.create_task(
                store, "Root", "next", project_id=project.entity_id,
                project_position="1",
            )
            first = self.create_task(
                store, "First", "planned", project_id=project.entity_id,
                depends_on=f"[{archived.entity_id}]", project_position="1",
            )
            second = self.create_task(
                store, "Second", "planned", project_id=project.entity_id,
                depends_on=f"[{archived.entity_id}]", project_position="2",
            )
            outside = self.create_task(
                store, "Outside", "waiting", project_id=other_project.entity_id,
                depends_on=f"[{archived.entity_id}]",
            )
            other_gate = self.create_task(store, "Other gate", "next")
            secondary = self.create_task(
                store, "Secondary", "waiting", project_id=project.entity_id,
                depends_on=f"[{other_gate.entity_id}, {archived.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )

            self.assertIsInstance(plan, WorkflowPlan)
            assert isinstance(plan, WorkflowPlan)
            by_id = {effect.entity_id: effect for effect in plan.effects}
            self.assertEqual(
                [effect.entity_id for effect in plan.effects[1:]],
                sorted([first.entity_id, second.entity_id, outside.entity_id, secondary.entity_id]),
            )
            for promoted in (first, second, outside):
                effect = by_id[promoted.entity_id]
                self.assertEqual(effect.role, "dependency_released")
                self.assertEqual(effect.planned_entity.frontmatter["status"], "next")
                self.assertNotIn("depends_on", effect.planned_entity.frontmatter)
            self.assertEqual(
                by_id[first.entity_id].planned_entity.frontmatter["project_position"],
                "1",
            )
            self.assertEqual(
                by_id[second.entity_id].planned_entity.frontmatter["project_position"],
                "2",
            )
            secondary_effect = by_id[secondary.entity_id]
            self.assertEqual(secondary_effect.role, "dependency_retargeted")
            self.assertEqual(
                secondary_effect.planned_entity.frontmatter["depends_on"],
                f"[{other_gate.entity_id}]",
            )
            self.assertEqual(
                secondary_effect.planned_entity.frontmatter["status"], "waiting"
            )

            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_task_archive_applies_valid_today_child_rewrite_as_one_final_state(self) -> None:
        """Catches archive validation observing the target before its child rewrite."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            archive_time = datetime.datetime(
                2026,
                9,
                3,
                10,
                0,
                tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
            )
            with mock.patch("webapp.store.current_time", return_value=archive_time):
                project = self.create_project(store)
            for entity_id, title, status, position, dependencies in (
                ("task-qa-r", "Root", "next", "2", None),
                ("task-qa-w", "Waiting", "waiting", "1", "[task-qa-r]"),
            ):
                frontmatter = {
                    "id": entity_id,
                    "type": "task",
                    "title": title,
                    "status": status,
                    "created_at": archive_time.isoformat(),
                    "updated_at": archive_time.isoformat(),
                    "project_id": project.entity_id,
                    "project_position": position,
                }
                if dependencies is not None:
                    frontmatter["depends_on"] = dependencies
                    frontmatter["action_date"] = "2026-09-03"
                StoreTest.write(
                    root / "tasks" / f"{entity_id}.md",
                    serialize_frontmatter(frontmatter, "task") + "Task body",
                )
            archived = store.get_entity("task-qa-r")
            dependent = store.get_entity("task-qa-w")
            self.assertEqual(validate_repository(root), [])
            self.assertEqual(store.repository_errors(), [])

            with mock.patch("webapp.store.current_time", return_value=archive_time):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )
                assert isinstance(plan, WorkflowPlan)
                results = store.apply_task_workflow(plan)

            self.assertEqual(
                [result.entity_id for result in results],
                [archived.entity_id, dependent.entity_id],
            )
            rewritten = store.get_entity(dependent.entity_id)
            self.assertEqual(rewritten.frontmatter["status"], "next")
            self.assertNotIn("depends_on", rewritten.frontmatter)
            self.assertEqual(rewritten.frontmatter["action_date"], "2026-09-03")
            self.assertEqual(validate_repository(root), [])

    def test_task_archive_keeps_status_when_an_availability_or_human_gate_remains(self) -> None:
        """Catches dependency removal bypassing remaining automatic or human gates."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            archived = self.create_task(store, "Archived", "next")
            future = self.create_task(
                store, "Future", "waiting",
                depends_on=f"[{archived.entity_id}]", available_from="2026-07-20",
            )
            human = self.create_task(
                store, "Human", "waiting",
                depends_on=f"[{archived.entity_id}]", waiting_for="reply",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )

            assert isinstance(plan, WorkflowPlan)
            by_id = {effect.entity_id: effect for effect in plan.effects}
            for blocked in (future, human):
                effect = by_id[blocked.entity_id]
                self.assertEqual(effect.role, "dependency_retargeted")
                self.assertEqual(effect.planned_entity.frontmatter["status"], "waiting")
                self.assertNotIn("depends_on", effect.planned_entity.frontmatter)

    def test_task_archive_rejects_dependent_set_drift_before_any_effect(self) -> None:
        """Catches a new post-preview reference escaping the atomic archive workflow."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            archived = self.create_task(store, "Archived", "next")
            first = self.create_task(
                store, "First", "waiting", depends_on=f"[{archived.entity_id}]"
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )
            late = self.create_task(
                store, "Late", "waiting", depends_on=f"[{archived.entity_id}]"
            )

            with self.assertRaisesRegex(
                MutationPlanConflict, "dependency references changed"
            ):
                assert isinstance(plan, WorkflowPlan)
                store.apply_task_workflow(plan)

            self.assertFalse(
                store.get_entity(archived.entity_id).relative_path.startswith("archive/")
            )
            self.assertEqual(
                store.get_entity(first.entity_id).frontmatter["depends_on"],
                f"[{archived.entity_id}]",
            )
            self.assertEqual(
                store.get_entity(late.entity_id).frontmatter["depends_on"],
                f"[{archived.entity_id}]",
            )
            self.assertFalse(store.mutation_recovery_required())

    def test_task_archive_rejects_dependent_hash_drift_before_any_effect(self) -> None:
        """Catches a post-preview dependent edit escaping the fixed workflow snapshot."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            archived = self.create_task(store, "Archived", "next")
            dependent = self.create_task(
                store, "Dependent", "waiting",
                depends_on=f"[{archived.entity_id}]",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )
            changed = store.update_entity(
                dependent.entity_id, dependent.content_hash,
                {"title": "Changed after preview"}, None,
            )

            with self.assertRaisesRegex(
                MutationPlanConflict, "dependency references changed"
            ):
                assert isinstance(plan, WorkflowPlan)
                store.apply_task_workflow(plan)

            self.assertFalse(
                store.get_entity(archived.entity_id).relative_path.startswith("archive/")
            )
            self.assertEqual(
                store.get_entity(dependent.entity_id).content_hash,
                changed.content_hash,
            )

    def test_task_archive_rolls_back_archive_and_dependency_updates_together(self) -> None:
        """Catches publication failure leaving an archived target or rewritten dependent."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            archived = self.create_task(store, "Archived", "next")
            dependent = self.create_task(
                store, "Dependent", "waiting", depends_on=f"[{archived.entity_id}]"
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_archive_entity(
                    archived.entity_id, archived.content_hash
                )
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            real_publish = store._publish_no_replace
            calls = 0

            def fail_second_publish(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected archive workflow publication failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(
                store, "_publish_no_replace", side_effect=fail_second_publish
            ):
                with self.assertRaisesRegex(OSError, "archive workflow"):
                    assert isinstance(plan, WorkflowPlan)
                    store.apply_task_workflow(plan)

            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                before,
            )
            self.assertFalse(store.mutation_recovery_required())

    def test_task_archive_without_dependents_remains_a_single_mutation_plan(self) -> None:
        """Catches the compatible standalone archive path becoming a workflow."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            standalone = self.create_task(store, "Standalone", "next")

            plan = store.plan_archive_entity(
                standalone.entity_id, standalone.content_hash
            )

            self.assertIsInstance(plan, MutationPlan)

    def test_project_task_plan_update_keeps_doing_content_fixed_while_normalizing_position(self) -> None:
        """Catches sibling removal either mutating doing content or leaving a position gap."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            project = store.update_entity(
                project.entity_id, project.content_hash, {"status": "doing"}, None
            )
            first = self.create_task(
                store, "First", "planned", project_id=project.entity_id,
                project_position="1",
            )
            running = self.create_task(
                store, "Running", "doing", project_id=project.entity_id,
                project_position="2", work_started_at=self.NOW.isoformat(),
                resume_status="next",
            )

            plan = store.plan_project_task_plan_update(
                project.entity_id,
                project.content_hash,
                [{"key": running.entity_id, "id": running.entity_id,
                  "base_hash": running.content_hash, "title": "Running",
                  "parent_key": None, "status": "doing"}],
                [{"id": first.entity_id, "base_hash": first.content_hash}],
            )
            self.assertEqual(
                [effect.role for effect in plan.effects],
                ["task_updated", "task_archived"],
            )
            store.apply_task_workflow(plan)
            updated = store.get_entity(running.entity_id)
            self.assertEqual(updated.frontmatter["title"], "Running")
            self.assertEqual(updated.frontmatter["status"], "doing")
            self.assertEqual(updated.frontmatter["project_position"], "1")

    def test_project_task_move_normalizes_legacy_sibling_positions_atomically(self) -> None:
        """Catches same-group reorder leaving missing or duplicate project positions."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            first = self.create_task(store, "First", "next", project_id=project.entity_id)
            second = self.create_task(store, "Second", "next", project_id=project.entity_id)
            self.create_task(
                store,
                "First child",
                "planned",
                project_id=project.entity_id,
                depends_on=f"[{first.entity_id}]",
            )
            self.create_task(
                store,
                "Second child",
                "planned",
                project_id=project.entity_id,
                depends_on=f"[{second.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_move(
                    second.entity_id,
                    second.content_hash,
                    project_id=project.entity_id,
                    status="next",
                    primary_parent_id=None,
                    position=1,
                )

            self.assertEqual(
                [(effect.role, effect.entity_id) for effect in plan.effects],
                [("task_moved", second.entity_id), ("task_reordered", first.entity_id)],
            )
            results = store.apply_task_workflow(plan)
            self.assertEqual(
                [(entity.entity_id, entity.frontmatter["project_position"]) for entity in results],
                [(second.entity_id, "1"), (first.entity_id, "2")],
            )
            self.assertEqual(validate_repository(root), [])

    def test_project_task_move_excludes_standalone_planned_tasks_from_root_siblings(self) -> None:
        """Catches a tree reorder silently enrolling standalone planned Tasks."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            standalone = self.create_task(
                store,
                "Standalone",
                "planned",
                project_id=project.entity_id,
            )
            first = self.create_task(
                store,
                "First tree root",
                "planned",
                project_id=project.entity_id,
                project_position="1",
            )
            second = self.create_task(
                store,
                "Second tree root",
                "planned",
                project_id=project.entity_id,
                project_position="2",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_move(
                    second.entity_id,
                    second.content_hash,
                    project_id=project.entity_id,
                    status="planned",
                    primary_parent_id=None,
                    position=1,
                )

            self.assertEqual(
                [effect.entity_id for effect in plan.effects],
                [second.entity_id, first.entity_id],
            )
            store.apply_task_workflow(plan)
            self.assertNotIn(
                "project_position",
                store.get_entity(standalone.entity_id).frontmatter,
            )

    def test_project_task_move_rejects_a_standalone_planned_task(self) -> None:
        """Catches a standalone planned Task being enrolled through tree reorder."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            standalone = self.create_task(
                store,
                "Standalone",
                "planned",
                project_id=project.entity_id,
            )

            with self.assertRaisesRegex(InputError, "outside the Project Task tree"):
                store.plan_project_task_move(
                    standalone.entity_id,
                    standalone.content_hash,
                    project_id=project.entity_id,
                    status="planned",
                    primary_parent_id=None,
                    position=1,
                )

    def test_normal_create_and_update_allow_standalone_planned_tasks(self) -> None:
        """Catches planned status becoming coupled to tree position metadata again."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)

            created = self.create_task(
                store,
                "Created standalone",
                "planned",
                project_id=project.entity_id,
            )
            self.assertNotIn("project_position", created.frontmatter)

            existing = self.create_task(store, "Existing standalone", "next")
            plan = store.plan_update_entity(
                existing.entity_id,
                existing.content_hash,
                {"status": "planned", "project_id": project.entity_id},
                None,
            )
            self.assertIsInstance(plan, MutationPlan)
            updated = store.apply_mutation_plan(plan)
            self.assertEqual(updated.frontmatter["status"], "planned")
            self.assertEqual(updated.frontmatter["project_id"], project.entity_id)
            self.assertNotIn("project_position", updated.frontmatter)

    def test_normal_status_update_preserves_project_tree_membership(self) -> None:
        """Catches the planned/Next root switch detaching a Task from its tree."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            tree_root = self.create_task(
                store,
                "Tree root",
                "planned",
                project_id=project.entity_id,
                project_position="1",
            )

            plan = store.plan_update_entity(
                tree_root.entity_id,
                tree_root.content_hash,
                {"status": "next"},
                None,
            )
            updated = store.apply_mutation_plan(plan)

            self.assertEqual(updated.frontmatter["status"], "next")
            self.assertEqual(updated.frontmatter["project_id"], project.entity_id)
            self.assertEqual(updated.frontmatter["project_position"], "1")
            self.assertEqual(validate_repository(root), [])

    def test_planned_dependency_completion_promotes_atomically_and_preserves_history(self) -> None:
        """Catches planned children staying hidden after all prerequisites finish."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            dependency = self.create_task(
                store,
                "Dependency",
                "doing",
                project_id=project.entity_id,
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            child = self.create_task(
                store,
                "Child",
                "planned",
                project_id=project.entity_id,
                project_position="1",
                depends_on=f"[{dependency.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_complete(dependency.entity_id, dependency.content_hash)

            self.assertEqual([effect.role for effect in plan.effects], ["completed", "dependency_released"])
            promoted = plan.effects[1].planned_entity.frontmatter
            self.assertEqual(promoted["status"], "next")
            self.assertEqual(promoted["depends_on"], f"[{dependency.entity_id}]")
            self.assertEqual(promoted["project_position"], "1")
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_legacy_project_task_migration_is_bounded_and_hash_atomic(self) -> None:
        """Catches migration widening its predicate or partially applying after hash drift."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = self.create_project(store)
            other_project = self.create_project(store, "Other")
            dependency = self.create_task(store, "Dependency", "next", project_id=project.entity_id)
            first = self.create_task(
                store, "First", "waiting", project_id=project.entity_id,
                depends_on=f"[{dependency.entity_id}]",
            )
            second = self.create_task(
                store, "Second", "waiting", project_id=project.entity_id,
                depends_on=f"[{dependency.entity_id}]",
            )
            waiting_for = self.create_task(
                store, "Human", "waiting", project_id=project.entity_id,
                depends_on=f"[{dependency.entity_id}]", waiting_for="reply",
            )
            no_dependency = self.create_task(
                store, "No dependency", "waiting", project_id=project.entity_id,
            )
            outside = self.create_task(
                store, "Outside", "waiting", project_id=other_project.entity_id,
                depends_on=f"[{dependency.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_project_task_plan_migrate(
                    project.entity_id, project.content_hash
                )

            self.assertEqual(
                [(effect.role, effect.entity_id) for effect in plan.effects],
                [("planned_migrated", first.entity_id), ("planned_migrated", second.entity_id)],
            )
            self.assertTrue(all(effect.planned_entity.frontmatter["status"] == "planned" for effect in plan.effects))
            changed = store.update_entity(second.entity_id, second.content_hash, {"title": "Changed"}, None)

            with self.assertRaises(ConflictError):
                store.apply_task_workflow(plan)

            self.assertEqual(store.get_entity(first.entity_id).frontmatter["status"], "waiting")
            self.assertEqual(store.get_entity(second.entity_id).content_hash, changed.content_hash)
            for untouched in (waiting_for, no_dependency, outside):
                self.assertEqual(store.get_entity(untouched.entity_id).frontmatter["status"], "waiting")
            self.assertFalse(store.mutation_recovery_required())

    def test_correct_work_session_plans_recorded_task_and_calendar_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            task = self.create_legacy_task(
                store,
                "Recorded",
                "done",
                work_started_at="2026-07-19T08:00:00+09:00",
                work_ended_at="2026-07-19T08:30:00+09:00",
                started_at="2026-07-19T08:00:00+09:00",
                completed_at="2026-07-19T08:30:00+09:00",
                **{**CALENDAR_FIELDS, "calendar_event_kind": "timed"},
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_correct_work_session(
                    task.entity_id,
                    task.content_hash,
                    work_started_at="2026-07-19T07:55:00+09:00",
                    work_ended_at="2026-07-19T08:45:00+09:00",
                )

            proposed = plan.planned_entity.frontmatter
            self.assertEqual(proposed["work_started_at"], "2026-07-19T07:55:00+09:00")
            self.assertEqual(proposed["work_ended_at"], "2026-07-19T08:45:00+09:00")
            self.assertEqual(proposed["started_at"], proposed["work_started_at"])
            self.assertEqual(proposed["completed_at"], proposed["work_ended_at"])

    def test_correct_work_session_rejects_future_order_break_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            task = self.create_task(
                store,
                "Recorded",
                "done",
                work_started_at="2026-07-19T08:00:00+09:00",
                work_ended_at="2026-07-19T08:30:00+09:00",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                for values, error in (
                    ({"work_started_at": "2026-07-19T09:08:08+09:00", "work_ended_at": "2026-07-19T09:08:09+09:00"}, "future"),
                    ({"work_started_at": "2026-07-19T08:30:00+09:00", "work_ended_at": "2026-07-19T08:00:00+09:00"}, "earlier"),
                ):
                    with self.subTest(values=values):
                        with self.assertRaisesRegex(InputError, error):
                            store.plan_task_correct_work_session(
                                task.entity_id, task.content_hash, **values
                            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                break_task = store.apply_task_workflow(store.plan_task_start_break())[0]
            with self.assertRaisesRegex(InputError, "break"):
                store.plan_task_correct_work_session(
                    break_task.entity_id,
                    break_task.content_hash,
                    work_started_at="2026-07-19T08:00:01+09:00",
                )
            archived = store.apply_mutation_plan(
                store.plan_archive_entity(task.entity_id, task.content_hash)
            )
            with self.assertRaisesRegex(InputError, "non-archived"):
                store.plan_task_correct_work_session(
                    archived.entity_id,
                    archived.content_hash,
                    work_started_at="2026-07-19T08:00:00+09:00",
                    work_ended_at="2026-07-19T08:30:00+09:00",
                )

    def test_correct_work_session_rejects_non_second_precision_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            task = self.create_task(
                store,
                "Recorded",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            with self.assertRaisesRegex(InputError, "work_started_at"):
                store.plan_task_correct_work_session(
                    task.entity_id,
                    task.content_hash,
                    work_started_at="2026-07-19T08:00:00.500+09:00",
                )

    def test_correct_work_session_requires_existing_valid_record_and_allows_non_calendar_zero_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            StoreTest.write(
                root / "tasks/task-unrecorded-doing.md",
                serialize_frontmatter(
                    {
                        "id": "task-unrecorded-doing",
                        "type": "task",
                        "title": "Unrecorded doing",
                        "status": "doing",
                        "created_at": "2026-07-19T08:00:00+09:00",
                        "updated_at": "2026-07-19T08:00:00+09:00",
                        "resume_status": "next",
                    },
                    "task",
                ) + "Task body\n",
            )
            unrecorded_doing = store.get_entity("task-unrecorded-doing")
            unrecorded_done = self.create_task(store, "Unrecorded done", "done")
            StoreTest.write(
                root / "tasks/task-invalid-done.md",
                serialize_frontmatter(
                    {
                        "id": "task-invalid-done",
                        "type": "task",
                        "title": "Invalid done",
                        "status": "done",
                        "created_at": "2026-07-19T08:00:00+09:00",
                        "updated_at": "2026-07-19T08:00:00+09:00",
                        "work_started_at": "not-a-timestamp",
                        "work_ended_at": "2026-07-19T08:30:00+09:00",
                    },
                    "task",
                ) + "Task body\n",
            )
            invalid_done = store.get_entity("task-invalid-done")
            recorded = self.create_task(
                store,
                "Recorded",
                "done",
                work_started_at="2026-07-19T08:00:00+09:00",
                work_ended_at="2026-07-19T08:30:00+09:00",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                for task in (unrecorded_doing, unrecorded_done, invalid_done):
                    with self.subTest(task=task.entity_id):
                        with self.assertRaisesRegex(InputError, "record"):
                            store.plan_task_correct_work_session(
                                task.entity_id,
                                task.content_hash,
                                work_started_at="2026-07-19T08:00:00+09:00",
                                work_ended_at=(
                                    None
                                    if task.frontmatter["status"] == "doing"
                                    else "2026-07-19T08:00:00+09:00"
                                ),
                            )
                plan = store.plan_task_correct_work_session(
                    recorded.entity_id,
                    recorded.content_hash,
                    work_started_at="2026-07-19T08:00:00+09:00",
                    work_ended_at="2026-07-19T08:00:00+09:00",
                )
            self.assertEqual(
                plan.planned_entity.frontmatter["work_ended_at"],
                "2026-07-19T08:00:00+09:00",
            )

    def test_create_and_start_handles_zero_or_one_doing_atomically(self) -> None:
        for has_active in (False, True):
            with self.subTest(has_active=has_active), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                active = None
                if has_active:
                    active = self.create_task(
                        store,
                        "Current",
                        "doing",
                        work_started_at="2026-07-19T08:00:00+09:00",
                        resume_status="next",
                    )

                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_create_and_start(
                        "Fresh",
                        active_entity_id=None if active is None else active.entity_id,
                        active_base_hash=None if active is None else active.content_hash,
                        resolution=None if active is None else "complete",
                    )

                expected_roles = ["started"] if active is None else ["completed", "started"]
                self.assertEqual([effect.role for effect in plan.effects], expected_roles)
                created = plan.effects[-1]
                self.assertIsNone(created.before_entity)
                self.assertEqual(created.planned_entity.frontmatter, {
                    "id": created.entity_id,
                    "type": "task",
                    "title": "Fresh",
                    "status": "doing",
                    "created_at": "2026-07-19T09:08:07+09:00",
                    "updated_at": "2026-07-19T09:08:07+09:00",
                    "work_started_at": "2026-07-19T09:08:07+09:00",
                    "resume_status": "next",
                })
                self.assertEqual(created.planned_entity.body, "")

                results = store.apply_task_workflow(plan)

                self.assertEqual(results, tuple(effect.planned_entity for effect in plan.effects))
                self.assertEqual(store.get_entity(created.entity_id).frontmatter["status"], "doing")
                if active is not None:
                    self.assertEqual(store.get_entity(active.entity_id).frontmatter["status"], "done")
                self.assertEqual(validate_repository(root), [])

    def test_create_and_start_uses_warm_planning_index_for_generated_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            self.create_task(store, "Existing", "next")

            with (
                mock.patch(
                    "webapp.store.current_time", return_value=self.NOW
                ),
                mock.patch.object(
                    store,
                    "next_entity_id",
                    side_effect=AssertionError(
                        "Quick Start must not rescan every entity"
                    ),
                ),
            ):
                plan = store.plan_task_create_and_start("Quick")

            self.assertEqual(plan.action, "create_and_start")
            self.assertEqual(plan.effects[-1].role, "started")

    def test_create_and_start_interrupts_current_and_creates_continuation_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="scheduled",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_create_and_start(
                    "Fresh",
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="interrupt",
                )

            self.assertEqual(
                [effect.role for effect in plan.effects],
                ["interrupted", "continuation", "started"],
            )
            continuation = plan.effects[1].planned_entity
            self.assertEqual(continuation.frontmatter["title"], "Current")
            self.assertEqual(continuation.frontmatter["status"], "scheduled")
            self.assertEqual(continuation.frontmatter["continuation_of"], active.entity_id)

            store.apply_task_workflow(plan)

            self.assertEqual(store.get_entity(active.entity_id).frontmatter["status"], "done")
            self.assertEqual(store.get_entity(continuation.entity_id).frontmatter["status"], "scheduled")
            self.assertEqual(store.get_entity(plan.effects[2].entity_id).frontmatter["status"], "doing")
            self.assertEqual(validate_repository(root), [])

    def test_create_and_start_rejects_unknown_active_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(store, "Current", "doing")

            with self.assertRaisesRegex(InputError, "requires"):
                store.plan_task_create_and_start(
                    "Fresh",
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="pause",
                )

    def test_create_and_start_rejects_multiple_doing_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            self.create_task(store, "One", "doing")
            StoreTest.write(
                root / "tasks/task-20260719-999.md",
                StoreTest.document(
                    "task-20260719-999", "task", "Two\n", status="doing"
                ),
            )
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }

            with self.assertRaisesRegex(InputError, "multiple"):
                store.plan_task_create_and_start("Fresh")

            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                before,
            )

    def test_create_and_start_rejects_stale_active_hash_and_generated_id_collision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(store, "Current", "doing")

            with self.assertRaises(ConflictError):
                store.plan_task_create_and_start(
                    "Fresh",
                    active_entity_id=active.entity_id,
                    active_base_hash="0" * 64,
                    resolution="complete",
                )

            occupied_id = "task-20260719-999"
            StoreTest.write(
                root / f"inbox/{occupied_id}.md",
                StoreTest.document(
                    occupied_id, "task", "Occupied\n", status="inbox"
                ),
            )
            before_collision = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            with (
                mock.patch("webapp.store.current_time", return_value=self.NOW),
                mock.patch.object(
                    store,
                    "_next_task_id_from_planning",
                    return_value=occupied_id,
                ),
                self.assertRaises(DestinationConflict),
            ):
                store.plan_task_create_and_start(
                    "Fresh",
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                )

            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                before_collision,
            )

    def test_create_and_start_is_single_use_and_rolls_back_all_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Same title",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_create_and_start(
                    "Same title",
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                )
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            real_publish = store._publish_no_replace
            calls = 0

            def fail_second_publish(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("publish failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(
                store, "_publish_no_replace", side_effect=fail_second_publish
            ):
                with self.assertRaisesRegex(OSError, "publish failure"):
                    store.apply_task_workflow(plan)

            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                before,
            )
            self.assertFalse(store.mutation_recovery_required())

            results = store.apply_task_workflow(plan)
            self.assertNotEqual(results[-1].entity_id, active.entity_id)
            with self.assertRaises((ConflictError, MutationPlanConflict)):
                store.apply_task_workflow(plan)

    def test_create_and_start_rollback_failure_arms_persistent_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_create_and_start("Fresh")

            with (
                mock.patch.object(
                    store,
                    "_publish_no_replace",
                    side_effect=OSError("publish failure"),
                ),
                mock.patch.object(
                    store,
                    "_rollback_task_workflow",
                    side_effect=WorkflowRollbackError(
                        (OSError("rollback failure"),), ()
                    ),
                ),
                self.assertRaises(WorkflowRollbackError),
            ):
                store.apply_task_workflow(plan)

            self.assertTrue(Store(root).mutation_recovery_required())

    def test_create_and_start_apply_time_id_or_path_collision_is_a_noop(self) -> None:
        for collision_kind in ("id", "path"):
            with self.subTest(collision_kind=collision_kind), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                active = self.create_task(store, "Current", "doing")
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_create_and_start(
                        "Fresh",
                        active_entity_id=active.entity_id,
                        active_base_hash=active.content_hash,
                        resolution="complete",
                    )
                created = plan.effects[-1]
                if collision_kind == "id":
                    StoreTest.write(
                        root / f"archive/{created.entity_id}.md",
                        StoreTest.document(
                            created.entity_id,
                            "task",
                            "External ID\n",
                            status="done",
                        ),
                    )
                else:
                    StoreTest.write(
                        root / created.destination_relative_path,
                        StoreTest.document(
                            "task-external-path",
                            "task",
                            "External path\n",
                            status="next",
                        ),
                    )
                before = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }

                with self.assertRaises(DestinationConflict):
                    store.apply_task_workflow(plan)

                self.assertEqual(
                    {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*.md")
                    },
                    before,
                )
                self.assertFalse(store.mutation_recovery_required())

    def test_start_accepts_every_non_done_status_and_normalizes_state(self) -> None:
        statuses = ("inbox", "next", "doing", "waiting", "scheduled", "someday")
        for status in statuses:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                fields = {
                    "scheduled_start": "2026-07-20T10:00:00+09:00",
                    "scheduled_end": "2026-07-20T11:00:00+09:00",
                }
                if status == "doing":
                    fields.update(
                        work_started_at="2026-07-19T08:00:00+09:00",
                        resume_status="next",
                    )
                task = self.create_task(store, status, status, **fields)
                before = (root / task.relative_path).read_bytes()

                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(task.entity_id, task.content_hash)

                self.assertIsInstance(plan, WorkflowPlan)
                self.assertEqual(len(plan.effects), 1)
                proposed = plan.effects[0].planned_entity
                self.assertEqual((root / task.relative_path).read_bytes(), before)
                self.assertEqual(proposed.frontmatter["status"], "doing")
                self.assertEqual(
                    proposed.frontmatter["work_started_at"],
                    "2026-07-19T09:08:07+09:00",
                )
                expected_resume = "scheduled" if status == "scheduled" else "next"
                self.assertEqual(proposed.frontmatter["resume_status"], expected_resume)
                if status == "scheduled":
                    self.assertIn("scheduled_start", proposed.frontmatter)
                    self.assertIn("scheduled_end", proposed.frontmatter)
                else:
                    self.assertNotIn("waiting_for", proposed.frontmatter)
                    self.assertNotIn("scheduled_start", proposed.frontmatter)
                    self.assertNotIn("scheduled_end", proposed.frontmatter)

                results = store.apply_task_workflow(plan)
                self.assertEqual(results, (plan.effects[0].planned_entity,))
                self.assertEqual(
                    (root / proposed.relative_path).read_bytes(),
                    plan.effects[0].after_bytes,
                )
                self.assertEqual(validate_repository(root), [])

    def test_calendar_linked_start_and_five_minute_complete_are_one_effect_each(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_legacy_task(
                store, "Calendar", "next", **CALENDAR_FIELDS
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                start = store.plan_task_start(current.entity_id, current.content_hash)
            self.assertEqual(len(start.effects), 1)
            started = start.effects[0].planned_entity
            self.assertEqual(
                started.frontmatter["started_at"],
                started.frontmatter["work_started_at"],
            )
            self.assertEqual(started.frontmatter["calendar_event_kind"], "timed")
            store.apply_task_workflow(start)
            self.assertEqual(validate_repository(root), [])

            stored = store.get_entity(current.entity_id)
            completed_now = self.NOW + datetime.timedelta(minutes=5)
            with mock.patch("webapp.store.current_time", return_value=completed_now):
                complete = store.plan_task_complete(
                    stored.entity_id, stored.content_hash
                )
            self.assertEqual(len(complete.effects), 1)
            completed = complete.effects[0].planned_entity
            self.assertEqual(
                completed.frontmatter["completed_at"],
                "2026-07-19T09:13:07+09:00",
            )
            self.assertEqual(
                completed.frontmatter["work_ended_at"],
                completed.frontmatter["completed_at"],
            )
            self.assertEqual(
                completed.frontmatter["started_at"],
                started.frontmatter["started_at"],
            )
            self.assertEqual(completed.frontmatter["calendar_event_kind"], "timed")
            store.apply_task_workflow(complete)
            self.assertEqual(validate_repository(root), [])

    def test_calendar_linked_interrupt_finalizes_old_task_without_linking_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_legacy_task(
                store, "Calendar", "next", **CALENDAR_FIELDS
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                started_plan = store.plan_task_start(
                    current.entity_id, current.content_hash
                )
            store.apply_task_workflow(started_plan)
            started = store.get_entity(current.entity_id)

            interrupted_now = self.NOW + datetime.timedelta(minutes=5)
            with mock.patch("webapp.store.current_time", return_value=interrupted_now):
                plan = store.plan_task_interrupt(
                    started.entity_id, started.content_hash
                )
            closed, continuation = (effect.planned_entity for effect in plan.effects)

            self.assertEqual(
                closed.frontmatter["completed_at"],
                "2026-07-19T09:13:07+09:00",
            )
            self.assertEqual(
                closed.frontmatter["work_ended_at"],
                closed.frontmatter["completed_at"],
            )
            self.assertEqual(closed.frontmatter["calendar_event_kind"], "timed")
            for key in (*CALENDAR_FIELDS, "started_at", "completed_at"):
                self.assertNotIn(key, continuation.frontmatter)
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_interrupt_preserves_direct_area_for_standalone_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                area = store.create_entity("area", {"title": "Health"}, "Body")
            current = self.create_task(
                store,
                "Standalone",
                "doing",
                area_id=area.entity_id,
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_interrupt(
                    current.entity_id, current.content_hash
                )

            continuation = plan.effects[1].planned_entity
            self.assertEqual(continuation.frontmatter["area_id"], area.entity_id)
            self.assertNotIn("project_id", continuation.frontmatter)
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_calendar_linked_zero_length_completion_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_legacy_task(
                store,
                "Zero",
                "doing",
                work_started_at="2026-07-19T09:08:07+09:00",
                resume_status="next",
                started_at="2026-07-19T09:08:07+09:00",
                **{**CALENDAR_FIELDS, "calendar_event_kind": "timed"},
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaises(InputError):
                    store.plan_task_complete(current.entity_id, current.content_hash)

    def test_calendar_linked_switch_backfills_active_for_both_resolutions(self) -> None:
        for resolution in ("complete", "interrupt"):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                active = self.create_legacy_task(
                    store,
                    "Active",
                    "doing",
                    work_started_at="2026-07-19T08:00:00+09:00",
                    resume_status="next",
                    **CALENDAR_FIELDS,
                )
                target = self.create_task(store, "Target", "next")

                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=active.entity_id,
                        active_base_hash=active.content_hash,
                        resolution=resolution,
                    )
                closed = plan.effects[0].planned_entity
                self.assertEqual(
                    closed.frontmatter["started_at"],
                    "2026-07-19T08:00:00+09:00",
                )
                self.assertEqual(
                    closed.frontmatter["completed_at"],
                    "2026-07-19T09:08:07+09:00",
                )
                self.assertEqual(closed.frontmatter["calendar_event_kind"], "timed")
                if resolution == "interrupt":
                    continuation = plan.effects[1].planned_entity
                    for key in (*CALENDAR_FIELDS, "started_at", "completed_at"):
                        self.assertNotIn(key, continuation.frontmatter)
                started_target = plan.effects[-1].planned_entity
                self.assertNotIn("started_at", started_target.frontmatter)
                self.assertNotIn("completed_at", started_target.frontmatter)
                store.apply_task_workflow(plan)
                self.assertEqual(validate_repository(root), [])

    def test_explicit_switch_boundary_uses_event_for_calendar_and_rejects_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            started_at = "2026-07-19T08:00:00+09:00"
            active = self.create_legacy_task(
                store,
                "Calendar active",
                "doing",
                work_started_at=started_at,
                resume_status="next",
                started_at=started_at,
                **{**CALENDAR_FIELDS, "calendar_event_kind": "timed"},
            )
            target = self.create_task(store, "Target", "next")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaises(InputError):
                    store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=active.entity_id,
                        active_base_hash=active.content_hash,
                        resolution="complete",
                        active_work_ended_at=started_at,
                    )
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                    active_work_ended_at="2026-07-19T08:00:01+09:00",
                )
            closed = plan.effects[0].planned_entity
            self.assertEqual(
                closed.frontmatter["completed_at"], "2026-07-19T08:00:01+09:00"
            )
            self.assertEqual(
                closed.frontmatter["work_ended_at"], closed.frontmatter["completed_at"]
            )
            self.assertEqual(
                plan.effects[-1].planned_entity.frontmatter["work_started_at"],
                "2026-07-19T08:00:01+09:00",
            )

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(self.make_repository(pathlib.Path(temporary)))
            legacy = self.create_task(store, "Legacy active", "doing")
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaisesRegex(InputError, "requires work_started_at"):
                    store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=legacy.entity_id,
                        active_base_hash=legacy.content_hash,
                        resolution="complete",
                        active_work_ended_at="2026-07-19T08:00:01+09:00",
                    )

    def test_calendar_linked_direct_complete_stays_all_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_legacy_task(
                store, "Direct", "doing", **CALENDAR_FIELDS
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_complete(current.entity_id, current.content_hash)
            after = plan.effects[0].planned_entity.frontmatter

            self.assertEqual(after["completed_at"], "2026-07-19T09:08:07+09:00")
            self.assertEqual(after["calendar_event_kind"], "all_day")
            self.assertNotIn("started_at", after)
            self.assertNotIn("work_ended_at", after)
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_calendar_workflow_rejects_partial_identity_and_mismatched_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            partial_frontmatter = {
                "id": "task-partial-calendar",
                "type": "task",
                "title": "Partial",
                "status": "next",
                "created_at": "2026-07-18T09:00:00+09:00",
                "updated_at": "2026-07-18T09:00:00+09:00",
                "calendar_id": CALENDAR_FIELDS["calendar_id"],
            }
            StoreTest.write(
                root / "tasks/task-partial-calendar.md",
                serialize_frontmatter(partial_frontmatter, "task") + "Body\n",
            )
            partial = store.get_entity("task-partial-calendar")
            with self.assertRaisesRegex(InputError, "Calendar identity"):
                store.plan_task_start(partial.entity_id, partial.content_hash)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            mismatched = self.create_legacy_task(
                store,
                "Mismatch",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
                started_at="2026-07-19T08:01:00+09:00",
                **{**CALENDAR_FIELDS, "calendar_event_kind": "timed"},
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaisesRegex(InputError, "started_at"):
                    store.plan_task_complete(
                        mismatched.entity_id, mismatched.content_hash
                    )

    def test_non_calendar_workflow_does_not_add_calendar_lifecycle_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(store, "Ordinary", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                start = store.plan_task_start(current.entity_id, current.content_hash)
            started = start.effects[0].planned_entity
            for key in (*CALENDAR_FIELDS, "started_at", "completed_at"):
                self.assertNotIn(key, started.frontmatter)
            store.apply_task_workflow(start)

            current = store.get_entity(current.entity_id)
            later = self.NOW + datetime.timedelta(minutes=5)
            with mock.patch("webapp.store.current_time", return_value=later):
                complete = store.plan_task_complete(
                    current.entity_id, current.content_hash
                )
            completed = complete.effects[0].planned_entity
            for key in (*CALENDAR_FIELDS, "started_at", "completed_at"):
                self.assertNotIn(key, completed.frontmatter)
            store.apply_task_workflow(complete)
            self.assertEqual(validate_repository(root), [])

    def test_start_rejects_done_and_requires_resolution_for_another_doing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            done = self.create_task(store, "Done", "done")
            with self.assertRaises(InputError):
                store.plan_task_start(done.entity_id, done.content_hash)

            current = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(store, "Target", "next")
            with self.assertRaises(InputError):
                store.plan_task_start(target.entity_id, target.content_hash)
            with self.assertRaises(ConflictError):
                store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash="0" * 64,
                    resolution="complete",
                )
            with self.assertRaises(InputError):
                store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="cancel",
                )

    def test_complete_records_end_or_safely_closes_legacy_doing(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                fields = {} if legacy else {
                    "work_started_at": "2026-07-19T08:00:00+09:00",
                    "resume_status": "next",
                }
                current = self.create_task(store, "Current", "doing", **fields)
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_complete(
                        current.entity_id, current.content_hash
                    )
                proposed = plan.effects[0].planned_entity.frontmatter
                self.assertEqual(proposed["status"], "done")
                self.assertNotIn("resume_status", proposed)
                if legacy:
                    self.assertNotIn("work_ended_at", proposed)
                else:
                    self.assertEqual(
                        proposed["work_ended_at"], "2026-07-19T09:08:07+09:00"
                    )
                store.apply_task_workflow(plan)
                self.assertEqual(validate_repository(root), [])

    def test_future_available_from_moves_next_to_waiting_and_start_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            created = self.create_task(
                store,
                "Created future",
                "next",
                available_from="2026-07-19T09:09:00+09:00",
            )
            self.assertEqual(created.frontmatter["status"], "waiting")
            target = self.create_task(store, "Future", "next")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_update_entity(
                    target.entity_id,
                    target.content_hash,
                    {"available_from": "2026-07-19T09:09:00+09:00"},
                    None,
                )

            proposed = (
                plan.planned_entity
                if isinstance(plan, MutationPlan)
                else plan.effects[0].planned_entity
            )
            self.assertEqual(proposed.frontmatter["status"], "waiting")
            self.assertEqual(
                proposed.frontmatter["available_from"],
                "2026-07-19T09:09:00+09:00",
            )
            if isinstance(plan, MutationPlan):
                store.apply_mutation_plan(plan)
            else:
                store.apply_task_workflow(plan)
            waiting = store.get_entity(target.entity_id)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaisesRegex(InputError, "available_from"):
                    store.plan_task_start(waiting.entity_id, waiting.content_hash)

    def test_available_release_uses_jst_midnight_for_date_only_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(
                store, "Date only", "waiting", available_from="2026-07-19"
            )
            before = datetime.datetime(
                2026,
                7,
                18,
                23,
                59,
                59,
                tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
            )
            exact = datetime.datetime(
                2026,
                7,
                19,
                0,
                0,
                0,
                tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
            )

            self.assertIsNone(store.plan_available_task_releases(before))
            with mock.patch("webapp.store.current_time", return_value=exact):
                plan = store.plan_available_task_releases(exact)

            assert plan is not None
            self.assertEqual(
                [effect.entity_id for effect in plan.effects], [target.entity_id]
            )

    def test_available_release_compares_aware_timestamps_by_instant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(
                store,
                "Offset instant",
                "waiting",
                available_from="2026-07-19T00:08:07Z",
            )
            exact = self.NOW

            self.assertIsNone(
                store.plan_available_task_releases(
                    exact - datetime.timedelta(microseconds=1)
                )
            )
            for now in (exact, exact + datetime.timedelta(microseconds=1)):
                with self.subTest(now=now):
                    with mock.patch("webapp.store.current_time", return_value=now):
                        plan = store.plan_available_task_releases(now)
                    assert plan is not None
                    self.assertEqual(
                        [effect.entity_id for effect in plan.effects],
                        [target.entity_id],
                    )

    def test_available_release_requires_timezone_aware_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)

            for invalid_now in (
                self.NOW.date(),
                self.NOW.replace(tzinfo=None),
            ):
                with self.subTest(invalid_now=invalid_now):
                    with self.assertRaisesRegex(
                        InputError, "timezone-aware datetime"
                    ):
                        store.plan_available_task_releases(invalid_now)  # type: ignore[arg-type]

    def test_available_release_waits_for_every_gate_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            unfinished = self.create_task(store, "Dependency", "next")
            ready = self.create_task(
                store, "Ready", "waiting", available_from="2026-07-19"
            )
            date_blocked = self.create_task(
                store, "Date blocked", "waiting", available_from="2026-07-20"
            )
            dependency_blocked = self.create_task(
                store,
                "Dependency blocked",
                "waiting",
                available_from="2026-07-19",
                depends_on=f"[{unfinished.entity_id}]",
            )
            human_blocked = self.create_task(
                store,
                "Human blocked",
                "waiting",
                available_from="2026-07-19",
                waiting_for="返答待ち",
            )
            legacy = self.create_task(
                store, "Legacy waiting", "waiting", depends_on="[]"
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_available_task_releases(self.NOW)

            self.assertIsInstance(plan, WorkflowPlan)
            assert plan is not None
            self.assertEqual([effect.role for effect in plan.effects], ["availability_released"])
            self.assertEqual([effect.entity_id for effect in plan.effects], [ready.entity_id])
            released = plan.effects[0].planned_entity
            self.assertEqual(released.frontmatter["status"], "next")
            self.assertEqual(released.frontmatter["available_from"], "2026-07-19")
            store.apply_task_workflow(plan)
            for blocked in (date_blocked, dependency_blocked, human_blocked, legacy):
                self.assertEqual(
                    store.get_entity(blocked.entity_id).frontmatter["status"],
                    "waiting",
                )

    def test_clearing_waiting_for_or_available_from_releases_only_auto_gated_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            human = self.create_task(
                store,
                "Human",
                "waiting",
                available_from="2026-07-19",
                waiting_for="返答待ち",
            )
            future = self.create_task(
                store, "Future", "waiting", available_from="2026-07-20"
            )
            legacy = self.create_task(store, "Legacy", "waiting", waiting_for="返答待ち")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                human_plan = store.plan_update_entity(
                    human.entity_id, human.content_hash, {"waiting_for": ""}, None
                )
            self.assertIsInstance(human_plan, WorkflowPlan)
            self.assertEqual(
                [effect.role for effect in human_plan.effects],
                ["availability_released"],
            )
            store.apply_task_workflow(human_plan)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                future_plan = store.plan_update_entity(
                    future.entity_id,
                    future.content_hash,
                    {"available_from": ""},
                    None,
                )
            self.assertIsInstance(future_plan, WorkflowPlan)
            self.assertEqual(
                future_plan.effects[0].planned_entity.frontmatter["status"], "next"
            )
            self.assertNotIn(
                "available_from", future_plan.effects[0].planned_entity.frontmatter
            )
            store.apply_task_workflow(future_plan)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                legacy_plan = store.plan_update_entity(
                    legacy.entity_id,
                    legacy.content_hash,
                    {"waiting_for": ""},
                    None,
                )
            self.assertIsInstance(legacy_plan, MutationPlan)
            self.assertEqual(legacy_plan.planned_entity.frontmatter["status"], "waiting")

    def test_available_release_effects_are_id_ordered_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            first = self.create_task(
                store, "First", "waiting", available_from="2026-07-19"
            )
            second = self.create_task(
                store, "Second", "waiting", available_from="2026-07-19"
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_available_task_releases(self.NOW)

            assert plan is not None
            self.assertEqual(
                [effect.entity_id for effect in plan.effects],
                sorted([first.entity_id, second.entity_id]),
            )
            original_publish = store._publish_no_replace
            calls = 0

            def fail_second_publication(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected availability publication failure")
                return original_publish(*args, **kwargs)

            with mock.patch.object(
                store, "_publish_no_replace", side_effect=fail_second_publication
            ):
                with self.assertRaisesRegex(OSError, "injected availability"):
                    store.apply_task_workflow(plan)

            self.assertEqual(store.get_entity(first.entity_id).frontmatter["status"], "waiting")
            self.assertEqual(store.get_entity(second.entity_id).frontmatter["status"], "waiting")
            self.assertFalse(store.mutation_recovery_required())

    def test_available_release_returns_none_without_ready_auto_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            self.create_task(store, "Legacy", "waiting", depends_on="[]")

            self.assertIsNone(
                store.plan_available_task_releases(self.NOW)
            )

    def test_available_release_rejects_dependency_reopened_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            dependency = self.create_task(store, "Dependency", "done")
            target = self.create_task(
                store,
                "Target",
                "waiting",
                available_from="2026-07-19",
                depends_on=f"[{dependency.entity_id}]",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_available_task_releases(self.NOW)
            assert plan is not None
            store.update_entity(
                dependency.entity_id,
                dependency.content_hash,
                {"status": "next"},
                None,
            )

            with self.assertRaisesRegex(
                MutationPlanConflict, "dependencies changed"
            ):
                store.apply_task_workflow(plan)

            self.assertEqual(
                store.get_entity(target.entity_id).frontmatter["status"],
                "waiting",
            )

    def test_start_rejects_dependency_reopened_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            dependency = self.create_task(store, "Dependency", "done")
            target = self.create_task(
                store,
                "Target",
                "next",
                depends_on=f"[{dependency.entity_id}]",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(target.entity_id, target.content_hash)
            store.update_entity(
                dependency.entity_id,
                dependency.content_hash,
                {"status": "next"},
                None,
            )

            with self.assertRaisesRegex(
                MutationPlanConflict, "dependencies changed"
            ):
                store.apply_task_workflow(plan)

            self.assertEqual(
                store.get_entity(target.entity_id).frontmatter["status"],
                "next",
            )

    def test_completion_releases_only_fully_satisfied_dependents_in_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            unfinished = self.create_task(store, "Other", "next")
            already_done = self.create_task(store, "Done", "done")
            blocked = self.create_task(
                store,
                "Blocked",
                "waiting",
                depends_on=f"[{active.entity_id}, {unfinished.entity_id}]",
                waiting_for="外部回答",
                project_id="",
            )
            released_b = self.create_task(
                store,
                "Released B",
                "waiting",
                depends_on=f"[{active.entity_id}, {already_done.entity_id}]",
                waiting_for="確認待ち",
                contexts="[home]",
            )
            released_b = store.update_entity(
                released_b.entity_id,
                released_b.content_hash,
                {},
                "Notes\n\n## 待機履歴\n\n- 以前の待機\n\n## その他\n\nKeep\n",
            )
            released_a = self.create_task(
                store,
                "Released A",
                "waiting",
                depends_on=f"[{active.entity_id}]",
                due="2026-07-31",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_complete(active.entity_id, active.content_hash)

            self.assertEqual(
                [effect.role for effect in plan.effects],
                ["completed", "dependency_released"],
            )
            self.assertEqual(
                [effect.entity_id for effect in plan.effects[1:]],
                [released_a.entity_id],
            )
            by_id = {effect.entity_id: effect.planned_entity for effect in plan.effects}
            self.assertNotIn(released_b.entity_id, by_id)
            self.assertNotIn(blocked.entity_id, by_id)

            store.apply_task_workflow(plan)
            self.assertEqual(store.get_entity(blocked.entity_id).frontmatter["status"], "waiting")
            self.assertEqual(store.get_entity(released_b.entity_id).frontmatter["status"], "waiting")
            self.assertEqual(
                store.get_entity(released_b.entity_id).frontmatter["waiting_for"],
                "確認待ち",
            )
            self.assertEqual(validate_repository(root), [])

    def test_dependency_edit_retargets_waiting_and_releases_when_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            unfinished = self.create_task(store, "Dependency", "next")
            target = self.create_task(store, "Target", "next")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                waiting_plan = store.plan_update_entity(
                    target.entity_id,
                    target.content_hash,
                    {"depends_on": f"[{unfinished.entity_id}]"},
                    None,
                )
            self.assertIsInstance(waiting_plan, WorkflowPlan)
            self.assertEqual([effect.role for effect in waiting_plan.effects], ["dependency_retargeted"])
            self.assertEqual(waiting_plan.effects[0].planned_entity.frontmatter["status"], "waiting")
            store.apply_task_workflow(waiting_plan)

            waiting = store.get_entity(target.entity_id)
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                released_plan = store.plan_update_entity(
                    waiting.entity_id,
                    waiting.content_hash,
                    {"depends_on": "[]"},
                    None,
                )
            self.assertIsInstance(released_plan, WorkflowPlan)
            self.assertEqual([effect.role for effect in released_plan.effects], ["dependency_released"])
            self.assertEqual(released_plan.effects[0].planned_entity.frontmatter["status"], "next")
            self.assertEqual(released_plan.effects[0].planned_entity.frontmatter["depends_on"], "[]")
            store.apply_task_workflow(released_plan)
            self.assertEqual(validate_repository(root), [])

    def test_empty_dependency_field_does_not_release_unrelated_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Human reply", "waiting", waiting_for="返答待ち")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_update_entity(
                    target.entity_id,
                    target.content_hash,
                    {"title": "Human reply updated", "depends_on": "[]"},
                    None,
                )

            self.assertIsInstance(plan, MutationPlan)
            store.apply_mutation_plan(plan)
            updated = store.get_entity(target.entity_id)
            self.assertEqual(updated.frontmatter["status"], "waiting")
            self.assertEqual(updated.frontmatter["waiting_for"], "返答待ち")
            self.assertNotIn("depends_on", updated.frontmatter)

    def test_dependency_create_preview_rejects_repository_wide_reference_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)

            with self.assertRaisesRegex(InputError, "unknown depends_on Task"):
                store.plan_create_entity(
                    "task",
                    {
                        "title": "Invalid dependency",
                        "depends_on": "[task-missing-dependency]",
                    },
                    "",
                )

    def test_reopening_dependency_does_not_move_an_already_released_task_backward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            dependency = self.create_task(store, "Dependency", "done")
            dependent = self.create_task(
                store,
                "Dependent",
                "next",
                depends_on=f"[{dependency.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_update_entity(
                    dependency.entity_id,
                    dependency.content_hash,
                    {"status": "next"},
                    None,
                )
            self.assertIsInstance(plan, MutationPlan)
            store.apply_mutation_plan(plan)
            self.assertEqual(store.get_entity(dependent.entity_id).frontmatter["status"], "next")

    def test_interrupt_retargets_dependents_to_continuation_without_releasing_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            dependent = self.create_task(
                store,
                "Dependent",
                "waiting",
                depends_on=f"[{active.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_interrupt(active.entity_id, active.content_hash)

            continuation = next(effect for effect in plan.effects if effect.role == "continuation")
            retargeted = next(effect for effect in plan.effects if effect.role == "dependency_retargeted")
            self.assertEqual(
                [effect.role for effect in plan.effects],
                ["interrupted", "continuation", "dependency_retargeted"],
            )
            self.assertEqual(
                retargeted.planned_entity.frontmatter["depends_on"],
                f"[{continuation.entity_id}]",
            )
            self.assertEqual(retargeted.planned_entity.frontmatter["status"], "waiting")
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_complete_switch_starts_newly_released_target_without_duplicate_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(
                store,
                "Target",
                "waiting",
                depends_on=f"[{active.entity_id}]",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                )

            self.assertEqual([effect.role for effect in plan.effects], ["completed", "started"])
            self.assertEqual(plan.effects[1].entity_id, target.entity_id)
            self.assertEqual(plan.effects[1].planned_entity.frontmatter["status"], "doing")
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_start_rejects_task_with_any_unfinished_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            unfinished = self.create_task(store, "Dependency", "next")
            target = self.create_task(
                store,
                "Target",
                "waiting",
                depends_on=f"[{unfinished.entity_id}]",
            )

            with self.assertRaisesRegex(InputError, "unfinished dependencies"):
                store.plan_task_start(target.entity_id, target.content_hash)

    def test_start_rejects_non_empty_waiting_for_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(
                store, "Human reply", "waiting", waiting_for="返答待ち"
            )

            with self.assertRaisesRegex(InputError, "waiting_for"):
                store.plan_task_start(target.entity_id, target.content_hash)

    def test_dependency_release_hash_conflict_and_publication_failure_are_all_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            dependent = self.create_task(
                store,
                "Dependent",
                "waiting",
                depends_on=f"[{active.entity_id}]",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                stale_plan = store.plan_task_complete(
                    active.entity_id, active.content_hash
                )
            externally_changed = store.update_entity(
                dependent.entity_id,
                dependent.content_hash,
                {"title": "External"},
                None,
            )

            with self.assertRaises(ConflictError):
                store.apply_task_workflow(stale_plan)

            self.assertEqual(store.get_entity(active.entity_id).frontmatter["status"], "doing")
            self.assertEqual(store.get_entity(dependent.entity_id).content_hash, externally_changed.content_hash)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Active",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            dependent = self.create_task(
                store,
                "Dependent",
                "waiting",
                depends_on=f"[{active.entity_id}]",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_complete(active.entity_id, active.content_hash)
            original_publish = store._publish_no_replace
            calls = 0

            def fail_second_publication(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected dependency publication failure")
                return original_publish(*args, **kwargs)

            with mock.patch.object(
                store, "_publish_no_replace", side_effect=fail_second_publication
            ):
                with self.assertRaisesRegex(OSError, "injected dependency"):
                    store.apply_task_workflow(plan)

            self.assertEqual(store.get_entity(active.entity_id).frontmatter["status"], "doing")
            self.assertEqual(store.get_entity(dependent.entity_id).frontmatter["status"], "waiting")
            self.assertFalse(store.mutation_recovery_required())
            self.assertEqual(validate_repository(root), [])

    def test_interrupt_safely_closes_legacy_doing_without_inventing_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(store, "Legacy", "doing")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_interrupt(
                    current.entity_id, current.content_hash
                )
            closed, continuation = (effect.planned_entity for effect in plan.effects)
            self.assertNotIn("work_ended_at", closed.frontmatter)
            self.assertEqual(continuation.frontmatter["status"], "next")
            self.assertNotIn("work_started_at", continuation.frontmatter)
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

    def test_clock_reversal_is_rejected_only_when_session_start_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store,
                "Future",
                "doing",
                work_started_at="2026-07-19T10:00:00+09:00",
                resume_status="next",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                with self.assertRaises(InputError):
                    store.plan_task_complete(current.entity_id, current.content_hash)
                with self.assertRaises(InputError):
                    store.plan_task_interrupt(current.entity_id, current.content_hash)

    def test_interrupt_closes_current_and_creates_precise_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            project = store.create_entity("project", {"title": "Project"}, "")
            StoreTest.write(
                root / "archive/task-20260718-099.md",
                StoreTest.document(
                    "task-20260718-099", "task", "Old\n", status="done"
                ),
            )
            current = self.create_task(
                store,
                "Current",
                "doing",
                project_id=project.entity_id,
                due="2026-07-31",
                contexts="[home, phone]",
                estimated_minutes="30",
                scheduled_start="2026-07-20T10:00:00+09:00",
                scheduled_end="2026-07-20T11:00:00+09:00",
                waiting_for="reply",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="scheduled",
                continuation_of="task-20260718-099",
            )
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_interrupt(
                    current.entity_id, current.content_hash
                )

            self.assertEqual([effect.role for effect in plan.effects], ["interrupted", "continuation"])
            closed, continuation = (effect.planned_entity for effect in plan.effects)
            self.assertEqual(closed.frontmatter["status"], "done")
            self.assertEqual(closed.frontmatter["work_ended_at"], "2026-07-19T09:08:07+09:00")
            self.assertEqual(continuation.body, current.body)
            self.assertEqual(
                {key: continuation.frontmatter.get(key) for key in (
                    "title", "project_id", "due", "contexts", "estimated_minutes",
                    "scheduled_start", "scheduled_end",
                )},
                {key: current.frontmatter.get(key) for key in (
                    "title", "project_id", "due", "contexts", "estimated_minutes",
                    "scheduled_start", "scheduled_end",
                )},
            )
            self.assertEqual(continuation.frontmatter["status"], "scheduled")
            self.assertEqual(continuation.frontmatter["continuation_of"], current.entity_id)
            for key in ("waiting_for", "work_started_at", "work_ended_at", "resume_status"):
                self.assertNotIn(key, continuation.frontmatter)
            self.assertNotEqual(continuation.frontmatter["id"], current.entity_id)
            self.assertEqual(continuation.frontmatter["created_at"], "2026-07-19T09:08:07+09:00")
            self.assertEqual(continuation.frontmatter["updated_at"], "2026-07-19T09:08:07+09:00")

            results = store.apply_task_workflow(plan)
            self.assertEqual(results, tuple(effect.planned_entity for effect in plan.effects))
            self.assertEqual(validate_repository(root), [])

    def test_interrupt_sets_continuation_action_date_to_local_interruption_day(self) -> None:
        """Catches continuations retaining or omitting the interrupted Task's action date."""
        interruption_now = datetime.datetime(
            2026, 7, 18, 16, 8, 7, tzinfo=datetime.timezone.utc,
        )
        for original_action_date in ("2026-07-01", "2026-07-31", None):
            with self.subTest(original_action_date=original_action_date):
                with tempfile.TemporaryDirectory() as temporary:
                    root = self.make_repository(pathlib.Path(temporary))
                    store = Store(root)
                    fields = {
                        "work_started_at": "2026-07-19T00:00:00+09:00",
                        "resume_status": "next",
                    }
                    if original_action_date is not None:
                        fields["action_date"] = original_action_date
                    current = self.create_task(store, "Current", "doing", **fields)

                    with mock.patch(
                        "webapp.store.current_time", return_value=interruption_now
                    ):
                        plan = store.plan_task_interrupt(
                            current.entity_id, current.content_hash
                        )

                    continuation = plan.effects[1].planned_entity
                    self.assertEqual(
                        continuation.frontmatter["action_date"], "2026-07-19"
                    )
                    closed = plan.effects[0].planned_entity
                    if original_action_date is None:
                        self.assertNotIn("action_date", closed.frontmatter)
                    else:
                        self.assertEqual(
                            closed.frontmatter["action_date"], original_action_date
                        )

    def test_interrupt_switch_keeps_pause_marker_only_on_closed_original(self) -> None:
        marker = (
            "[gtd-focus-monitor] \u901a\u77e5\u505c\u6b62\u671f\u9650: "
            "2026-07-19T10:00:00+09:00"
        )
        similar_prose = f"Keep this prose before {marker} after"
        body = f"\nBody\n\n{marker}\n{similar_prose}\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            current = store.update_entity(
                current.entity_id, current.content_hash, {}, body
            )
            target = self.create_task(store, "Target", "next")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )

            closed = plan.effects[0].planned_entity
            continuation = plan.effects[1].planned_entity
            self.assertEqual(closed.body, body)
            self.assertEqual(continuation.body, f"\nBody\n\n{similar_prose}\n")
            self.assertNotIn(f"\n{marker}\n", continuation.body)

            store.apply_task_workflow(plan)
            persisted_closed = store.get_entity(closed.entity_id)
            persisted_continuation = store.get_entity(continuation.entity_id)
            self.assertEqual(persisted_closed.body, body)
            self.assertEqual(
                persisted_continuation.body, f"\nBody\n\n{similar_prose}\n"
            )

    def test_interrupt_keeps_calendar_impossible_pause_marker_on_continuation(self) -> None:
        invalid_marker = (
            "[gtd-focus-monitor] 通知停止期限: "
            "2026-99-99T99:99:99+09:00"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store, "Current", "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            current = store.update_entity(
                current.entity_id, current.content_hash, {}, f"Body\n\n{invalid_marker}\n"
            )
            target = self.create_task(store, "Target", "next")

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id, target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )

            continuation = plan.effects[1].planned_entity
            self.assertEqual(continuation.body, current.body)

    def test_start_switch_has_ordered_two_or_three_effects_and_one_timestamp(self) -> None:
        for resolution, roles in (
            ("complete", ["completed", "started"]),
            ("interrupt", ["interrupted", "continuation", "started"]),
        ):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create_task(
                    store,
                    "Current",
                    "doing",
                    work_started_at="2026-07-19T08:00:00+09:00",
                    resume_status="next",
                )
                target = self.create_task(store, "Target", "waiting")
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=current.entity_id,
                        active_base_hash=current.content_hash,
                        resolution=resolution,
                    )
                self.assertEqual([effect.role for effect in plan.effects], roles)
                timestamps = {
                    entity.frontmatter[key]
                    for entity in (effect.planned_entity for effect in plan.effects)
                    for key in ("updated_at",)
                }
                self.assertEqual(timestamps, {"2026-07-19T09:08:07+09:00"})
                store.apply_task_workflow(plan)
                self.assertEqual(validate_repository(root), [])

    def test_start_break_complete_closes_current_without_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty_root = self.make_repository(pathlib.Path(temporary))
            empty_store = Store(empty_root)
            with self.assertRaises(InputError):
                empty_store.plan_task_start_break(
                    active_entity_id="task-unexpected",
                    active_base_hash="a" * 64,
                    resolution="complete",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            active = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start_break(
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                )

            self.assertEqual([effect.role for effect in plan.effects], ["completed", "started"])
            self.assertEqual(plan.effects[0].planned_entity.frontmatter["status"], "done")
            self.assertEqual(plan.effects[1].planned_entity.frontmatter["timer_kind"], "break")
            self.assertFalse(any(effect.role == "continuation" for effect in plan.effects))

            interrupt_plan = store.plan_task_start_break(
                active_entity_id=active.entity_id,
                active_base_hash=active.content_hash,
                resolution="interrupt",
            )
            self.assertEqual(
                [effect.role for effect in interrupt_plan.effects],
                ["interrupted", "continuation", "started"],
            )
            with self.assertRaises(InputError):
                store.plan_task_start_break(
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="pause",
                )
            store.apply_task_workflow(plan)
            self.assertEqual(validate_repository(root), [])

            with self.assertRaises(InputError):
                store.plan_task_start_break(
                    active_entity_id=active.entity_id,
                    active_base_hash=active.content_hash,
                    resolution="complete",
                )

            break_task = next(
                entity
                for entity in store.list_entities()
                if entity.frontmatter.get("timer_kind") == "break"
            )
            with self.assertRaises(InputError):
                store.plan_task_start_break(
                    active_entity_id=break_task.entity_id,
                    active_base_hash=break_task.content_hash,
                    resolution="interrupt",
                )

    def test_plan_is_store_bound_exact_and_single_use_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            task = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(task.entity_id, task.content_hash)
            before = (root / task.relative_path).read_bytes()
            self.assertEqual(plan.effects[0].before_bytes, before)
            with self.assertRaises(TypeError):
                plan.effects[0].planned_entity.frontmatter["title"] = "tampered"
            with self.assertRaises(InputError):
                Store(root).apply_task_workflow(plan)
            store.apply_task_workflow(plan)
            with self.assertRaises((ConflictError, MutationPlanConflict)):
                store.apply_task_workflow(plan)

    def test_stale_hash_direct_inode_replacement_duplicate_doing_and_collision_are_noops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            task = self.create_task(store, "Target", "next")
            with self.assertRaises(ConflictError):
                store.plan_task_start(task.entity_id, "0" * 64)

            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(task.entity_id, task.content_hash)
            path = root / task.relative_path
            exact = path.read_bytes()
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(exact)
            os.replace(replacement, path)
            with self.assertRaises(MutationPlanConflict):
                store.apply_task_workflow(plan)
            self.assertEqual(path.read_bytes(), exact)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            first = self.create_task(store, "One", "doing")
            StoreTest.write(
                root / "tasks/task-20260719-999.md",
                StoreTest.document(
                    "task-20260719-999", "task", "Two\n", status="doing"
                ),
            )
            before = {path: path.read_bytes() for path in root.rglob("*.md")}
            with self.assertRaises(InputError):
                store.plan_task_complete(first.entity_id, first.content_hash)
            self.assertEqual({path: path.read_bytes() for path in before}, before)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(store, "Current", "doing")
            occupied = self.create_task(store, "Occupied", "next")
            baseline = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            with (
                mock.patch("webapp.store.current_time", return_value=self.NOW),
                mock.patch.object(
                    store, "next_entity_id", return_value=occupied.entity_id
                ),
                self.assertRaises(DestinationConflict),
            ):
                store.plan_task_interrupt(current.entity_id, current.content_hash)
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                baseline,
            )

    def test_interrupt_destination_collision_and_faults_rollback_every_effect(self) -> None:
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create_task(
                    store,
                    "Current",
                    "doing",
                    work_started_at="2026-07-19T08:00:00+09:00",
                    resume_status="next",
                )
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_interrupt(current.entity_id, current.content_hash)
                baseline = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }
                original_publish = store._publish_no_replace
                calls = 0

                def fail_publication(path, data, tokens):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(f"fault {fail_at}")
                    return original_publish(path, data, tokens)

                with mock.patch.object(store, "_publish_no_replace", side_effect=fail_publication):
                    with self.assertRaises(OSError):
                        store.apply_task_workflow(plan)
                self.assertEqual(
                    {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*.md")
                    },
                    baseline,
                )
                self.assertEqual(list(root.rglob("*.quarantine")), [])
                self.assertEqual(validate_repository(root), [])

    def test_three_effect_switch_rolls_back_fault_at_each_publication_stage(self) -> None:
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create_task(
                    store,
                    "Current",
                    "doing",
                    work_started_at="2026-07-19T08:00:00+09:00",
                    resume_status="next",
                )
                target = self.create_task(store, "Target", "next")
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=current.entity_id,
                        active_base_hash=current.content_hash,
                        resolution="interrupt",
                    )
                baseline = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }
                original_publish = store._publish_no_replace
                calls = 0

                def fail_publication(path, data, tokens):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(f"fault {fail_at}")
                    return original_publish(path, data, tokens)

                with mock.patch.object(store, "_publish_no_replace", side_effect=fail_publication):
                    with self.assertRaises(OSError):
                        store.apply_task_workflow(plan)
                self.assertEqual(
                    {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*.md")
                    },
                    baseline,
                )
                self.assertEqual(list(root.rglob("*.quarantine")), [])
                self.assertEqual(validate_repository(root), [])

    def test_post_commit_cleanup_failure_never_rolls_back_canonical_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            real_discard = store._discard_reserved_source
            discard_calls = 0
            external_inode: tuple[int, int] | None = None

            def fail_second_cleanup(quarantine, token):
                nonlocal discard_calls, external_inode
                discard_calls += 1
                if discard_calls == 1:
                    real_discard(quarantine, token)
                    return
                current_path = root / plan.effects[0].destination_relative_path
                replacement = current_path.with_name("external-identical.md")
                replacement.write_bytes(plan.effects[0].after_bytes)
                os.replace(replacement, current_path)
                current_stat = os.stat(current_path, follow_symlinks=False)
                external_inode = (current_stat.st_dev, current_stat.st_ino)
                raise OSError("second post-commit quarantine cleanup failed")

            with mock.patch.object(
                store, "_discard_reserved_source", side_effect=fail_second_cleanup
            ):
                with self.assertRaises(WorkflowPostCommitCleanupError) as raised:
                    store.apply_task_workflow(plan)

            self.assertEqual(discard_calls, 2)
            self.assertTrue(hasattr(raised.exception, "committed_entities"))
            self.assertEqual(
                raised.exception.committed_entities,
                tuple(effect.planned_entity for effect in plan.effects),
            )
            for effect in plan.effects:
                self.assertEqual(
                    (root / effect.destination_relative_path).read_bytes(),
                    effect.after_bytes,
                )
            current_stat = os.stat(
                root / plan.effects[0].destination_relative_path,
                follow_symlinks=False,
            )
            self.assertEqual((current_stat.st_dev, current_stat.st_ino), external_inode)
            self.assertEqual(len(list(root.rglob("*.quarantine"))), 1)
            self.assertEqual(validate_repository(root), [])

    def test_post_commit_cleanup_reports_every_alternate_residual_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(store, "Current", "doing")
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            real_safe_unlink = safe_unlink
            failed = False

            def unlink_then_fail_once(path, repository_root):
                nonlocal failed
                real_safe_unlink(path, repository_root)
                if not failed:
                    failed = True
                    raise OSError("post-unlink directory fsync outcome unknown")

            with mock.patch(
                "webapp.store.safe_unlink", side_effect=unlink_then_fail_once
            ):
                with self.assertRaises(WorkflowPostCommitCleanupError) as raised:
                    store.apply_task_workflow(plan)

            self.assertTrue(failed)
            self.assertTrue(hasattr(raised.exception.errors[0], "residual_paths"))
            reported = set(raised.exception.remaining_quarantines)
            typed_reported = {
                path
                for error in raised.exception.errors
                for path in error.residual_paths
            }
            actual = set(root.rglob("*.quarantine"))
            self.assertEqual(reported, actual)
            self.assertEqual(typed_reported, actual)
            self.assertEqual(len(actual), 1)
            self.assertEqual(validate_repository(root), [])

    def test_post_commit_cleanup_reports_nested_post_effect_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            self.assertEqual(len(plan.effects), 3)
            real_safe_unlink = safe_unlink
            real_create = __import__(
                "webapp.store", fromlist=["atomic_create_no_replace"]
            ).atomic_create_no_replace
            cleanup_failed = False
            preservation_candidate: pathlib.Path | None = None

            def unlink_then_fail_once(path, repository_root):
                nonlocal cleanup_failed
                real_safe_unlink(path, repository_root)
                if not cleanup_failed:
                    cleanup_failed = True
                    raise OSError("source quarantine unlink post-effect failure")

            def preserve_then_fail(path, data):
                nonlocal preservation_candidate
                token = real_create(path, data)
                if path.name.endswith(".quarantine"):
                    preservation_candidate = path
                    error = OSError("preservation publication post-effect failure")
                    error.publication_token = token
                    raise error
                return token

            with (
                mock.patch(
                    "webapp.store.safe_unlink", side_effect=unlink_then_fail_once
                ),
                mock.patch(
                    "webapp.store.atomic_create_no_replace",
                    side_effect=preserve_then_fail,
                ),
                self.assertRaises(WorkflowPostCommitCleanupError) as raised,
            ):
                store.apply_task_workflow(plan)

            self.assertTrue(cleanup_failed)
            self.assertIsNotNone(preservation_candidate)
            actual = set(root.rglob("*.quarantine"))
            self.assertEqual(actual, {preservation_candidate})
            self.assertEqual(set(raised.exception.remaining_quarantines), actual)
            self.assertEqual(
                {
                    path
                    for error in raised.exception.errors
                    for path in error.residual_paths
                },
                actual,
            )
            for effect in plan.effects:
                self.assertEqual(
                    (root / effect.destination_relative_path).read_bytes(),
                    effect.after_bytes,
                )
            self.assertEqual(validate_repository(root), [])

    def test_nested_preservation_external_replacement_is_protected_not_owned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            real_safe_unlink = safe_unlink
            real_create = __import__(
                "webapp.store", fromlist=["atomic_create_no_replace"]
            ).atomic_create_no_replace
            cleanup_failed = False
            external_candidate: pathlib.Path | None = None
            external_bytes = b"external quarantine winner\n"

            def unlink_then_fail_once(path, repository_root):
                nonlocal cleanup_failed
                real_safe_unlink(path, repository_root)
                if not cleanup_failed:
                    cleanup_failed = True
                    raise OSError("source quarantine unlink post-effect failure")

            def replace_preservation_then_fail(path, data):
                nonlocal external_candidate
                token = real_create(path, data)
                if path.name.endswith(".quarantine"):
                    external_candidate = path
                    replacement = path.with_name(f"{path.name}.external")
                    replacement.write_bytes(external_bytes)
                    os.replace(replacement, path)
                    error = OSError("preservation publication post-effect failure")
                    error.publication_token = token
                    raise error
                return token

            with (
                mock.patch(
                    "webapp.store.safe_unlink", side_effect=unlink_then_fail_once
                ),
                mock.patch(
                    "webapp.store.atomic_create_no_replace",
                    side_effect=replace_preservation_then_fail,
                ),
                self.assertRaises(WorkflowPostCommitCleanupError) as raised,
            ):
                store.apply_task_workflow(plan)

            self.assertIsNotNone(external_candidate)
            self.assertEqual(external_candidate.read_bytes(), external_bytes)
            self.assertEqual(raised.exception.remaining_quarantines, ())
            self.assertEqual(
                {
                    path
                    for error in raised.exception.errors
                    for path in error.residual_paths
                },
                set(),
            )
            self.assertEqual(
                {
                    path
                    for error in raised.exception.errors
                    for path in error.protected_paths
                },
                {external_candidate},
            )
            self.assertEqual(
                set(raised.exception.protected_paths), {external_candidate}
            )
            for effect in plan.effects:
                self.assertEqual(
                    (root / effect.destination_relative_path).read_bytes(),
                    effect.after_bytes,
                )
            self.assertEqual(validate_repository(root), [])

    def test_nested_preservation_absent_and_unknown_outcomes_are_structured(
        self,
    ) -> None:
        for outcome in ("absent", "unknown"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                original_path = root / "tasks/task-20260719-999.md"
                real_create = __import__(
                    "webapp.store", fromlist=["atomic_create_no_replace"]
                ).atomic_create_no_replace
                candidate: pathlib.Path | None = None

                def publish_then_fail(path, data):
                    nonlocal candidate
                    candidate = path
                    token = real_create(path, data)
                    if outcome == "absent":
                        real_safe_unlink = safe_unlink
                        real_safe_unlink(path, root)
                    error = OSError(f"{outcome} preservation outcome")
                    error.publication_token = token
                    raise error

                state_patch = (
                    mock.patch.object(
                        store,
                        "_publication_path_state",
                        return_value=("unknown", OSError("inspection unknown")),
                    )
                    if outcome == "unknown"
                    else contextlib.nullcontext()
                )
                with (
                    state_patch,
                    mock.patch(
                        "webapp.store.atomic_create_no_replace",
                        side_effect=publish_then_fail,
                    ),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    store._preserve_quarantine_bytes(original_path, b"recovery")

                self.assertIsNotNone(candidate)
                self.assertEqual(raised.exception.protected_paths, ())
                if outcome == "absent":
                    self.assertFalse(candidate.exists())
                    self.assertEqual(raised.exception.uncertain_paths, ())
                else:
                    self.assertTrue(candidate.exists())
                    self.assertEqual(
                        raised.exception.uncertain_paths, (candidate,)
                    )

    def test_rollback_post_effect_cleanup_error_without_winner_restores_all_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(store, "Current", "doing")
            target = self.create_task(store, "Target", "next")
            source_state = {
                root / entity.relative_path: (
                    (root / entity.relative_path).read_bytes(),
                    os.stat(root / entity.relative_path, follow_symlinks=False),
                )
                for entity in (current, target)
            }
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            real_publish = store._publish_no_replace
            real_read = store._read_regular_file_with_token
            real_delete = store._quarantine_delete_owned_path
            publications = 0
            reread_failed = False
            cleanup_failed = False
            reread_failure_path = root / plan.effects[1].destination_relative_path
            cleanup_failure_path = root / plan.effects[0].destination_relative_path

            def mark_publication(path, data, tokens):
                nonlocal publications
                result = real_publish(path, data, tokens)
                publications += 1
                return result

            def fail_final_reread_once(path):
                nonlocal reread_failed
                if publications == 3 and path == reread_failure_path and not reread_failed:
                    reread_failed = True
                    raise OSError("trigger rollback after all publications")
                return real_read(path)

            def delete_owned_then_report_post_effect(path, token, *, ownership_kind):
                nonlocal cleanup_failed
                real_delete(path, token, ownership_kind=ownership_kind)
                if path == cleanup_failure_path and not cleanup_failed:
                    cleanup_failed = True
                    raise OSError("owned publication deleted but post-effect failed")

            with (
                mock.patch.object(
                    store, "_publish_no_replace", side_effect=mark_publication
                ),
                mock.patch.object(
                    store,
                    "_read_regular_file_with_token",
                    side_effect=fail_final_reread_once,
                ),
                mock.patch.object(
                    store,
                    "_quarantine_delete_owned_path",
                    side_effect=delete_owned_then_report_post_effect,
                ),
            ):
                with self.assertRaises(WorkflowRollbackError) as raised:
                    store.apply_task_workflow(plan)

            self.assertTrue(reread_failed)
            self.assertTrue(cleanup_failed)
            self.assertEqual(raised.exception.protected_paths, ())
            self.assertIsInstance(raised.exception.__cause__, OSError)
            for path, (expected_bytes, expected_stat) in source_state.items():
                self.assertTrue(
                    path.exists(),
                    msg={
                        "missing": path,
                        "artifacts": sorted(
                            candidate.relative_to(root).as_posix()
                            for candidate in root.rglob("*")
                        ),
                        "errors": [str(error) for error in raised.exception.errors],
                    },
                )
                self.assertEqual(path.read_bytes(), expected_bytes)
                actual_stat = os.stat(path, follow_symlinks=False)
                self.assertEqual(
                    (actual_stat.st_dev, actual_stat.st_ino),
                    (expected_stat.st_dev, expected_stat.st_ino),
                )
            self.assertFalse((root / plan.effects[1].destination_relative_path).exists())
            self.assertEqual(list(root.rglob("*.quarantine")), [])
            self.assertEqual(validate_repository(root), [])

    def test_rollback_ownership_mismatch_protects_winner_and_restores_other_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(
                store,
                "Current",
                "doing",
                work_started_at="2026-07-19T08:00:00+09:00",
                resume_status="next",
            )
            target = self.create_task(store, "Target", "next")
            current_before = (root / current.relative_path).read_bytes()
            target_before = (root / target.relative_path).read_bytes()
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            real_snapshot = store._repository_document_snapshot
            snapshot_calls = 0
            winner_inode: tuple[int, int] | None = None

            def install_winner_during_post_publish_refresh():
                nonlocal snapshot_calls, winner_inode
                documents, generation = real_snapshot()
                snapshot_calls += 1
                if snapshot_calls == 2:
                    path = root / plan.effects[0].destination_relative_path
                    replacement = path.with_name("external-winner.md")
                    replacement.write_bytes(plan.effects[0].after_bytes)
                    os.replace(replacement, path)
                    result = os.stat(path, follow_symlinks=False)
                    winner_inode = (result.st_dev, result.st_ino)
                return documents, generation

            with mock.patch.object(
                store,
                "_repository_document_snapshot",
                side_effect=install_winner_during_post_publish_refresh,
            ):
                with self.assertRaises(WorkflowRollbackError) as raised:
                    store.apply_task_workflow(plan)

            self.assertTrue(hasattr(raised.exception, "errors"))
            self.assertGreaterEqual(len(raised.exception.errors), 2)
            self.assertIsInstance(raised.exception.__cause__, MutationPlanConflict)
            winner_path = root / plan.effects[0].destination_relative_path
            winner_stat = os.stat(winner_path, follow_symlinks=False)
            self.assertEqual((winner_stat.st_dev, winner_stat.st_ino), winner_inode)
            self.assertEqual(winner_path.read_bytes(), plan.effects[0].after_bytes)
            self.assertEqual((root / target.relative_path).read_bytes(), target_before)
            self.assertNotEqual(winner_path.read_bytes(), current_before)
            self.assertFalse((root / plan.effects[1].destination_relative_path).exists())
            quarantines = list(root.rglob("*.quarantine"))
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), current_before)
            self.assertEqual(validate_repository(root), [])

    def test_workflow_preview_rejects_supported_root_symlinks_without_reading_target(
        self,
    ) -> None:
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                target = self.create_task(store, "Target", "next")
                if kind == "file":
                    outside = pathlib.Path(temporary) / "outside.md"
                    outside.write_bytes((root / target.relative_path).read_bytes())
                    symlink = root / "tasks/duplicate-via-symlink.md"
                else:
                    outside = pathlib.Path(temporary) / "outside-directory"
                    outside.mkdir()
                    (outside / "duplicate.md").write_bytes(
                        (root / target.relative_path).read_bytes()
                    )
                    symlink = root / "tasks/duplicate-directory"
                symlink.symlink_to(outside, target_is_directory=kind == "directory")

                with self.assertRaises(InputError):
                    store.plan_task_start(target.entity_id, target.content_hash)

                self.assertTrue(symlink.is_symlink())

    def test_workflow_apply_rejects_symlink_added_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(target.entity_id, target.content_hash)
            before = (root / target.relative_path).read_bytes()
            (root / "tasks/late-symlink.md").symlink_to(
                pathlib.Path(temporary) / "missing-target.md"
            )

            with self.assertRaises(InputError):
                store.apply_task_workflow(plan)

            self.assertEqual((root / target.relative_path).read_bytes(), before)

    def test_three_effect_reservation_failures_restore_every_source(self) -> None:
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create_task(store, "Current", "doing")
                target = self.create_task(store, "Target", "next")
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=current.entity_id,
                        active_base_hash=current.content_hash,
                        resolution="interrupt",
                    )
                baseline = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }
                real_reserve = store._reserve_owned_source_for_replace
                calls = 0

                def fail_reservation(path, token):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(f"reservation fault {fail_at}")
                    return real_reserve(path, token)

                with mock.patch.object(
                    store,
                    "_reserve_owned_source_for_replace",
                    side_effect=fail_reservation,
                ):
                    with self.assertRaises(OSError):
                        store.apply_task_workflow(plan)
                self.assertEqual(
                    {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*.md")
                    },
                    baseline,
                )
                self.assertEqual(list(root.rglob("*.quarantine")), [])

    def test_three_effect_post_effect_publication_faults_restore_every_source(
        self,
    ) -> None:
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create_task(store, "Current", "doing")
                target = self.create_task(store, "Target", "next")
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=current.entity_id,
                        active_base_hash=current.content_hash,
                        resolution="interrupt",
                    )
                baseline = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }
                real_create = __import__(
                    "webapp.store", fromlist=["atomic_create_no_replace"]
                ).atomic_create_no_replace
                calls = 0

                def publish_then_fail(path, data):
                    nonlocal calls
                    calls += 1
                    token = real_create(path, data)
                    if calls == fail_at:
                        error = OSError(f"post-effect publication fault {fail_at}")
                        error.publication_token = token
                        raise error
                    return token

                with mock.patch(
                    "webapp.store.atomic_create_no_replace",
                    side_effect=publish_then_fail,
                ):
                    with self.assertRaises(OSError):
                        store.apply_task_workflow(plan)
                self.assertEqual(
                    {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*.md")
                    },
                    baseline,
                )
                self.assertEqual(list(root.rglob("*.quarantine")), [])

    def test_three_effect_final_reread_faults_restore_every_source(self) -> None:
        for fail_at in (0, 1, 2):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                current = self.create_task(store, "Current", "doing")
                target = self.create_task(store, "Target", "next")
                with mock.patch("webapp.store.current_time", return_value=self.NOW):
                    plan = store.plan_task_start(
                        target.entity_id,
                        target.content_hash,
                        active_entity_id=current.entity_id,
                        active_base_hash=current.content_hash,
                        resolution="interrupt",
                    )
                baseline = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }
                real_publish = store._publish_no_replace
                real_read = store._read_regular_file_with_token
                publication_calls = 0
                failed = False

                def mark_publications(path, data, tokens):
                    nonlocal publication_calls
                    result = real_publish(path, data, tokens)
                    publication_calls += 1
                    return result

                failure_path = root / plan.effects[fail_at].destination_relative_path

                def fail_selected_final_reread(path):
                    nonlocal failed
                    if publication_calls == 3 and path == failure_path and not failed:
                        failed = True
                        raise OSError(f"final reread fault {fail_at}")
                    return real_read(path)

                with (
                    mock.patch.object(
                        store, "_publish_no_replace", side_effect=mark_publications
                    ),
                    mock.patch.object(
                        store,
                        "_read_regular_file_with_token",
                        side_effect=fail_selected_final_reread,
                    ),
                ):
                    with self.assertRaises(OSError):
                        store.apply_task_workflow(plan)
                self.assertTrue(failed)
                self.assertEqual(
                    {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*.md")
                    },
                    baseline,
                )
                self.assertEqual(list(root.rglob("*.quarantine")), [])

    def test_three_effect_post_validation_failure_restores_every_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            current = self.create_task(store, "Current", "doing")
            target = self.create_task(store, "Target", "next")
            with mock.patch("webapp.store.current_time", return_value=self.NOW):
                plan = store.plan_task_start(
                    target.entity_id,
                    target.content_hash,
                    active_entity_id=current.entity_id,
                    active_base_hash=current.content_hash,
                    resolution="interrupt",
                )
            baseline = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }
            real_snapshot = store._repository_document_snapshot
            calls = 0

            def inject_post_validation_error():
                nonlocal calls
                calls += 1
                documents, generation = real_snapshot()
                if calls == 2:
                    documents["tasks/injected.md"] = b"invalid\n"
                return documents, generation

            with mock.patch.object(
                store,
                "_repository_document_snapshot",
                side_effect=inject_post_validation_error,
            ):
                with self.assertRaises(SchemaError):
                    store.apply_task_workflow(plan)
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                },
                baseline,
            )
            self.assertEqual(list(root.rglob("*.quarantine")), [])

    def test_recovery_state_persists_danger_across_store_restart_and_gates_all_writes(
        self,
    ) -> None:
        for dangerous_error in (
            WorkflowPostCommitCleanupError((), (OSError("cleanup"),), ()),
            WorkflowRollbackError((OSError("rollback"),), ()),
        ):
            with self.subTest(error=type(dangerous_error).__name__), tempfile.TemporaryDirectory() as temporary:
                root = self.make_repository(pathlib.Path(temporary))
                store = Store(root)
                target = self.create_task(store, "Target", "next")
                workflow = store.plan_task_start(target.entity_id, target.content_hash)
                legacy_plan = store.plan_create_entity(
                    "goal", {"title": "Blocked", "status": "active"}, ""
                )
                with mock.patch.object(
                    store,
                    "_apply_task_workflow_locked",
                    side_effect=dangerous_error,
                ):
                    with self.assertRaises(type(dangerous_error)):
                        store.apply_task_workflow(workflow)

                state_path = root / ".webapp-mutation-state"
                self.assertEqual(state_path.read_bytes(), b"armed\n")
                self.assertFalse(state_path.is_symlink())
                self.assertEqual(validate_repository(root), [])

                restarted = Store(root)
                with self.assertRaises(MutationRecoveryRequired):
                    restarted.plan_create_entity(
                        "goal", {"title": "Blocked preview", "status": "active"}, ""
                    )
                with self.assertRaises(MutationRecoveryRequired):
                    restarted.apply_mutation_plan(legacy_plan)
                with self.assertRaises(MutationRecoveryRequired):
                    restarted.create_entity(
                        "goal", {"title": "Also blocked", "status": "active"}, ""
                    )

                state_path.write_bytes(b"idle\n")
                recovered_plan = restarted.plan_create_entity(
                    "goal", {"title": "Blocked", "status": "active"}, ""
                )
                applied = restarted.apply_mutation_plan(recovered_plan)
                self.assertEqual(applied.frontmatter["title"], "Blocked")

    def test_workflow_safe_failure_transitions_recovery_state_to_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            workflow = store.plan_task_start(target.entity_id, target.content_hash)
            with mock.patch.object(
                store,
                "_apply_task_workflow_locked",
                side_effect=OSError("safe pre-commit failure"),
            ):
                with self.assertRaises(OSError):
                    store.apply_task_workflow(workflow)
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"idle\n"
            )
            with mock.patch.object(
                store,
                "mutation_lock",
                side_effect=StoreLockTimeout("live writer"),
            ):
                with self.assertRaises(StoreLockTimeout):
                    store.mutation_recovery_required()

    def test_recovery_state_atomic_transition_faults_are_safely_classified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            before = (root / target.relative_path).read_bytes()
            workflow = store.plan_task_start(target.entity_id, target.content_hash)
            with mock.patch("webapp.store.atomic_write", side_effect=OSError("arm failed")):
                with self.assertRaises(OSError):
                    store.apply_task_workflow(workflow)
            self.assertEqual((root / target.relative_path).read_bytes(), before)
            self.assertFalse((root / ".webapp-mutation-state").exists())

            real_atomic_write = atomic_write
            armed_post_effect_reported = False

            def report_after_armed_transition(path, data):
                nonlocal armed_post_effect_reported
                real_atomic_write(path, data)
                if data == b"armed\n" and not armed_post_effect_reported:
                    armed_post_effect_reported = True
                    raise OSError("arm post-effect report")

            with mock.patch(
                "webapp.store.atomic_write", side_effect=report_after_armed_transition
            ):
                with self.assertRaises(MutationRecoveryRequired):
                    store.apply_task_workflow(workflow)
            self.assertEqual((root / target.relative_path).read_bytes(), before)
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"armed\n"
            )
            self.assertTrue(store.mutation_recovery_required())

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            workflow = store.plan_task_start(target.entity_id, target.content_hash)

            def fail_idle_transition(path, data):
                if data == b"idle\n":
                    raise OSError("idle transition failed")
                return atomic_write(path, data)

            with mock.patch(
                "webapp.store.atomic_write", side_effect=fail_idle_transition
            ):
                with self.assertRaises(WorkflowPostCommitCleanupError) as raised:
                    store.apply_task_workflow(workflow)
            self.assertEqual(len(raised.exception.committed_entities), 1)
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"armed\n"
            )
            self.assertEqual(
                store.get_entity(target.entity_id).frontmatter["status"], "doing"
            )
            with self.assertRaises(MutationRecoveryRequired):
                Store(root).plan_create_entity(
                    "goal", {"title": "Blocked", "status": "active"}, ""
                )

    def test_untrusted_recovery_state_path_is_never_followed_or_auto_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            outside = pathlib.Path(temporary) / "outside-secret"
            outside.write_text("do not read", encoding="utf-8")
            state_path = root / ".webapp-mutation-state"
            state_path.symlink_to(outside)
            store = Store(root)
            self.assertTrue(store.mutation_recovery_required())
            with self.assertRaises(MutationRecoveryRequired):
                store.plan_create_entity(
                    "goal", {"title": "Blocked", "status": "active"}, ""
                )
            self.assertTrue(state_path.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not read")

    def test_arm_durability_failure_never_reaches_a_canonical_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            before = (root / target.relative_path).read_bytes()
            workflow = store.plan_task_start(target.entity_id, target.content_hash)
            real_fsync = os.fsync
            root_directory_fsyncs: list[int] = []

            def fail_arm_parent_durability(descriptor: int) -> None:
                descriptor_path = pathlib.Path(
                    f"/proc/self/fd/{descriptor}"
                ).resolve(strict=True)
                if (
                    stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    and descriptor_path == root
                ):
                    root_directory_fsyncs.append(descriptor)
                    if len(root_directory_fsyncs) <= 2:
                        raise OSError("arm parent durability unknown")
                real_fsync(descriptor)

            with mock.patch(
                "webapp.store.os.fsync", side_effect=fail_arm_parent_durability
            ):
                with self.assertRaises(MutationRecoveryRequired):
                    store.apply_task_workflow(workflow)

            self.assertEqual((root / target.relative_path).read_bytes(), before)
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"armed\n"
            )
            self.assertEqual(len(root_directory_fsyncs), 2)
            self.assertEqual(root_directory_fsyncs[0], root_directory_fsyncs[1])
            self.assertTrue(store.mutation_recovery_required())

    def test_clear_durability_failure_is_committed_cleanup_and_rearms_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            workflow = store.plan_task_start(target.entity_id, target.content_hash)
            real_fsync = os.fsync
            root_directory_fsyncs: list[int] = []

            def fail_clear_parent_durability(descriptor: int) -> None:
                descriptor_path = pathlib.Path(
                    f"/proc/self/fd/{descriptor}"
                ).resolve(strict=True)
                if (
                    stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    and descriptor_path == root
                ):
                    root_directory_fsyncs.append(descriptor)
                    if len(root_directory_fsyncs) in {2, 3}:
                        raise OSError("clear parent durability unknown")
                real_fsync(descriptor)

            with mock.patch(
                "webapp.store.os.fsync", side_effect=fail_clear_parent_durability
            ):
                with self.assertRaises(WorkflowPostCommitCleanupError) as raised:
                    store.apply_task_workflow(workflow)

            self.assertEqual(len(raised.exception.committed_entities), 1)
            self.assertEqual(
                store.get_entity(target.entity_id).frontmatter["status"], "doing"
            )
            self.assertGreaterEqual(len(root_directory_fsyncs), 4)
            self.assertEqual(root_directory_fsyncs[1], root_directory_fsyncs[2])
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"armed\n"
            )
            self.assertTrue(store.mutation_recovery_required())

    def test_every_rollback_exception_is_normalized_and_keeps_state_armed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            workflow = store.plan_task_start(target.entity_id, target.content_hash)
            with (
                mock.patch.object(
                    store, "_publish_no_replace", side_effect=OSError("publish")
                ),
                mock.patch.object(
                    store,
                    "_rollback_task_workflow",
                    side_effect=ValueError("unexpected rollback exception"),
                ),
            ):
                with self.assertRaises(WorkflowRollbackError) as raised:
                    store.apply_task_workflow(workflow)
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertNotIn(str(root), str(raised.exception))
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"armed\n"
            )
            with self.assertRaises(MutationRecoveryRequired):
                Store(root).plan_create_entity(
                    "goal", {"title": "Blocked", "status": "active"}, ""
                )

    def test_recovery_snapshot_waits_for_inflight_workflow_and_reports_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_repository(pathlib.Path(temporary))
            store = Store(root)
            target = self.create_task(store, "Target", "next")
            workflow = store.plan_task_start(target.entity_id, target.content_hash)
            read_snapshot = getattr(store, "read_snapshot", None)
            self.assertIsNotNone(read_snapshot)
            entered = threading.Event()
            release = threading.Event()
            real_apply = store._apply_task_workflow_locked

            def pause_after_arm(plan):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return real_apply(plan)

            apply_errors: list[BaseException] = []
            observed: list[object] = []

            def apply_workflow():
                try:
                    store.apply_task_workflow(workflow)
                except BaseException as error:
                    apply_errors.append(error)

            with mock.patch.object(
                store, "_apply_task_workflow_locked", side_effect=pause_after_arm
            ):
                apply_thread = threading.Thread(target=apply_workflow)
                apply_thread.start()
                self.assertTrue(entered.wait(2.0))
                snapshot_thread = threading.Thread(
                    target=lambda: observed.append(
                        read_snapshot()
                    )
                )
                snapshot_thread.start()
                time.sleep(0.05)
                self.assertTrue(snapshot_thread.is_alive())
                release.set()
                apply_thread.join(2.0)
                snapshot_thread.join(2.0)
            self.assertEqual(apply_errors, [])
            self.assertEqual(len(observed), 1)
            snapshot = observed[0]
            self.assertFalse(snapshot.recovery_required)
            self.assertEqual(
                [
                    entity.frontmatter["status"]
                    for entity in snapshot.entities
                    if entity.entity_id == target.entity_id
                ],
                ["doing"],
            )
            self.assertEqual(
                (root / ".webapp-mutation-state").read_bytes(), b"idle\n"
            )


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "O_NOFOLLOW")
    and pathlib.Path("/proc/self/fd").is_dir(),
    "secure mutations require Linux /proc and O_NOFOLLOW",
)
class StoreRoadmapWorkflowTest(unittest.TestCase):
    FIXED_NOW = datetime.datetime(
        2026, 8, 25, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
    )

    def make_store(self, base: pathlib.Path) -> Store:
        root = base / "repository"
        for directory in (
            "inbox", "tasks", "purposes", "visions", "areas", "projects",
            "goals", "roadmap-outcomes", "cycles", "reviews/daily",
            "reviews/weekly", "archive",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        return Store(root)

    def create(self, store: Store, entity_type: str, fields: dict[str, str], body: str = "Body"):
        with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
            plan = store.plan_create_entity(entity_type, fields, body)
        return store.apply_mutation_plan(plan)

    def create_goal(self, store: Store):
        return self.create(store, "goal", {"title": "Goal"})

    def create_outcome(self, store: Store, goal_id: str, title: str, lane: str, position: int):
        return self.create(
            store,
            "roadmap_outcome",
            {
                "title": title,
                "goal_id": goal_id,
                "roadmap_lane": lane,
                "roadmap_position": str(position),
            },
            "## 成功条件\n\n受入確認済み\n\n## メモ\n",
        )

    def create_cycle(self, store: Store, outcome_ids: list[str], *, start: str = "2026-08-25", end: str = "2026-10-05"):
        return self.create(
            store,
            "cycle",
            {
                "title": "6週間Cycle",
                "start_date": start,
                "end_date": end,
                "outcome_ids": "[" + ", ".join(outcome_ids) + "]",
            },
            "## 振り返り\n\n## メモ\n",
        )

    def test_roadmap_entities_use_canonical_crud_and_project_direction_exclusion(self) -> None:
        """Catches missing CRUD kinds, wrong directories, or ambiguous Project links."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            cycle = self.create_cycle(store, [outcome.entity_id])
            self.assertEqual(outcome.relative_path, f"roadmap-outcomes/{outcome.entity_id}.md")
            self.assertEqual(cycle.relative_path, f"cycles/{cycle.entity_id}.md")
            with self.assertRaises(InputError):
                self.create(
                    store,
                    "project",
                    {
                        "title": "Ambiguous",
                        "goal_id": goal.entity_id,
                        "roadmap_outcome_id": outcome.entity_id,
                    },
                )
            self.assertEqual(validate_repository(store._root), [])

    def test_roadmap_crud_preview_rejects_missing_required_body_headings(self) -> None:
        """Catches a preview claiming validity before repository-wide apply rollback."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            with self.assertRaises(SchemaError):
                store.plan_create_entity(
                    "roadmap_outcome",
                    {
                        "title": "Outcome",
                        "goal_id": goal.entity_id,
                        "roadmap_lane": "next",
                        "roadmap_position": "1",
                    },
                    "見出しなし",
                )
            with self.assertRaises(SchemaError):
                store.plan_create_entity(
                    "cycle",
                    {
                        "title": "Cycle",
                        "start_date": "2026-08-25",
                        "end_date": "2026-10-05",
                        "outcome_ids": "[]",
                    },
                    "見出しなし",
                )

    def test_first_roadmap_entity_create_initializes_only_its_canonical_directory(self) -> None:
        """Catches first-use create failing because empty entity directories are not tracked."""
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "repository"
            for directory in ("goals", "archive"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            store = Store(root)
            goal = self.create_goal(store)

            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)

            self.assertTrue((root / "roadmap-outcomes" / f"{outcome.entity_id}.md").is_file())
            self.assertFalse((root / "cycles").exists())

    def test_move_and_activate_are_exact_atomic_multi_entity_workflows(self) -> None:
        """Catches partial lane reorder or activation that leaves member lane fields."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            first = self.create_outcome(store, goal.entity_id, "First", "next", 1)
            second = self.create_outcome(store, goal.entity_id, "Second", "next", 2)
            cycle = self.create_cycle(store, [second.entity_id])

            move = store.plan_roadmap_move(second.entity_id, second.content_hash, "next", 1)
            self.assertEqual([effect.entity_id for effect in move.effects], [second.entity_id, first.entity_id])
            moved = store.apply_task_workflow(move)
            self.assertEqual([entity.frontmatter["roadmap_position"] for entity in moved], ["1", "2"])

            current_cycle = store.get_entity(cycle.entity_id)
            current_second = store.get_entity(second.entity_id)
            activate = store.plan_cycle_activate(current_cycle.entity_id, current_cycle.content_hash)
            self.assertEqual(
                [effect.role for effect in activate.effects[:2]],
                ["cycle_activated", "outcome_now"],
            )
            activated = store.apply_task_workflow(activate)
            self.assertEqual(activated[0].frontmatter["status"], "active")
            self.assertNotIn("roadmap_lane", activated[1].frontmatter)
            self.assertNotIn("roadmap_position", activated[1].frontmatter)
            self.assertEqual(validate_repository(store._root), [])

    def test_activation_rejects_an_externally_introduced_period_overlap(self) -> None:
        """Catches activation trusting pre-existing validation debt for overlapping dates."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            first = self.create_cycle(store, [], start="2026-08-25", end="2026-10-05")
            second = self.create_cycle(store, [], start="2026-10-06", end="2026-11-16")
            path = store._root / second.relative_path
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "start_date: 2026-10-06", "start_date: 2026-09-01"
                ),
                encoding="utf-8",
            )
            current = store.get_entity(first.entity_id)

            with self.assertRaises(RoadmapOperationError) as raised:
                store.plan_cycle_activate(current.entity_id, current.content_hash)

            self.assertEqual(raised.exception.code, "cycle_period_overlap")

    def test_close_carried_appends_planned_cycle_and_restores_next_tail(self) -> None:
        """Catches carried being treated as a new Cycle or not restored to Next."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            now = self.create_outcome(store, goal.entity_id, "Now", "next", 1)
            tail = self.create_outcome(store, goal.entity_id, "Tail", "next", 2)
            active_cycle = self.create_cycle(store, [now.entity_id])
            store.apply_task_workflow(
                store.plan_cycle_activate(active_cycle.entity_id, active_cycle.content_hash)
            )
            carryover = self.create_cycle(
                store, [], start="2026-10-06", end="2026-11-16"
            )
            current_cycle = store.get_entity(active_cycle.entity_id)
            close = store.plan_cycle_close(
                current_cycle.entity_id,
                current_cycle.content_hash,
                status="completed",
                outcome_results={now.entity_id: "carried"},
                retrospective="継続する価値がある。",
                carryover_cycle_id=carryover.entity_id,
            )
            self.assertEqual(
                [effect.role for effect in close.effects],
                ["cycle_closed", "outcome_carried", "carryover_cycle_updated"],
            )
            store.apply_task_workflow(close)
            closed = store.get_entity(active_cycle.entity_id)
            carried = store.get_entity(now.entity_id)
            planned = store.get_entity(carryover.entity_id)
            self.assertEqual(closed.frontmatter["carried_outcome_ids"], f"[{now.entity_id}]")
            self.assertEqual(carried.frontmatter["roadmap_lane"], "next")
            self.assertEqual(carried.frontmatter["roadmap_position"], "2")
            self.assertEqual(planned.frontmatter["outcome_ids"], f"[{now.entity_id}]")
            self.assertEqual(store.get_entity(tail.entity_id).frontmatter["roadmap_position"], "1")
            self.assertEqual(validate_repository(store._root), [])

    def test_carried_outcome_can_activate_and_finish_in_the_carryover_cycle(self) -> None:
        """Catches historical carry metadata blocking the next Cycle lifecycle."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            first = self.create_cycle(store, [outcome.entity_id])
            store.apply_task_workflow(
                store.plan_cycle_activate(first.entity_id, first.content_hash)
            )
            second = self.create_cycle(
                store, [], start="2026-10-06", end="2026-11-16"
            )

            current_first = store.get_entity(first.entity_id)
            store.apply_task_workflow(
                store.plan_cycle_close(
                    current_first.entity_id,
                    current_first.content_hash,
                    status="completed",
                    outcome_results={outcome.entity_id: "carried"},
                    retrospective="次Cycleへ継続する。",
                    carryover_cycle_id=second.entity_id,
                )
            )
            current_second = store.get_entity(second.entity_id)
            store.apply_task_workflow(
                store.plan_cycle_activate(
                    current_second.entity_id, current_second.content_hash
                )
            )
            current_second = store.get_entity(second.entity_id)
            store.apply_task_workflow(
                store.plan_cycle_close(
                    current_second.entity_id,
                    current_second.content_hash,
                    status="completed",
                    outcome_results={outcome.entity_id: "achieved"},
                    retrospective="成果を達成した。",
                )
            )

            first_history = store.get_entity(first.entity_id)
            second_history = store.get_entity(second.entity_id)
            finished = store.get_entity(outcome.entity_id)
            self.assertEqual(
                first_history.frontmatter["carried_outcome_ids"],
                f"[{outcome.entity_id}]",
            )
            self.assertEqual(first_history.frontmatter["carryover_cycle_id"], second.entity_id)
            self.assertEqual(second_history.frontmatter["status"], "completed")
            self.assertEqual(
                second_history.frontmatter["achieved_outcome_ids"],
                f"[{outcome.entity_id}]",
            )
            self.assertEqual(finished.frontmatter["status"], "achieved")
            self.assertEqual(validate_repository(store._root), [])

    def test_ordinary_planned_cycle_crud_rejects_a_current_now_outcome(self) -> None:
        """Catches create/update bypassing the planned-member lane invariant."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Now", "next", 1)
            active = self.create_cycle(store, [outcome.entity_id])
            planned = self.create_cycle(
                store, [], start="2026-10-06", end="2026-11-16"
            )
            store.apply_task_workflow(
                store.plan_cycle_activate(active.entity_id, active.content_hash)
            )

            current_planned = store.get_entity(planned.entity_id)
            with self.assertRaises(RoadmapOperationError) as update_error:
                store.plan_update_entity(
                    current_planned.entity_id,
                    current_planned.content_hash,
                    {"outcome_ids": f"[{outcome.entity_id}]"},
                    None,
                )
            self.assertEqual(update_error.exception.code, "roadmap_operation_invalid")

            with self.assertRaises(RoadmapOperationError) as create_error:
                store.plan_create_entity(
                    "cycle",
                    {
                        "title": "Future Cycle",
                        "start_date": "2026-11-17",
                        "end_date": "2026-12-28",
                        "outcome_ids": f"[{outcome.entity_id}]",
                    },
                    "## 振り返り\n\n## メモ\n",
                )
            self.assertEqual(create_error.exception.code, "roadmap_operation_invalid")

    def test_close_preserves_cycle_memo_and_requires_exact_optional_fields(self) -> None:
        """Catches close erasing notes or accepting optional keys with empty values."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Now", "next", 1)
            cycle = self.create(
                store,
                "cycle",
                {
                    "title": "Cycle",
                    "start_date": "2026-08-25",
                    "end_date": "2026-10-05",
                    "outcome_ids": f"[{outcome.entity_id}]",
                },
                "## 振り返り\n\n## メモ\n\n消してはいけない前提\n",
            )
            store.apply_task_workflow(
                store.plan_cycle_activate(cycle.entity_id, cycle.content_hash)
            )
            current = store.get_entity(cycle.entity_id)
            with self.assertRaises(RoadmapOperationError):
                store.plan_cycle_close(
                    current.entity_id,
                    current.content_hash,
                    status="completed",
                    outcome_results={outcome.entity_id: "next"},
                    retrospective="確認した。",
                    cancellation_reason="",
                )
            with self.assertRaises(RoadmapOperationError):
                store.plan_cycle_close(
                    current.entity_id,
                    current.content_hash,
                    status="completed",
                    outcome_results={outcome.entity_id: "next"},
                    retrospective="確認した。",
                    carryover_cycle_id="",
                )
            plan = store.plan_cycle_close(
                current.entity_id,
                current.content_hash,
                status="completed",
                outcome_results={outcome.entity_id: "next"},
                retrospective="確認した。",
            )
            result = store.apply_task_workflow(plan)[0]
            self.assertIn("消してはいけない前提", result.body)

    def test_roadmap_workflow_publication_failure_rolls_back_every_effect(self) -> None:
        """Catches a mid-publication failure leaving a partially reordered lane."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            first = self.create_outcome(store, goal.entity_id, "First", "next", 1)
            second = self.create_outcome(store, goal.entity_id, "Second", "next", 2)
            before = {entity.entity_id: entity.content_hash for entity in (first, second)}
            plan = store.plan_roadmap_move(second.entity_id, second.content_hash, "next", 1)
            real_publish = store._publish_no_replace
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publication failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(store, "_publish_no_replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    store.apply_task_workflow(plan)

            self.assertEqual(
                {entity_id: store.get_entity(entity_id).content_hash for entity_id in before},
                before,
            )
            self.assertFalse(store.mutation_recovery_required())

    def test_roadmap_bundle_previews_and_applies_every_project_change_as_exact_effects(self) -> None:
        """Catches bundle changes being split into partial independent mutations."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            destination = self.create_outcome(
                store, goal.entity_id, "Destination", "later", 1
            )
            updated = self.create(
                store,
                "project",
                {
                    "title": "Update me",
                    "roadmap_outcome_id": outcome.entity_id,
                },
            )
            moved = self.create(
                store,
                "project",
                {
                    "title": "Move me",
                    "roadmap_outcome_id": destination.entity_id,
                },
            )
            archived = self.create(
                store,
                "project",
                {
                    "title": "Archive me",
                    "roadmap_outcome_id": outcome.entity_id,
                },
            )
            child = self.create(
                store,
                "task",
                {"title": "Archive child", "project_id": archived.entity_id},
            )

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                plan = store.plan_roadmap_outcome_bundle_update(
                    outcome.entity_id,
                    outcome.content_hash,
                    {"title": "Updated Outcome"},
                    None,
                    [
                        {
                            "action": "create",
                            "fields": {
                                "title": "Created",
                                "roadmap_outcome_id": outcome.entity_id,
                                "planned_start_date": "2026-09-01",
                                "planned_end_date": "2026-09-30",
                            },
                            "body": "Created body",
                        },
                        {
                            "action": "update",
                            "id": updated.entity_id,
                            "base_hash": updated.content_hash,
                            "fields": {"title": "Updated Project"},
                            "body": None,
                        },
                        {
                            "action": "move",
                            "id": moved.entity_id,
                            "base_hash": moved.content_hash,
                        },
                        {
                            "action": "archive",
                            "id": archived.entity_id,
                            "base_hash": archived.content_hash,
                            "task_cascade": [
                                {"id": child.entity_id, "base_hash": child.content_hash}
                            ],
                        },
                    ],
                )

            self.assertEqual(
                [effect.role for effect in plan.effects],
                [
                    "outcome_updated",
                    "project_created",
                    "project_updated",
                    "project_moved",
                    "task_archived",
                    "project_archived",
                ],
            )
            self.assertEqual(plan.effects[0].before_entity, outcome)
            self.assertIsNone(plan.effects[1].before_entity)
            self.assertEqual(plan.effects[-2].before_entity, child)
            self.assertEqual(plan.effects[-1].before_entity, archived)

            results = store.apply_task_workflow(plan)

            self.assertEqual([result.entity_id for result in results], [
                effect.entity_id for effect in plan.effects
            ])
            self.assertEqual(
                store.get_entity(outcome.entity_id).frontmatter["title"],
                "Updated Outcome",
            )
            self.assertEqual(
                store.get_entity(updated.entity_id).frontmatter["title"],
                "Updated Project",
            )
            self.assertEqual(
                results[1].frontmatter["planned_start_date"], "2026-09-01"
            )
            self.assertEqual(
                results[1].frontmatter["planned_end_date"], "2026-09-30"
            )
            self.assertEqual(
                store.get_entity(moved.entity_id).frontmatter["roadmap_outcome_id"],
                outcome.entity_id,
            )
            self.assertTrue(store.get_entity(child.entity_id).relative_path.startswith("archive/"))
            self.assertTrue(store.get_entity(archived.entity_id).relative_path.startswith("archive/"))
            self.assertEqual(validate_repository(store._root), [])

    def test_roadmap_bundle_status_change_appends_project_to_destination_lane(self) -> None:
        """Catches a bundle reusing the source position inside the destination lane."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            target = self.create(
                store,
                "project",
                {"title": "Target", "roadmap_outcome_id": outcome.entity_id},
            )
            first = self.create(
                store,
                "project",
                {
                    "title": "First doing",
                    "status": "doing",
                    "roadmap_outcome_id": outcome.entity_id,
                },
            )
            second = self.create(
                store,
                "project",
                {
                    "title": "Second doing",
                    "status": "doing",
                    "roadmap_outcome_id": outcome.entity_id,
                },
            )

            with mock.patch("webapp.store.current_time", return_value=self.FIXED_NOW):
                plan = store.plan_roadmap_outcome_bundle_update(
                    outcome.entity_id,
                    outcome.content_hash,
                    {},
                    None,
                    [
                        {
                            "action": "update",
                            "id": target.entity_id,
                            "base_hash": target.content_hash,
                            "fields": {"status": "doing"},
                            "body": None,
                        }
                    ],
                )
            store.apply_task_workflow(plan)

            doing = [
                entity
                for entity in store.list_entities()
                if entity.entity_type == "project"
                and entity.frontmatter.get("status") == "doing"
            ]
            doing.sort(key=lambda entity: int(entity.frontmatter["kanban_position"]))
            self.assertEqual(
                [(entity.entity_id, entity.frontmatter["kanban_position"]) for entity in doing],
                [
                    (first.entity_id, "1"),
                    (second.entity_id, "2"),
                    (target.entity_id, "3"),
                ],
            )
            self.assertEqual(validate_repository(store._root), [])

    def test_roadmap_bundle_rejects_omitted_or_extra_project_children(self) -> None:
        """Catches Project archive inferring, omitting, or widening its Task cascade."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            project = self.create(
                store,
                "project",
                {"title": "Project", "roadmap_outcome_id": outcome.entity_id},
            )
            child = self.create(
                store,
                "task",
                {"title": "Child", "project_id": project.entity_id},
            )
            unrelated = self.create(store, "task", {"title": "Unrelated"})
            before = {
                entity.entity_id: entity.content_hash
                for entity in (outcome, project, child, unrelated)
            }

            for cascade in (
                [],
                [
                    {"id": child.entity_id, "base_hash": child.content_hash},
                    {"id": unrelated.entity_id, "base_hash": unrelated.content_hash},
                ],
            ):
                with self.subTest(cascade=cascade):
                    with self.assertRaises(RoadmapOperationError) as raised:
                        store.plan_roadmap_outcome_bundle_update(
                            outcome.entity_id,
                            outcome.content_hash,
                            {},
                            None,
                            [
                                {
                                    "action": "archive",
                                    "id": project.entity_id,
                                    "base_hash": project.content_hash,
                                    "task_cascade": cascade,
                                }
                            ],
                        )
                    self.assertEqual(
                        raised.exception.code, "roadmap_project_cascade_invalid"
                    )
            self.assertEqual(
                {
                    entity_id: store.get_entity(entity_id).content_hash
                    for entity_id in before
                },
                before,
            )

    def test_roadmap_bundle_stale_project_hash_preflight_leaves_other_effects_unapplied(self) -> None:
        """Catches an early valid effect publishing before a later stale hash is found."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            project = self.create(
                store,
                "project",
                {"title": "Project", "roadmap_outcome_id": outcome.entity_id},
            )
            plan = store.plan_roadmap_outcome_bundle_update(
                outcome.entity_id,
                outcome.content_hash,
                {"title": "Bundle title"},
                None,
                [
                    {
                        "action": "update",
                        "id": project.entity_id,
                        "base_hash": project.content_hash,
                        "fields": {"title": "Bundle Project"},
                        "body": None,
                    }
                ],
            )
            external = store.apply_mutation_plan(
                store.plan_update_entity(
                    project.entity_id,
                    project.content_hash,
                    {"title": "External Project"},
                    None,
                )
            )

            with self.assertRaises(ConflictError):
                store.apply_task_workflow(plan)

            self.assertEqual(
                store.get_entity(outcome.entity_id).content_hash,
                outcome.content_hash,
            )
            self.assertEqual(
                store.get_entity(project.entity_id).content_hash,
                external.content_hash,
            )
            self.assertFalse(store.mutation_recovery_required())

    def test_roadmap_bundle_apply_rejects_current_dangling_reference_before_publication(self) -> None:
        """Catches apply accepting reference damage introduced after preview."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(
                store, goal.entity_id, "Outcome", "next", 1
            )
            plan = store.plan_roadmap_outcome_bundle_update(
                outcome.entity_id,
                outcome.content_hash,
                {"title": "Bundle title"},
                None,
                [],
            )
            goal_source = store._root / goal.relative_path
            goal_archive = store._root / "archive" / goal_source.name
            goal_bytes = goal_source.read_bytes()
            goal_source.replace(goal_archive)

            with mock.patch.object(
                store,
                "_reserve_owned_source_for_replace",
                side_effect=AssertionError("must fail before source quarantine"),
            ):
                with self.assertRaises(SchemaError) as raised:
                    store.apply_task_workflow(plan)

            self.assertTrue(
                any("references archived Goal" in error for error in raised.exception.errors)
            )
            self.assertEqual(
                store.get_entity(outcome.entity_id).content_hash,
                outcome.content_hash,
            )
            self.assertFalse(goal_source.exists())
            self.assertEqual(goal_archive.read_bytes(), goal_bytes)
            self.assertFalse(store.mutation_recovery_required())
            self.assertEqual(list(store._root.rglob("*.quarantine")), [])

    def test_roadmap_bundle_write_failure_rolls_back_every_published_effect(self) -> None:
        """Catches a mid-bundle write failure leaving the Outcome partially updated."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            project = self.create(
                store,
                "project",
                {"title": "Project", "roadmap_outcome_id": outcome.entity_id},
            )
            before = {
                entity.entity_id: entity.content_hash for entity in (outcome, project)
            }
            plan = store.plan_roadmap_outcome_bundle_update(
                outcome.entity_id,
                outcome.content_hash,
                {"title": "Bundle title"},
                None,
                [
                    {
                        "action": "update",
                        "id": project.entity_id,
                        "base_hash": project.content_hash,
                        "fields": {"title": "Bundle Project"},
                        "body": None,
                    }
                ],
            )
            real_publish = store._publish_no_replace
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected bundle write failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(store, "_publish_no_replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    store.apply_task_workflow(plan)

            self.assertEqual(
                {
                    entity_id: store.get_entity(entity_id).content_hash
                    for entity_id in before
                },
                before,
            )
            self.assertFalse(store.mutation_recovery_required())
            self.assertEqual(validate_repository(store._root), [])

    def test_roadmap_bundle_rollback_failure_keeps_recovery_required_armed(self) -> None:
        """Catches an incomplete bundle rollback reopening later mutations."""
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(pathlib.Path(temporary))
            goal = self.create_goal(store)
            outcome = self.create_outcome(store, goal.entity_id, "Outcome", "next", 1)
            project = self.create(
                store,
                "project",
                {"title": "Project", "roadmap_outcome_id": outcome.entity_id},
            )
            plan = store.plan_roadmap_outcome_bundle_update(
                outcome.entity_id,
                outcome.content_hash,
                {"title": "Bundle title"},
                None,
                [
                    {
                        "action": "update",
                        "id": project.entity_id,
                        "base_hash": project.content_hash,
                        "fields": {"title": "Bundle Project"},
                        "body": None,
                    }
                ],
            )
            real_publish = store._publish_no_replace
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected bundle write failure")
                return real_publish(*args, **kwargs)

            rollback_error = WorkflowRollbackError(
                (OSError("injected bundle rollback failure"),), ()
            )
            with (
                mock.patch.object(store, "_publish_no_replace", side_effect=fail_second),
                mock.patch.object(
                    store, "_rollback_task_workflow", side_effect=rollback_error
                ),
            ):
                with self.assertRaises(WorkflowRollbackError):
                    store.apply_task_workflow(plan)

            self.assertTrue(store.mutation_recovery_required())
            with self.assertRaises(MutationRecoveryRequired):
                store.plan_roadmap_outcome_bundle_update(
                    outcome.entity_id, outcome.content_hash, {}, None, []
                )


if __name__ == "__main__":
    unittest.main()
