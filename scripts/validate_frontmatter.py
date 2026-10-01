#!/usr/bin/env python3
"""Validate constrained frontmatter in GTD entity Markdown files."""

import dataclasses
import datetime
import pathlib
import re
import sys
import urllib.parse
from collections.abc import Mapping


@dataclasses.dataclass(frozen=True)
class EntityContract:
    entity_type: str
    review_kind: str | None = None
    task_location: str | None = None


ENTITY_CONTRACTS = (
    ("inbox", EntityContract("task", task_location="inbox")),
    ("tasks", EntityContract("task", task_location="tasks")),
    ("purposes", EntityContract("purpose")),
    ("visions", EntityContract("vision")),
    ("areas", EntityContract("area")),
    ("projects", EntityContract("project")),
    ("goals", EntityContract("goal")),
    ("roadmap-outcomes", EntityContract("roadmap_outcome")),
    ("cycles", EntityContract("cycle")),
    ("progress", EntityContract("progress")),
    ("time-allocation-plans", EntityContract("time_allocation_plan")),
    ("reviews/daily", EntityContract("review", review_kind="daily")),
    ("reviews/weekly", EntityContract("review", review_kind="weekly")),
)

REQUIRED_KEYS = {
    "task": ("id", "type", "title", "status", "created_at", "updated_at"),
    "purpose": ("id", "type", "title", "created_at", "updated_at"),
    "vision": ("id", "type", "title", "status", "created_at", "updated_at"),
    "area": ("id", "type", "title", "created_at", "updated_at"),
    "project": ("id", "type", "title", "status", "created_at", "updated_at"),
    "goal": ("id", "type", "title", "status", "created_at", "updated_at"),
    "roadmap_outcome": (
        "id", "type", "title", "status", "goal_id", "created_at", "updated_at"
    ),
    "cycle": (
        "id", "type", "title", "status", "start_date", "end_date",
        "outcome_ids", "created_at", "updated_at",
    ),
    "progress": (
        "id", "type", "title", "occurred_on", "visibility",
        "created_at", "updated_at",
    ),
    "review": (
        "id",
        "type",
        "title",
        "review_kind",
        "period_start",
        "created_at",
    ),
    "time_allocation_plan": (
        "id", "type", "title", "axis", "period_kind", "period_start",
        "period_end", "revision", "status", "input_mode", "total_minutes",
        "created_at", "updated_at",
    ),
}

ALLOWED_STATUSES = {
    "task": {
        "inbox", "planned", "next", "doing", "waiting", "scheduled",
        "someday", "done",
    },
    "vision": {"active", "on_hold", "realized", "dropped"},
    "project": {
        "not_started", "doing", "on_hold", "completed", "dropped", "active"
    },
    "goal": {"active", "on_hold", "achieved", "dropped"},
    "roadmap_outcome": {"active", "achieved", "dropped"},
    "cycle": {"planned", "active", "completed", "cancelled"},
    "time_allocation_plan": {"active", "superseded", "withdrawn"},
}

OPTIONAL_KEY_ORDER = {
    "task": (
        "project_id",
        "project_position",
        "area_id",
        "action_date",
        "due",
        "scheduled_start",
        "scheduled_end",
        "available_from",
        "contexts",
        "estimated_minutes",
        "depends_on",
        "waiting_for",
        "work_started_at",
        "work_ended_at",
        "timer_kind",
        "timer_ends_at",
        "resume_status",
        "continuation_of",
        "calendar_id",
        "calendar_event_id",
        "calendar_event_url",
        "calendar_event_kind",
        "calendar_sync_version",
        "started_at",
        "completed_at",
    ),
    "purpose": (),
    "vision": (),
    "area": ("health", "last_reviewed_on"),
    "project": (
        "goal_id",
        "roadmap_outcome_id",
        "area_id",
        "planned_start_date",
        "planned_end_date",
        "kanban_position",
    ),
    "goal": ("vision_id", "target_date"),
    "roadmap_outcome": ("target_date", "roadmap_lane", "roadmap_position"),
    "cycle": (
        "achieved_outcome_ids",
        "carried_outcome_ids",
        "next_outcome_ids",
        "later_outcome_ids",
        "dropped_outcome_ids",
        "carryover_cycle_id",
        "cancellation_reason",
    ),
    "progress": ("project_id", "goal_id", "area_id"),
    "review": (),
    "time_allocation_plan": ("supersedes_id",),
}

OPTIONAL_KEYS = {
    entity_type: set(keys)
    for entity_type, keys in OPTIONAL_KEY_ORDER.items()
}

FORBIDDEN_KEYS = {
    "priority",
    "xp",
    "level",
    "reward",
    "rewards",
    "randomized_reward",
    "punishment",
    "streak",
    "streak_loss",
}

DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})"
)
WORK_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})"
)
TASK_ID_PATTERN = re.compile(r"task-[A-Za-z0-9_-]{1,95}", re.ASCII)
PROGRESS_ID_PATTERN = re.compile(
    r"progress-(?P<year>[0-9]{4})(?P<month>[0-9]{2})(?P<day>[0-9]{2})-[0-9]{3}",
    re.ASCII,
)
TIME_ALLOCATION_PLAN_ID_PATTERN = re.compile(
    r"time-allocation-plan-[0-9]{8}-[0-9]{3}", re.ASCII
)
POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9]\d*")
PROJECT_KANBAN_POSITION_PATTERN = re.compile(r"[1-9][0-9]*", re.ASCII)
PROJECT_TASK_POSITION_PATTERN = re.compile(r"[1-9][0-9]*", re.ASCII)
INLINE_LIST_PATTERN = re.compile(r"\[.*\]")
CALENDAR_EVENT_ID_PATTERN = re.compile(r"[!-~]+")

DONE_CALENDAR_ID = "example@group.calendar.google.com"
CALENDAR_IDENTITY_KEYS = (
    "calendar_id",
    "calendar_event_id",
    "calendar_event_url",
    "calendar_event_kind",
)

REFERENCE_CONTRACTS = {
    "task": (
        ("project_id", "project", "Project"),
        ("area_id", "area", "Area"),
    ),
    "project": (
        ("goal_id", "goal", "Goal"),
        ("roadmap_outcome_id", "roadmap_outcome", "Roadmap Outcome"),
        ("area_id", "area", "Area"),
    ),
    "goal": (("vision_id", "vision", "Vision"),),
    "roadmap_outcome": (("goal_id", "goal", "Goal"),),
    "progress": (
        ("project_id", "project", "Project"),
        ("goal_id", "goal", "Goal"),
        ("area_id", "area", "Area"),
    ),
}

TIME_ALLOCATION_AXES = frozenset({"area", "goal", "roadmap_outcome"})
TIME_ALLOCATION_PERIOD_KINDS = frozenset({"day", "week", "month", "year"})
TIME_ALLOCATION_INPUT_MODES = frozenset({"ratio", "minutes"})


def time_allocation_period_end(
    period_kind: str, period_start: str
) -> datetime.date | None:
    """Return the aligned half-open period end, or None for invalid input."""
    start = _parse_iso_date(period_start)
    if start is None or period_kind not in TIME_ALLOCATION_PERIOD_KINDS:
        return None
    if period_kind == "day":
        return start + datetime.timedelta(days=1)
    if period_kind == "week":
        return start + datetime.timedelta(days=7) if start.weekday() == 0 else None
    if period_kind == "month":
        if start.day != 1:
            return None
        return (
            datetime.date(start.year + 1, 1, 1)
            if start.month == 12
            else datetime.date(start.year, start.month + 1, 1)
        )
    if start.month != 1 or start.day != 1:
        return None
    return datetime.date(start.year + 1, 1, 1)


def parse_time_allocation_allocations(body: str) -> dict[str, int] | None:
    """Parse the strict deterministic `## Allocations` body contract."""
    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    try:
        heading_index = lines.index("## Allocations")
    except ValueError:
        return None
    if any(line.strip() for line in lines[:heading_index]):
        return None
    allocations: dict[str, int] = {}
    for line in lines[heading_index + 1 :]:
        if not line.strip():
            continue
        match = re.fullmatch(r"- ([A-Za-z0-9_-]+): (0|[1-9][0-9]*)", line)
        if match is None or match.group(1) in allocations:
            return None
        allocations[match.group(1)] = int(match.group(2))
    return allocations or None

_CYCLE_RESULT_KEYS = (
    "achieved_outcome_ids",
    "carried_outcome_ids",
    "next_outcome_ids",
    "later_outcome_ids",
    "dropped_outcome_ids",
)


def _parse_frontmatter_text(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening ---")

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise ValueError("missing closing ---")

    frontmatter: dict[str, str] = {}
    for line in lines[1:closing_index]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            raise ValueError(f"invalid frontmatter line: {line}")
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {line}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid frontmatter line: {line}")
        if key in frontmatter:
            raise ValueError(f"duplicate frontmatter key: {key}")
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        frontmatter[key] = value
    return frontmatter


def parse_frontmatter(path: pathlib.Path) -> dict[str, str]:
    """Parse the repository's constrained top-level frontmatter format."""
    return _parse_frontmatter_text(path.read_text(encoding="utf-8"))


def _infer_document_contract(
    path: pathlib.Path, frontmatter: dict[str, str]
) -> EntityContract | None:
    parts = path.parts[:-1]
    actual_type = frontmatter.get("type")
    if "archive" in parts and actual_type in REQUIRED_KEYS:
        return EntityContract(actual_type)

    candidates: list[tuple[int, EntityContract]] = []
    for index, part in enumerate(parts):
        if part == "inbox":
            candidates.append((index, EntityContract("task", task_location="inbox")))
        elif part == "tasks":
            candidates.append((index, EntityContract("task", task_location="tasks")))
        elif part == "projects":
            candidates.append((index, EntityContract("project")))
        elif part == "goals":
            candidates.append((index, EntityContract("goal")))
        elif part == "roadmap-outcomes":
            candidates.append((index, EntityContract("roadmap_outcome")))
        elif part == "cycles":
            candidates.append((index, EntityContract("cycle")))
        elif part == "progress":
            candidates.append((index, EntityContract("progress")))
        elif part == "time-allocation-plans":
            candidates.append((index, EntityContract("time_allocation_plan")))
        elif part == "purposes":
            candidates.append((index, EntityContract("purpose")))
        elif part == "visions":
            candidates.append((index, EntityContract("vision")))
        elif part == "areas":
            candidates.append((index, EntityContract("area")))
        elif (
            part == "reviews"
            and index + 1 < len(parts)
            and parts[index + 1] in {"daily", "weekly"}
        ):
            candidates.append(
                (index, EntityContract("review", review_kind=parts[index + 1]))
            )

    matching_type = [
        candidate
        for _, candidate in candidates
        if candidate.entity_type == actual_type
    ]
    if matching_type:
        return matching_type[0]
    if candidates:
        return candidates[0][1]
    return None


def _parse_iso_date(value: str) -> datetime.date | None:
    if DATE_PATTERN.fullmatch(value) is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_iso_timestamp(value: str) -> datetime.datetime | None:
    if TIMESTAMP_PATTERN.fullmatch(value) is None:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed


def parse_inline_list(value: str) -> list[str] | None:
    """Parse the repository's flat, unquoted inline-list subset."""
    if not isinstance(value, str) or INLINE_LIST_PATTERN.fullmatch(value) is None:
        return None
    inner = value[1:-1].strip()
    if not inner:
        return []
    values = [item.strip() for item in inner.split(",")]
    if any(not item or any(character in item for character in "[]\r\n") for item in values):
        return None
    return values


def _parse_work_timestamp(value: str) -> datetime.datetime | None:
    if WORK_TIMESTAMP_PATTERN.fullmatch(value) is None:
        return None
    return _parse_iso_timestamp(value)


def _is_google_calendar_event_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"calendar.google.com", "www.google.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return False
    event_ids = urllib.parse.parse_qs(
        parsed.query,
        keep_blank_values=True,
    ).get("eid", [])
    return (
        parsed.path in {"/calendar/event", "/calendar/eventedit"}
        and len(event_ids) == 1
        and bool(event_ids[0])
    )


def _validate_values(
    frontmatter: dict[str, str],
    contract: EntityContract,
) -> list[str]:
    entity_type = contract.entity_type
    errors: list[str] = []
    for key in REQUIRED_KEYS[entity_type]:
        if key not in frontmatter:
            errors.append(f"missing required key: {key}")

    allowed_keys = set(REQUIRED_KEYS[entity_type]) | OPTIONAL_KEYS[entity_type]
    for key in frontmatter:
        if key in FORBIDDEN_KEYS:
            errors.append(f"forbidden key: {key}")
        elif key not in allowed_keys:
            errors.append(f"unknown key: {key}")

    for key in ("id", "title"):
        if key in frontmatter and not frontmatter[key].strip():
            errors.append(f"empty required key: {key}")

    actual_type = frontmatter.get("type")
    if actual_type is not None and actual_type != entity_type:
        errors.append(f"invalid type: {actual_type}; expected {entity_type}")

    status = frontmatter.get("status")
    if status is not None and status not in ALLOWED_STATUSES.get(entity_type, set()):
        errors.append(f"invalid {entity_type} status: {status}")
    if entity_type == "task" and status is not None:
        if contract.task_location == "inbox" and status != "inbox":
            errors.append(
                f"invalid task status for inbox: {status}; expected inbox"
            )
        elif contract.task_location == "tasks" and status == "inbox":
            errors.append("invalid task status for tasks: inbox")

    if entity_type == "review":
        review_kind = frontmatter.get("review_kind")
        if review_kind is not None and review_kind not in {"daily", "weekly"}:
            errors.append(f"invalid review kind: {review_kind}")
        elif (
            review_kind is not None
            and contract.review_kind is not None
            and review_kind != contract.review_kind
        ):
            errors.append(
                f"invalid review kind for directory: {review_kind}; "
                f"expected {contract.review_kind}"
            )

    timestamp_fields = ["created_at"]
    if entity_type in {
        "task", "purpose", "vision", "area", "project", "goal",
        "roadmap_outcome", "cycle", "progress", "time_allocation_plan",
    }:
        timestamp_fields.append("updated_at")
    if entity_type == "task":
        timestamp_fields.extend(
            (
                "scheduled_start",
                "scheduled_end",
                "work_started_at",
                "work_ended_at",
                "timer_ends_at",
            )
        )
    parsed_timestamps: dict[str, datetime.datetime] = {}
    for key in timestamp_fields:
        value = frontmatter.get(key)
        if value is None or (
            key
            in {
                "scheduled_start",
                "scheduled_end",
                "work_started_at",
                "work_ended_at",
                "timer_ends_at",
            }
            and not value
        ):
            continue
        parser = (
            _parse_work_timestamp
            if key.startswith("work_") or key == "timer_ends_at"
            else _parse_iso_timestamp
        )
        parsed = parser(value)
        if parsed is None:
            errors.append(f"invalid timestamp for {key}: {value}")
        else:
            parsed_timestamps[key] = parsed

    if entity_type == "task":
        if frontmatter.get("project_id") and frontmatter.get("area_id"):
            errors.append("project_id and area_id cannot both be set")

        project_position = frontmatter.get("project_position")
        if (
            project_position
            and PROJECT_TASK_POSITION_PATTERN.fullmatch(project_position) is None
        ):
            errors.append(f"invalid project_position: {project_position}")

        if status == "planned":
            if not frontmatter.get("project_id"):
                errors.append("planned Task requires project_id")
            planned_forbidden = (
                "waiting_for",
                "action_date",
                "scheduled_start",
                "scheduled_end",
                *CALENDAR_IDENTITY_KEYS,
                "calendar_sync_version",
                "started_at",
                "completed_at",
            )
            for key in planned_forbidden:
                if frontmatter.get(key):
                    errors.append(f"planned Task forbids {key}")

        if status == "inbox" and frontmatter.get("action_date"):
            errors.append("inbox Task forbids action_date")

        action_date = frontmatter.get("action_date")
        if action_date and _parse_iso_date(action_date) is None:
            errors.append(f"invalid action_date: {action_date}")

        available_from = frontmatter.get("available_from")
        if (
            available_from
            and _parse_iso_date(available_from) is None
            and _parse_iso_timestamp(available_from) is None
        ):
            errors.append(f"invalid available_from: {available_from}")

        due = frontmatter.get("due")
        if due and _parse_iso_date(due) is None and _parse_iso_timestamp(due) is None:
            errors.append(f"invalid due: {due}")

        estimated_minutes = frontmatter.get("estimated_minutes")
        if (
            estimated_minutes
            and POSITIVE_INTEGER_PATTERN.fullmatch(estimated_minutes) is None
        ):
            errors.append(
                f"invalid estimated_minutes: {estimated_minutes}; "
                "expected positive integer"
            )

        contexts = frontmatter.get("contexts")
        if contexts is not None and INLINE_LIST_PATTERN.fullmatch(contexts) is None:
            errors.append(f"invalid contexts: {contexts}; expected inline list")

        depends_on_value = frontmatter.get("depends_on")
        if depends_on_value is not None:
            depends_on = parse_inline_list(depends_on_value)
            if depends_on is None:
                errors.append("depends_on must be an inline list")
            else:
                if any(TASK_ID_PATTERN.fullmatch(item) is None for item in depends_on):
                    errors.append("depends_on must contain only Task IDs")
                if frontmatter.get("id") in depends_on:
                    errors.append("depends_on must not reference its own Task")
                if len(depends_on) != len(set(depends_on)):
                    errors.append("depends_on must not contain duplicate IDs")

        scheduled_start = parsed_timestamps.get("scheduled_start")
        scheduled_end = parsed_timestamps.get("scheduled_end")
        if (
            scheduled_start is not None
            and scheduled_end is not None
            and scheduled_end < scheduled_start
        ):
            errors.append("scheduled_end is earlier than scheduled_start")

        work_started_at = parsed_timestamps.get("work_started_at")
        work_ended_at = parsed_timestamps.get("work_ended_at")
        if (
            work_started_at is not None
            and work_ended_at is not None
            and work_ended_at < work_started_at
        ):
            errors.append("work_ended_at is earlier than work_started_at")

        has_work_started_at = bool(frontmatter.get("work_started_at"))
        has_work_ended_at = bool(frontmatter.get("work_ended_at"))
        has_resume_status = bool(frontmatter.get("resume_status"))
        has_continuation_of = bool(frontmatter.get("continuation_of"))
        has_work_session_metadata = any(
            (
                has_work_started_at,
                has_work_ended_at,
                has_resume_status,
                has_continuation_of,
            )
        )
        resume_status = frontmatter.get("resume_status")
        if resume_status and resume_status not in {"next", "scheduled"}:
            errors.append(
                f"invalid resume_status: {resume_status}; expected next or scheduled"
            )
        if has_work_ended_at and not has_work_started_at:
            errors.append("work_ended_at requires work_started_at")
        if has_resume_status and not has_work_started_at:
            errors.append("resume_status requires work_started_at")
        if has_resume_status and status != "doing":
            errors.append("resume_status is only valid for doing Tasks")
        if has_work_ended_at and status != "done":
            errors.append("work_ended_at is only valid for done Tasks")
        if status == "doing" and has_work_session_metadata:
            if not has_work_started_at:
                errors.append("doing work session requires work_started_at")
            if not has_resume_status:
                errors.append("doing work session requires resume_status")
        if has_work_started_at and status == "done" and not has_work_ended_at:
            errors.append("done work session requires work_ended_at")
        if has_work_started_at and status not in {"doing", "done"}:
            errors.append("work_started_at is only valid for doing or done Tasks")

        timer_kind = frontmatter.get("timer_kind", "")
        timer_ends_at_value = frontmatter.get("timer_ends_at", "")
        if bool(timer_kind) != bool(timer_ends_at_value):
            errors.append("timer_kind and timer_ends_at must be paired")
        if timer_kind:
            if timer_kind != "break":
                errors.append("timer_kind must be break")
            if frontmatter.get("title") != "5分休憩":
                errors.append("break timer requires title 5分休憩")
            if status not in {"doing", "done"}:
                errors.append("break timer is only valid for doing or done Tasks")
            timer_ends_at = parsed_timestamps.get("timer_ends_at")
            if work_started_at is None:
                errors.append("break timer requires work_started_at")
            elif (
                timer_ends_at is not None
                and timer_ends_at - work_started_at != datetime.timedelta(seconds=300)
            ):
                errors.append(
                    "timer_ends_at must be exactly 300 seconds after work_started_at"
                )

        continuation_of = frontmatter.get("continuation_of")
        if continuation_of:
            if TASK_ID_PATTERN.fullmatch(continuation_of) is None:
                errors.append("continuation_of must be a Task ID")
            elif continuation_of == frontmatter.get("id"):
                errors.append("continuation_of must not reference its own Task")

        calendar_values = {
            key: frontmatter.get(key, "") for key in CALENDAR_IDENTITY_KEYS
        }
        calendar_sync_version = frontmatter.get("calendar_sync_version", "")
        if calendar_sync_version and calendar_sync_version != "2":
            errors.append("calendar_sync_version must be 2")
        calendar_metadata_present = any(calendar_values.values()) or bool(
            calendar_sync_version
        )
        if calendar_metadata_present:
            for key in CALENDAR_IDENTITY_KEYS:
                if not calendar_values[key]:
                    errors.append(f"calendar identity requires {key}")

        calendar_id = calendar_values["calendar_id"]
        if calendar_id and calendar_id != DONE_CALENDAR_ID:
            errors.append(f"invalid calendar_id: {calendar_id}")

        calendar_event_id = calendar_values["calendar_event_id"]
        if calendar_event_id and (
            CALENDAR_EVENT_ID_PATTERN.fullmatch(calendar_event_id) is None
            or "/" in calendar_event_id
            or "\\" in calendar_event_id
        ):
            errors.append(f"invalid calendar_event_id: {calendar_event_id}")

        calendar_event_url = calendar_values["calendar_event_url"]
        if calendar_event_url and not _is_google_calendar_event_url(
            calendar_event_url
        ):
            errors.append(f"invalid calendar_event_url: {calendar_event_url}")

        calendar_event_kind = calendar_values["calendar_event_kind"]
        if calendar_event_kind and calendar_event_kind not in {"all_day", "timed"}:
            errors.append(f"invalid calendar_event_kind: {calendar_event_kind}")

        calendar_identity_complete = all(calendar_values.values())
        if calendar_sync_version and not calendar_identity_complete:
            errors.append("calendar_sync_version requires Calendar identity")
        for key in ("started_at", "completed_at"):
            if frontmatter.get(key) and not calendar_identity_complete:
                errors.append(f"{key} requires Calendar identity")

        started_at = frontmatter.get("started_at", "")
        completed_at = frontmatter.get("completed_at", "")
        for key, value in (("started_at", started_at), ("completed_at", completed_at)):
            if not value:
                continue
            parsed = _parse_iso_timestamp(value)
            if parsed is None:
                errors.append(f"invalid timestamp for {key}: {value}")
            else:
                parsed_timestamps[key] = parsed

        if started_at and status not in {"doing", "done"}:
            errors.append("started_at requires task status doing or done")
        if completed_at and status != "done":
            errors.append("completed_at requires task status done")

        parsed_started_at = parsed_timestamps.get("started_at")
        parsed_completed_at = parsed_timestamps.get("completed_at")
        if (
            parsed_started_at is not None
            and parsed_completed_at is not None
            and parsed_completed_at <= parsed_started_at
        ):
            errors.append("completed_at must be later than started_at")

    if entity_type == "review":
        period_start = frontmatter.get("period_start")
        if period_start is not None and _parse_iso_date(period_start) is None:
            errors.append(f"invalid date for period_start: {period_start}")

    if entity_type == "time_allocation_plan":
        entity_id = frontmatter.get("id", "")
        if TIME_ALLOCATION_PLAN_ID_PATTERN.fullmatch(entity_id) is None:
            errors.append(f"invalid time allocation plan id: {entity_id}")
        axis = frontmatter.get("axis", "")
        if axis not in TIME_ALLOCATION_AXES:
            errors.append(f"invalid allocation axis: {axis}")
        period_kind = frontmatter.get("period_kind", "")
        if period_kind not in TIME_ALLOCATION_PERIOD_KINDS:
            errors.append(f"invalid period_kind: {period_kind}")
        period_start = frontmatter.get("period_start", "")
        expected_end = time_allocation_period_end(period_kind, period_start)
        if expected_end is None:
            errors.append(
                f"invalid aligned period_start for {period_kind}: {period_start}"
            )
        elif frontmatter.get("period_end") != expected_end.isoformat():
            errors.append(
                f"invalid period_end: {frontmatter.get('period_end', '')}; "
                f"expected {expected_end.isoformat()}"
            )
        revision = frontmatter.get("revision", "")
        if POSITIVE_INTEGER_PATTERN.fullmatch(revision) is None:
            errors.append(f"invalid revision: {revision}")
        input_mode = frontmatter.get("input_mode", "")
        if input_mode not in TIME_ALLOCATION_INPUT_MODES:
            errors.append(f"invalid input_mode: {input_mode}")
        total_minutes = frontmatter.get("total_minutes", "")
        if POSITIVE_INTEGER_PATTERN.fullmatch(total_minutes) is None:
            errors.append(f"invalid total_minutes: {total_minutes}")
        supersedes_id = frontmatter.get("supersedes_id", "")
        if revision and POSITIVE_INTEGER_PATTERN.fullmatch(revision):
            if int(revision) == 1 and supersedes_id:
                errors.append("revision 1 forbids supersedes_id")
            if int(revision) > 1 and not supersedes_id:
                errors.append("revision after 1 requires supersedes_id")
        if supersedes_id and TIME_ALLOCATION_PLAN_ID_PATTERN.fullmatch(supersedes_id) is None:
            errors.append(f"invalid supersedes_id: {supersedes_id}")

    if entity_type == "progress":
        entity_id = frontmatter.get("id", "")
        match = PROGRESS_ID_PATTERN.fullmatch(entity_id)
        if match is None or _parse_iso_date(
            f"{match.group('year')}-{match.group('month')}-{match.group('day')}"
        ) is None:
            errors.append(f"invalid progress id: {entity_id}")
        occurred_on = frontmatter.get("occurred_on")
        if occurred_on is not None and _parse_iso_date(occurred_on) is None:
            errors.append(f"invalid date for occurred_on: {occurred_on}")
        if frontmatter.get("visibility") != "private":
            errors.append("visibility must be private")
        if sum(bool(frontmatter.get(key)) for key in ("project_id", "goal_id", "area_id")) > 1:
            errors.append("Progress origin must contain at most one of project_id, goal_id, area_id")

    if entity_type == "area":
        health = frontmatter.get("health")
        if health and health not in {"maintained", "needs_attention"}:
            errors.append(f"invalid area health: {health}")
        last_reviewed_on = frontmatter.get("last_reviewed_on")
        if last_reviewed_on and _parse_iso_date(last_reviewed_on) is None:
            errors.append(
                f"invalid date for last_reviewed_on: {last_reviewed_on}"
            )

    if entity_type == "goal":
        target_date = frontmatter.get("target_date")
        if target_date and _parse_iso_date(target_date) is None:
            errors.append(f"invalid date for target_date: {target_date}")

    if entity_type == "project":
        if frontmatter.get("goal_id") and frontmatter.get("roadmap_outcome_id"):
            errors.append("goal_id and roadmap_outcome_id cannot both be set")
        planned_start_date = frontmatter.get("planned_start_date")
        planned_end_date = frontmatter.get("planned_end_date")
        if bool(planned_start_date) != bool(planned_end_date):
            errors.append("planned_start_date and planned_end_date must be paired")
        elif planned_start_date and planned_end_date:
            start = _parse_iso_date(planned_start_date)
            end = _parse_iso_date(planned_end_date)
            if start is None:
                errors.append(f"invalid date for planned_start_date: {planned_start_date}")
            if end is None:
                errors.append(f"invalid date for planned_end_date: {planned_end_date}")
            if start is not None and end is not None and end < start:
                errors.append("planned_end_date is earlier than planned_start_date")
        position = frontmatter.get("kanban_position")
        if position and PROJECT_KANBAN_POSITION_PATTERN.fullmatch(position) is None:
            errors.append(f"invalid kanban_position: {position}")

    if entity_type == "roadmap_outcome":
        if not frontmatter.get("goal_id", "").strip():
            errors.append("empty required key: goal_id")
        target_date = frontmatter.get("target_date")
        if target_date and _parse_iso_date(target_date) is None:
            errors.append(f"invalid date for target_date: {target_date}")
        lane = frontmatter.get("roadmap_lane")
        position = frontmatter.get("roadmap_position")
        if bool(lane) != bool(position):
            errors.append("roadmap_lane and roadmap_position must be paired")
        if lane and lane not in {"next", "later"}:
            errors.append(f"invalid roadmap_lane: {lane}")
        if position and POSITIVE_INTEGER_PATTERN.fullmatch(position) is None:
            errors.append(f"invalid roadmap_position: {position}")
        if status in {"achieved", "dropped"} and (lane or position):
            errors.append("closed Roadmap Outcome must not have lane fields")

    if entity_type == "cycle":
        start_date = _parse_iso_date(frontmatter.get("start_date", ""))
        end_date = _parse_iso_date(frontmatter.get("end_date", ""))
        if start_date is None:
            errors.append(f"invalid date for start_date: {frontmatter.get('start_date', '')}")
        if end_date is None:
            errors.append(f"invalid date for end_date: {frontmatter.get('end_date', '')}")
        if start_date is not None and end_date is not None and end_date < start_date:
            errors.append("end_date is earlier than start_date")
        outcome_ids = parse_inline_list(frontmatter.get("outcome_ids", ""))
        if outcome_ids is None:
            errors.append("outcome_ids must be an inline list")
            outcome_ids = []
        elif len(outcome_ids) > 2:
            errors.append("outcome_ids must contain at most two IDs")
        if len(outcome_ids) != len(set(outcome_ids)):
            errors.append("outcome_ids must not contain duplicate IDs")

        present_results = [key for key in _CYCLE_RESULT_KEYS if key in frontmatter]
        if status in {"planned", "active"}:
            for key in (*_CYCLE_RESULT_KEYS, "carryover_cycle_id", "cancellation_reason"):
                if key in frontmatter:
                    errors.append(f"{key} is forbidden for open Cycle")
        elif status in {"completed", "cancelled"}:
            if len(present_results) != len(_CYCLE_RESULT_KEYS):
                errors.append("closed Cycle requires all five result lists")
            result_values: list[str] = []
            malformed = False
            for key in _CYCLE_RESULT_KEYS:
                parsed = parse_inline_list(frontmatter.get(key, ""))
                if parsed is None:
                    malformed = True
                    errors.append(f"{key} must be an inline list")
                else:
                    result_values.extend(parsed)
            if (
                not malformed
                and (
                    len(result_values) != len(set(result_values))
                    or set(result_values) != set(outcome_ids)
                )
            ):
                errors.append("closing result lists must partition outcome_ids")
            carried = parse_inline_list(frontmatter.get("carried_outcome_ids", "")) or []
            if (
                (carried and not frontmatter.get("carryover_cycle_id"))
                or (not carried and "carryover_cycle_id" in frontmatter)
            ):
                errors.append("carryover_cycle_id must match carried_outcome_ids")
            has_reason = bool(frontmatter.get("cancellation_reason", "").strip())
            if status == "cancelled" and not has_reason:
                errors.append("cancelled Cycle requires cancellation_reason")
            if status == "completed" and "cancellation_reason" in frontmatter:
                errors.append("completed Cycle must not have cancellation_reason")

    return errors


def _validate_purpose_body_text(text: str) -> list[str]:
    lines = text.splitlines()
    closing_index = next(
        index
        for index, line in enumerate(lines[1:], start=1)
        if line.strip() == "---"
    )
    body_lines = lines[closing_index + 1 :]
    return [
        f"missing required Purpose body heading: {heading}"
        for heading in ("## Purpose", "## Principles")
        if heading not in body_lines
    ]


def _body_section_text_from_text(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    try:
        closing_index = next(
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return None
    body = lines[closing_index + 1 :]
    try:
        heading_index = body.index(heading)
    except ValueError:
        return None
    section: list[str] = []
    for line in body[heading_index + 1 :]:
        if line.startswith("## "):
            break
        section.append(line)
    return "\n".join(section).strip()


def _body_section_text(path: pathlib.Path, heading: str) -> str | None:
    return _body_section_text_from_text(path.read_text(encoding="utf-8"), heading)


def _validate_document(
    path: pathlib.Path,
    display_path: pathlib.Path,
    contract: EntityContract | None,
    *,
    infer_contract: bool = False,
    archive: bool = False,
    document_bytes: bytes | None = None,
) -> list[str]:
    text = (
        path.read_text(encoding="utf-8")
        if document_bytes is None
        else document_bytes.decode("utf-8")
    )
    try:
        frontmatter = _parse_frontmatter_text(text)
    except ValueError as error:
        return [f"{display_path}: {error}"]

    if archive:
        archive_type = frontmatter.get("type")
        if archive_type is None:
            return [f"{display_path}: missing required key: type"]
        if archive_type not in REQUIRED_KEYS:
            return [f"{display_path}: invalid archive entity type: {archive_type}"]
        contract = EntityContract(archive_type)
    elif infer_contract:
        contract = _infer_document_contract(path, frontmatter)
    if contract is None:
        return [f"{display_path}: unsupported entity directory"]

    errors = _validate_values(frontmatter, contract)
    if frontmatter.get("type") == "purpose":
        errors.extend(_validate_purpose_body_text(text))
    elif frontmatter.get("type") == "roadmap_outcome":
        for heading in ("## 成功条件", "## メモ"):
            if _body_section_text_from_text(text, heading) is None:
                errors.append(f"missing required Roadmap Outcome body heading: {heading}")
    elif frontmatter.get("type") == "cycle":
        for heading in ("## 振り返り", "## メモ"):
            if _body_section_text_from_text(text, heading) is None:
                errors.append(f"missing required Cycle body heading: {heading}")
        if (
            frontmatter.get("status") in {"completed", "cancelled"}
            and not (_body_section_text_from_text(text, "## 振り返り") or "").strip()
        ):
            errors.append("closed Cycle requires non-empty retrospective")
    elif frontmatter.get("type") == "time_allocation_plan":
        body = text
        closing_index = body.find("\n---", 4)
        allocations = parse_time_allocation_allocations(
            "" if closing_index < 0 else body[closing_index + 4 :]
        )
        if allocations is None:
            errors.append("invalid or missing Allocations body")
        else:
            expected_total = (
                10000
                if frontmatter.get("input_mode") == "ratio"
                else int(frontmatter.get("total_minutes", "0") or "0")
            )
            if sum(allocations.values()) != expected_total:
                errors.append(
                    f"allocation values must sum to {expected_total}"
                )
    return [f"{display_path}: {error}" for error in errors]


def validate_document(path: pathlib.Path) -> list[str]:
    """Validate one document by inferring its contract from path and type.

    Use validate_repository for authoritative scan-root handling when a path has
    multiple entity-directory components and its intended root is ambiguous.
    """
    return _validate_document(path, path, None, infer_contract=True)


def validate_document_map(documents: Mapping[str, bytes]) -> list[str]:
    """Validate supported repository documents held entirely in memory."""
    normalized_documents = {
        pathlib.PurePosixPath(relative_path): document_bytes
        for relative_path, document_bytes in documents.items()
    }
    errors: list[str] = []
    ids: dict[str, pathlib.Path] = {}
    entities_by_id: dict[
        str, list[tuple[pathlib.Path, dict[str, str], bool]]
    ] = {}
    entity_types_by_path: dict[pathlib.Path, str] = {}
    continuation_references: list[tuple[pathlib.Path, str]] = []
    dependency_references: list[tuple[pathlib.Path, str, str, bool]] = []
    entity_references: list[
        tuple[pathlib.Path, str, str, str, str, bool]
    ] = []
    doing_tasks: list[pathlib.Path] = []
    purposes: list[pathlib.Path] = []
    roadmap_outcomes: list[tuple[pathlib.Path, dict[str, str]]] = []
    cycles: list[tuple[pathlib.Path, dict[str, str]]] = []
    projects: list[tuple[pathlib.Path, dict[str, str]]] = []
    time_allocation_plans: list[tuple[pathlib.Path, dict[str, str], dict[str, int]]] = []

    scan_targets = (*ENTITY_CONTRACTS, ("archive", None))
    for directory, contract in scan_targets:
        directory_path = pathlib.PurePosixPath(directory)
        matching_paths = sorted(
            relative_path
            for relative_path in normalized_documents
            if relative_path.suffix == ".md"
            and relative_path.name != "README.md"
            and relative_path.is_relative_to(directory_path)
        )
        for relative_path in matching_paths:
            document_bytes = normalized_documents[relative_path]
            errors.extend(
                _validate_document(
                    pathlib.Path(relative_path.as_posix()),
                    pathlib.Path(relative_path.as_posix()),
                    contract,
                    archive=directory == "archive",
                    document_bytes=document_bytes,
                )
            )
            try:
                text = document_bytes.decode("utf-8")
                frontmatter = _parse_frontmatter_text(text)
            except ValueError:
                continue
            entity_id = frontmatter.get("id", "")
            entity_types_by_path[relative_path] = frontmatter.get("type", "")
            if (
                directory != "archive"
                and contract is not None
                and contract.entity_type == "task"
                and frontmatter.get("type") == "task"
                and frontmatter.get("status") == "doing"
            ):
                doing_tasks.append(relative_path)
            if (
                directory != "archive"
                and contract is not None
                and contract.entity_type == "purpose"
                and frontmatter.get("type") == "purpose"
            ):
                purposes.append(relative_path)
            if directory != "archive" and frontmatter.get("type") == "roadmap_outcome":
                roadmap_outcomes.append((relative_path, frontmatter))
            if directory != "archive" and frontmatter.get("type") == "cycle":
                cycles.append((relative_path, frontmatter))
            if directory != "archive" and frontmatter.get("type") == "project":
                projects.append((relative_path, frontmatter))
            if (
                directory != "archive"
                and frontmatter.get("type") == "time_allocation_plan"
            ):
                section = _body_section_text_from_text(text, "## Allocations")
                allocations = parse_time_allocation_allocations(
                    "## Allocations\n" + (section or "")
                )
                time_allocation_plans.append(
                    (relative_path, frontmatter, allocations or {})
                )
            if not entity_id:
                continue
            entities_by_id.setdefault(entity_id, []).append(
                (relative_path, frontmatter, directory == "archive")
            )
            entity_type = frontmatter.get("type", "")
            for field, target_type, target_label in REFERENCE_CONTRACTS.get(
                entity_type, ()
            ):
                target_id = frontmatter.get(field, "")
                if target_id:
                    entity_references.append(
                        (
                            relative_path,
                            field,
                            target_id,
                            target_type,
                            target_label,
                            directory == "archive",
                        )
                    )
            if frontmatter.get("type") == "cycle":
                cycle_reference_fields = ("outcome_ids", *_CYCLE_RESULT_KEYS)
                for field in cycle_reference_fields:
                    for target_id in parse_inline_list(frontmatter.get(field, "")) or []:
                        entity_references.append(
                            (
                                relative_path,
                                field,
                                target_id,
                                "roadmap_outcome",
                                "Roadmap Outcome",
                                directory == "archive",
                            )
                        )
                carryover_cycle_id = frontmatter.get("carryover_cycle_id", "")
                if carryover_cycle_id:
                    entity_references.append(
                        (
                            relative_path,
                            "carryover_cycle_id",
                            carryover_cycle_id,
                            "cycle",
                            "Cycle",
                            directory == "archive",
                        )
                    )
            continuation_of = frontmatter.get("continuation_of")
            if (
                frontmatter.get("type") == "task"
                and continuation_of
                and TASK_ID_PATTERN.fullmatch(continuation_of) is not None
                and continuation_of != entity_id
            ):
                continuation_references.append(
                    (relative_path, continuation_of)
                )
            if frontmatter.get("type") == "task":
                dependencies = parse_inline_list(frontmatter.get("depends_on", ""))
                if dependencies is not None:
                    for dependency_id in dependencies:
                        if (
                            TASK_ID_PATTERN.fullmatch(dependency_id) is not None
                            and dependency_id != entity_id
                        ):
                            dependency_references.append(
                                (
                                    relative_path,
                                    entity_id,
                                    dependency_id,
                                    directory == "archive",
                                )
                            )
            if entity_id in ids:
                errors.append(
                    f"{relative_path}: duplicate id: {entity_id}; "
                    f"first seen in {ids[entity_id]}"
                )
            else:
                ids[entity_id] = relative_path

    if len(doing_tasks) > 1:
        errors.append(
            f"{doing_tasks[1]}: multiple non-archived doing Tasks; "
            f"first seen in {doing_tasks[0]}"
        )

    if len(purposes) > 1:
        errors.append(
            f"{purposes[1]}: multiple non-archived Purposes; "
            f"first seen in {purposes[0]}"
        )

    active_plan_keys: dict[tuple[str, str, str], pathlib.Path] = {}
    plan_revisions: dict[tuple[str, str, str], dict[str, pathlib.Path]] = {}
    plan_successors: dict[str, pathlib.Path] = {}
    expected_target_types = {
        "area": "area",
        "goal": "goal",
        "roadmap_outcome": "roadmap_outcome",
    }
    for plan_path, plan, allocations in time_allocation_plans:
        key = (
            plan.get("axis", ""),
            plan.get("period_kind", ""),
            plan.get("period_start", ""),
        )
        if plan.get("status") == "active":
            prior = active_plan_keys.get(key)
            if prior is not None:
                errors.append(
                    f"{plan_path}: multiple active Time Allocation Plans for "
                    f"{key}; first seen in {prior}"
                )
            else:
                active_plan_keys[key] = plan_path
        revision = plan.get("revision", "")
        if POSITIVE_INTEGER_PATTERN.fullmatch(revision) is not None:
            prior_revision = plan_revisions.setdefault(key, {}).get(revision)
            if prior_revision is not None:
                errors.append(
                    f"{plan_path}: duplicate revision {revision} for {key}; "
                    f"first seen in {prior_revision}"
                )
            else:
                plan_revisions[key][revision] = plan_path
        expected_type = expected_target_types.get(plan.get("axis", ""))
        for target_id in allocations:
            if target_id == "none" or expected_type is None:
                continue
            targets = entities_by_id.get(target_id, [])
            if not targets:
                errors.append(
                    f"{plan_path}: allocation target references missing "
                    f"{expected_type}: {target_id}"
                )
            elif len(targets) != 1 or targets[0][1].get("type") != expected_type:
                errors.append(
                    f"{plan_path}: allocation target has invalid type: {target_id}"
                )
        supersedes_id = plan.get("supersedes_id", "")
        if supersedes_id:
            prior_successor = plan_successors.get(supersedes_id)
            if prior_successor is not None:
                errors.append(
                    f"{plan_path}: multiple successors for supersedes_id "
                    f"{supersedes_id}; first seen in {prior_successor}"
                )
            else:
                plan_successors[supersedes_id] = plan_path
            targets = entities_by_id.get(supersedes_id, [])
            if len(targets) != 1 or targets[0][1].get("type") != "time_allocation_plan":
                errors.append(
                    f"{plan_path}: supersedes_id references invalid plan: "
                    f"{supersedes_id}"
                )
            else:
                prior = targets[0][1]
                prior_key = (
                    prior.get("axis", ""), prior.get("period_kind", ""),
                    prior.get("period_start", ""),
                )
                if prior_key != key:
                    errors.append(
                        f"{plan_path}: supersedes_id period key differs"
                    )
                prior_revision = prior.get("revision", "")
                if (
                    POSITIVE_INTEGER_PATTERN.fullmatch(revision) is not None
                    and int(revision) > 1
                    and prior.get("status") != "superseded"
                ):
                    errors.append(
                        f"{plan_path}: superseded plan must have status superseded: "
                        f"{supersedes_id}"
                    )
                if (
                    POSITIVE_INTEGER_PATTERN.fullmatch(revision) is not None
                    and POSITIVE_INTEGER_PATTERN.fullmatch(prior_revision) is not None
                    and int(revision) != int(prior_revision) + 1
                ):
                    errors.append(
                        f"{plan_path}: revision must increment superseded plan"
                    )

    project_lanes = ("not_started", "doing", "on_hold", "completed", "dropped")
    for lane in project_lanes:
        entries = [
            (path, project.get("kanban_position"))
            for path, project in projects
            if (
                "not_started"
                if project.get("status") == "active"
                else project.get("status")
            )
            == lane
        ]
        if not entries or any(not position for _, position in entries):
            continue
        positions = sorted(
            int(position)
            for _, position in entries
            if position is not None and PROJECT_KANBAN_POSITION_PATTERN.fullmatch(position)
        )
        if len(positions) != len(entries):
            continue
        if positions != list(range(1, len(entries) + 1)):
            errors.append(
                f"{entries[0][0]}: kanban positions for {lane} must be unique "
                "and contiguous from 1"
            )

    active_cycles = [
        (path, frontmatter)
        for path, frontmatter in cycles
        if frontmatter.get("status") == "active"
    ]
    if len(active_cycles) > 1:
        errors.append(
            f"{active_cycles[1][0]}: multiple non-archived active Cycles; "
            f"first seen in {active_cycles[0][0]}"
        )

    live_periods: list[tuple[pathlib.Path, datetime.date, datetime.date]] = []
    for cycle_path, cycle in cycles:
        if cycle.get("status") not in {"planned", "active"}:
            continue
        start = _parse_iso_date(cycle.get("start_date", ""))
        end = _parse_iso_date(cycle.get("end_date", ""))
        if start is None or end is None:
            continue
        for other_path, other_start, other_end in live_periods:
            if start <= other_end and other_start <= end:
                errors.append(
                    f"{cycle_path}: Cycle periods overlap: {other_path}"
                )
        live_periods.append((cycle_path, start, end))

    active_member_ids = {
        outcome_id
        for _, cycle in active_cycles
        for outcome_id in (parse_inline_list(cycle.get("outcome_ids", "")) or [])
    }
    lane_positions: dict[str, list[tuple[pathlib.Path, int]]] = {
        "next": [],
        "later": [],
    }
    for outcome_path, outcome in roadmap_outcomes:
        outcome_id = outcome.get("id", "")
        status = outcome.get("status")
        lane = outcome.get("roadmap_lane")
        position = outcome.get("roadmap_position")
        if status == "active" and outcome_id in active_member_ids:
            if lane or position:
                errors.append(
                    f"{outcome_path}: active Cycle member must not have roadmap_lane or roadmap_position"
                )
            outcome_text = normalized_documents[
                pathlib.PurePosixPath(outcome_path.as_posix())
            ].decode("utf-8")
            if not (
                _body_section_text_from_text(outcome_text, "## 成功条件") or ""
            ).strip():
                errors.append(
                    f"{outcome_path}: active Cycle member requires non-empty success condition"
                )
        elif status == "active" and (not lane or not position):
            errors.append(
                f"{outcome_path}: active Outcome outside active Cycle requires lane fields"
            )
        if status == "active" and outcome_id not in active_member_ids and lane in lane_positions:
            if position and POSITIVE_INTEGER_PATTERN.fullmatch(position):
                lane_positions[lane].append((outcome_path, int(position)))
    for lane, entries in lane_positions.items():
        positions = sorted(position for _, position in entries)
        if positions != list(range(1, len(entries) + 1)):
            path = entries[0][0] if entries else pathlib.Path("roadmap-outcomes")
            errors.append(
                f"{path}: roadmap positions for {lane} must be unique and contiguous from 1"
            )

    for cycle_path, cycle in cycles:
        member_ids = parse_inline_list(cycle.get("outcome_ids", "")) or []
        for outcome_id in member_ids:
            targets = entities_by_id.get(outcome_id, [])
            if len(targets) == 1 and targets[0][1].get("status") != "active" and cycle.get("status") in {"planned", "active"}:
                errors.append(
                    f"{cycle_path}: open Cycle references non-active Roadmap Outcome: {outcome_id}"
                )
            if len(targets) == 1 and cycle.get("status") == "planned":
                outcome = targets[0][1]
                lane = outcome.get("roadmap_lane")
                position = outcome.get("roadmap_position")
                if (
                    outcome.get("status") == "active"
                    and (
                        lane not in {"next", "later"}
                        or not position
                        or not POSITIVE_INTEGER_PATTERN.fullmatch(position)
                    )
                ):
                    errors.append(
                        f"{cycle_path}: planned Cycle member must remain in next/later lane: {outcome_id}"
                    )
        if cycle.get("status") in {"completed", "cancelled"}:
            carried_ids = parse_inline_list(
                cycle.get("carried_outcome_ids", "")
            ) or []
            carryover_id = cycle.get("carryover_cycle_id", "")
            if carried_ids and carryover_id:
                targets = entities_by_id.get(carryover_id, [])
                if len(targets) == 1:
                    carryover_members = parse_inline_list(
                        targets[0][1].get("outcome_ids", "")
                    ) or []
                    if not set(carried_ids) <= set(carryover_members):
                        errors.append(
                            f"{cycle_path}: carryover Cycle must contain every carried Outcome"
                        )

    for source_path, continuation_of in continuation_references:
        targets = entities_by_id.get(continuation_of, [])
        if not targets:
            errors.append(
                f"{source_path}: continuation_of references missing Task: "
                f"{continuation_of}"
            )
            continue
        if len(targets) > 1:
            errors.append(
                f"{source_path}: continuation_of reference is ambiguous: "
                f"{continuation_of}"
            )
            continue
        target = targets[0][1]
        if target.get("type") != "task":
            errors.append(
                f"{source_path}: continuation_of references non-Task entity: "
                f"{continuation_of}"
            )
        elif target.get("status") != "done":
            errors.append(
                f"{source_path}: continuation_of references Task that is not done: "
                f"{continuation_of}"
            )

    for source_path, _, dependency_id, _ in dependency_references:
        targets = entities_by_id.get(dependency_id, [])
        if not targets:
            errors.append(
                f"{source_path}: depends_on references missing Task: {dependency_id}"
            )
            continue
        if len(targets) > 1:
            errors.append(
                f"{source_path}: depends_on reference is ambiguous: {dependency_id}"
            )
            continue
        _, target, target_archived = targets[0]
        if target.get("type") != "task":
            errors.append(
                f"{source_path}: depends_on references non-Task entity: {dependency_id}"
            )
        elif target.get("title") == "5分休憩" or target.get("timer_kind") == "break":
            errors.append(
                f"{source_path}: depends_on references break Task: {dependency_id}"
            )
        elif target_archived and target.get("status") != "done":
            errors.append(
                f"{source_path}: depends_on references archived Task that is not done: "
                f"{dependency_id}"
            )

    waiting_paths: dict[str, pathlib.Path] = {}
    for entity_id, targets in entities_by_id.items():
        if len(targets) != 1:
            continue
        path, frontmatter, archived = targets[0]
        if (
            not archived
            and frontmatter.get("type") == "task"
            and frontmatter.get("status") == "waiting"
        ):
            waiting_paths[entity_id] = path
    waiting_graph: dict[str, set[str]] = {entity_id: set() for entity_id in waiting_paths}
    for _, source_id, dependency_id, source_archived in dependency_references:
        if (
            not source_archived
            and source_id in waiting_graph
            and dependency_id in waiting_graph
        ):
            waiting_graph[source_id].add(dependency_id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_waiting(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        if any(visit_waiting(dependency_id) for dependency_id in sorted(waiting_graph[task_id])):
            return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    for task_id in sorted(waiting_graph):
        if task_id not in visited and visit_waiting(task_id):
            errors.append(
                f"{waiting_paths[task_id]}: waiting Task dependency cycle"
            )
            break

    for (
        source_path,
        field,
        target_id,
        target_type,
        target_label,
        source_is_archived,
    ) in entity_references:
        targets = entities_by_id.get(target_id, [])
        if not targets:
            errors.append(
                f"{source_path}: {field} references missing {target_label}: "
                f"{target_id}"
            )
            continue
        _, target, target_is_archived = targets[0]
        if target.get("type") != target_type:
            errors.append(
                f"{source_path}: {field} references non-{target_label} entity: "
                f"{target_id}"
            )
        elif (
            target_is_archived
            and not source_is_archived
            and entity_types_by_path.get(source_path) != "progress"
        ):
            errors.append(
                f"{source_path}: {field} references archived {target_label}: "
                f"{target_id}"
            )

    return errors


def validate_repository(root: pathlib.Path) -> list[str]:
    """Validate all entity Markdown files below the supported directories."""
    if not root.is_dir():
        return [f"{root}: root is not a directory"]

    documents: dict[str, bytes] = {}
    for directory, _ in (*ENTITY_CONTRACTS, ("archive", None)):
        entity_root = root / directory
        if not entity_root.is_dir():
            continue
        for path in sorted(entity_root.rglob("*.md")):
            if path.name != "README.md":
                documents[path.relative_to(root).as_posix()] = path.read_bytes()
    return validate_document_map(documents)


def main(argv: list[str]) -> int:
    """Run repository validation for the root supplied on the command line."""
    if len(argv) != 1:
        print("usage: validate_frontmatter.py ROOT", file=sys.stderr)
        return 2

    errors = validate_repository(pathlib.Path(argv[0]))
    if errors:
        for error in errors:
            print(error)
        return 1

    print("validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
