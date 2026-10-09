"""Socket-independent JSON API handlers for repository reads and mutations."""

from __future__ import annotations

import datetime
import difflib
import hashlib
import hmac
import json
import re
import threading
import time
import unicodedata
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from webapp.local_sync_status import (
    read_calendar_sync_status,
    unavailable_status,
    validate_status_document,
)

from webapp.security import (
    ForbiddenRequestError,
    SecurityInputError,
    validate_entity_id,
    validate_mutation_headers,
)
from webapp.suggestions import build_quick_start_suggestions
from webapp.progress_report import monthly_progress_report, progress_period
from webapp.store import (
    ConflictError,
    DestinationCollision,
    DestinationConflict,
    Entity,
    InputError,
    MutationRecoveryRequired,
    ProjectOperationError,
    RoadmapOperationError,
    MutationPlan,
    MutationPlanConflict,
    NotFoundError,
    SchemaError,
    SourceOwnershipConflict,
    Store,
    StoreLockTimeout,
    WorkflowEffect,
    WorkflowPlan,
    WorkflowPostCommitCleanupError,
    WorkflowRollbackError,
    focus_monitor_pause_deadline,
)


BIND_ORIGIN = "http://127.0.0.1:24873"
_KINDS_BY_TYPE = {
    "task": "tasks",
    "purpose": "purposes",
    "vision": "visions",
    "area": "areas",
    "project": "projects",
    "goal": "goals",
    "roadmap_outcome": "roadmap_outcomes",
    "cycle": "cycles",
    "progress": "progress",
    "review": "reviews",
    "time_allocation_plan": "time_allocation_plans",
}
_TASK_STATUSES = (
    "inbox",
    "planned",
    "next",
    "doing",
    "waiting",
    "scheduled",
    "someday",
    "done",
)
_SNAPSHOT_QUERY_KEYS = {
    "availability",
    "today",
    "status",
    "project_id",
    "unassigned",
    "goal_id",
    "roadmap_outcome_id",
    "area_id",
    "context",
    "max_minutes",
    "due_before",
    "scheduled_before",
}
_POSITIVE_ASCII_INTEGER = re.compile(r"[1-9][0-9]*", re.ASCII)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_ISO_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}", re.ASCII)
_ISO_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)
_CONTEXT_TOKEN = re.compile(r"[^\s,\[\]\"'\x00-\x1f\x7f]+")
_MOKVIA_LOCAL_TIMEZONE = datetime.timezone(datetime.timedelta(hours=9))
_MUTATION_ROUTES = {
    "/api/v1/mutations/preview",
    "/api/v1/mutations/apply",
}
_LEGACY_MUTATION_ACTIONS = frozenset({"create", "update", "archive"})
_WORKFLOW_MUTATION_ACTIONS = frozenset(
    {
        "start", "interrupt", "complete", "create_and_start", "start_break",
        "project_task_plan_create", "project_task_plan_update",
        "project_task_move", "project_task_plan_migrate",
        "project_move",
        "roadmap_move", "cycle_activate", "cycle_close",
        "roadmap_outcome_bundle_update",
        "resource_allocation_create", "resource_allocation_revise",
        "resource_allocation_withdraw", "resource_allocation_expand",
    }
)
_ROADMAP_MUTATION_ACTIONS = frozenset(
    {
        "roadmap_move", "cycle_activate", "cycle_close",
        "roadmap_outcome_bundle_update",
    }
)
_KIND_TO_TYPE = {value: key for key, value in _KINDS_BY_TYPE.items()}
_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_MAX_BODY_BYTES = 1_048_576
_MAX_JSON_DEPTH = 64
_PREVIEW_TTL_SECONDS = 15 * 60
_MAX_LIVE_PREVIEWS = 128


class _InvalidJson(Exception):
    pass


class _TokenOnlyPreview(Exception):
    pass


@dataclass(frozen=True)
class _PreviewRecord:
    plan: MutationPlan | WorkflowPlan
    preview_bytes: bytes
    operation: dict[str, object]
    preview_hash: str
    expires_at: float


class ApiState:
    """Thread-safe, Store-bound registry of exact server-issued previews."""

    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _PREVIEW_TTL_SECONDS,
        max_previews: int = _MAX_LIVE_PREVIEWS,
    ) -> None:
        if not callable(clock) or not (0 < ttl_seconds <= _PREVIEW_TTL_SECONDS):
            raise ValueError("invalid preview registry clock or TTL")
        if not isinstance(max_previews, int) or not (1 <= max_previews <= _MAX_LIVE_PREVIEWS):
            raise ValueError("invalid preview registry capacity")
        self._store_ref = weakref.ref(store)
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._max_previews = max_previews
        self._lock = threading.Lock()
        self._records: OrderedDict[str, _PreviewRecord] = OrderedDict()

    def _matches_store(self, store: Store) -> bool:
        return self._store_ref() is store

    def _prune_locked(self, now: float) -> None:
        expired = [
            preview_hash
            for preview_hash, record in self._records.items()
            if record.expires_at <= now
        ]
        for preview_hash in expired:
            self._records.pop(preview_hash, None)

    def register(
        self,
        store: Store,
        plan: MutationPlan | WorkflowPlan,
        preview_bytes: bytes,
        operation: dict[str, object],
        preview_hash: str,
    ) -> None:
        with self._lock:
            if not self._matches_store(store):
                raise ValueError("preview state belongs to another Store")
            now = self._clock()
            self._prune_locked(now)
            self._records.pop(preview_hash, None)
            self._records[preview_hash] = _PreviewRecord(
                plan=plan,
                preview_bytes=bytes(preview_bytes),
                operation=_copy_json_object(operation),
                preview_hash=preview_hash,
                expires_at=now + self._ttl_seconds,
            )
            while len(self._records) > self._max_previews:
                self._records.popitem(last=False)

    def consume(
        self,
        store: Store,
        preview_hash: str,
        submitted_bytes: bytes,
    ) -> _PreviewRecord | None:
        """Consume only an exact, live record; mismatches leave it usable."""
        with self._lock:
            if not self._matches_store(store):
                return None
            self._prune_locked(self._clock())
            record = self._records.get(preview_hash)
            if record is None:
                return None
            if not hmac.compare_digest(record.preview_bytes, submitted_bytes):
                return None
            return self._records.pop(preview_hash)

    def token_action(self, store: Store, preview_hash: str) -> str | None:
        """Inspect one live token without consuming it."""
        with self._lock:
            if not self._matches_store(store):
                return None
            self._prune_locked(self._clock())
            record = self._records.get(preview_hash)
            if record is None:
                return None
            action = record.operation.get("action")
            return action if isinstance(action, str) else None

    def consume_token(
        self, store: Store, preview_hash: str
    ) -> _PreviewRecord | None:
        """Consume only a live allocation-expansion record by its token."""
        with self._lock:
            if not self._matches_store(store):
                return None
            self._prune_locked(self._clock())
            record = self._records.get(preview_hash)
            if (
                record is None
                or record.operation.get("action")
                != "resource_allocation_expand"
            ):
                return None
            return self._records.pop(preview_hash)

    def consume_legacy(
        self,
        store: Store,
        preview_hash: str,
        submitted_bytes: bytes,
    ) -> _PreviewRecord | None:
        """Atomically consume only a live non-expansion full preview."""
        with self._lock:
            if not self._matches_store(store):
                return None
            self._prune_locked(self._clock())
            record = self._records.get(preview_hash)
            if record is None:
                return None
            if not hmac.compare_digest(record.preview_bytes, submitted_bytes):
                return None
            if record.operation.get("action") == "resource_allocation_expand":
                raise _TokenOnlyPreview
            return self._records.pop(preview_hash)


_DEFAULT_STATES: weakref.WeakKeyDictionary[Store, ApiState] = weakref.WeakKeyDictionary()
_DEFAULT_STATES_LOCK = threading.Lock()


def _state_for(store: Store, state: ApiState | None) -> ApiState | None:
    if state is not None:
        return state if isinstance(state, ApiState) and state._matches_store(store) else None
    with _DEFAULT_STATES_LOCK:
        existing = _DEFAULT_STATES.get(store)
        if existing is None:
            existing = ApiState(store)
            _DEFAULT_STATES[store] = existing
        return existing


class _InvalidQuery(Exception):
    pass


def _error(
    status: int,
    code: str,
    message: str,
    details: object | None = None,
) -> tuple[int, dict[str, object]]:
    return status, {
        "error": {
            "code": code,
            "message": message,
            "details": [] if details is None else details,
        }
    }


def _copy_json_object(value: dict[str, object]) -> dict[str, object]:
    """Return a detached JSON-only copy without invoking custom serializers."""
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise _InvalidJson


def _validate_json_tree(root: object) -> None:
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise _InvalidJson
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise _InvalidJson from error


def _strict_json_object(body: bytes | None) -> dict[str, object]:
    if type(body) is not bytes or not body:
        raise _InvalidJson
    if len(body) > _MAX_BODY_BYTES:
        raise OverflowError
    if body.startswith(b"\xef\xbb\xbf"):
        raise _InvalidJson
    try:
        text = body.decode("utf-8", errors="strict")
        decoder = json.JSONDecoder(
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        value, end = decoder.raw_decode(text)
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        OverflowError,
        _InvalidJson,
    ) as error:
        raise _InvalidJson from error
    if end != len(text) or type(value) is not dict:
        raise _InvalidJson
    _validate_json_tree(value)
    return value


def _is_archived(entity: Entity) -> bool:
    return entity.relative_path.startswith("archive/")


def _kind(entity: Entity) -> str | None:
    return _KINDS_BY_TYPE.get(entity.entity_type)


def _summary(entity: Entity) -> dict[str, object]:
    kind = _kind(entity)
    if kind is None:
        raise InputError("repository contains an unsupported entity type")
    return {
        "id": entity.entity_id,
        "kind": kind,
        "type": entity.entity_type,
        "path": entity.relative_path,
        "frontmatter": dict(entity.frontmatter),
        "content_hash": entity.content_hash,
        "archived": False,
    }


def _detail(entity: Entity) -> dict[str, object]:
    kind = _kind(entity)
    if kind is None:
        raise InputError("repository contains an unsupported entity type")
    return {
        "id": entity.entity_id,
        "kind": kind,
        "type": entity.entity_type,
        "path": entity.relative_path,
        "frontmatter": dict(entity.frontmatter),
        "body": entity.body,
        "content_hash": entity.content_hash,
        "archived": _is_archived(entity),
    }


def _require_exact_keys(
    value: dict[str, object], required: set[str], optional: set[str] = set()
) -> None:
    keys = set(value)
    if not required <= keys or not keys <= required | optional:
        raise InputError("mutation object keys are invalid")


def _operation_fields(value: object) -> dict[str, str]:
    if type(value) is not dict or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise InputError("mutation fields must contain only strings")
    return dict(value)


def _operation_allocations(value: object) -> dict[str, int]:
    if type(value) is not dict or any(
        type(key) is not str or type(item) is not int
        for key, item in value.items()
    ):
        raise InputError("mutation allocations are invalid")
    return dict(value)


def _operation_kind(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or value not in _KIND_TO_TYPE:
        raise InputError("mutation kind is invalid")
    return value, _KIND_TO_TYPE[value]


def _operation_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise InputError("mutation hash is invalid")
    return value


def _current_of_requested_kind(store: Store, kind: str, entity_id: object) -> Entity:
    validated_id = _operation_entity_id(entity_id)
    current = store.get_entity(validated_id)
    if _kind(current) != kind:
        raise NotFoundError("entity was not found")
    return current


def _operation_entity_id(value: object) -> str:
    if not isinstance(value, str):
        raise InputError("mutation entity ID is invalid")
    try:
        validate_entity_id(value)
    except SecurityInputError as error:
        raise InputError("mutation entity ID is invalid") from error
    return value


def _mokvia_now() -> datetime.datetime:
    return datetime.datetime.now(_MOKVIA_LOCAL_TIMEZONE)


def _plan_operation(
    store: Store, request: dict[str, object]
) -> tuple[MutationPlan | WorkflowPlan, dict[str, object]]:
    action = request.get("action")
    if action == "resource_allocation_expand":
        _require_exact_keys(request, {
            "action", "kind", "source_id", "source_base_hash",
            "target_period_kinds",
        })
        kind, _ = _operation_kind(request["kind"])
        if (
            kind != "time_allocation_plans"
            or type(request["target_period_kinds"]) is not list
        ):
            raise InputError("allocation expansion input is invalid")
        source_id = _operation_entity_id(request["source_id"])
        source_base_hash = _operation_hash(request["source_base_hash"])
        plan = store.plan_resource_allocation_expand(
            source_id,
            source_base_hash,
            tuple(request["target_period_kinds"]),
        )
        normalized_kinds = json.loads(
            dict(plan.operation_inputs)["target_period_kinds"]
        )
        return plan, {
            "action": action,
            "kind": kind,
            "source_id": source_id,
            "source_base_hash": source_base_hash,
            "target_period_kinds": normalized_kinds,
        }

    if action == "resource_allocation_create":
        _require_exact_keys(request, {
            "action", "kind", "axis", "period_kind", "period_start",
            "input_mode", "total_minutes", "allocations",
        })
        kind, _ = _operation_kind(request["kind"])
        if kind != "time_allocation_plans":
            raise InputError("allocation workflow kind is invalid")
        axis = request["axis"]
        period_kind = request["period_kind"]
        period_start = request["period_start"]
        input_mode = request["input_mode"]
        total_minutes = request["total_minutes"]
        if (
            type(axis) is not str
            or type(period_kind) is not str
            or type(period_start) is not str
            or type(input_mode) is not str
            or type(total_minutes) is not int
        ):
            raise InputError("allocation workflow input is invalid")
        allocations = _operation_allocations(request["allocations"])
        plan = store.plan_resource_allocation_create(
            axis, period_kind, period_start, input_mode,
            total_minutes, allocations,
        )
        return plan, {
            "action": action, "kind": kind, "axis": axis,
            "period_kind": period_kind, "period_start": period_start,
            "input_mode": input_mode, "total_minutes": total_minutes,
            "allocations": allocations,
        }

    if action == "resource_allocation_revise":
        _require_exact_keys(request, {
            "action", "kind", "id", "base_hash", "input_mode",
            "total_minutes", "allocations",
        })
        kind, _ = _operation_kind(request["kind"])
        if kind != "time_allocation_plans":
            raise InputError("allocation workflow kind is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        input_mode = request["input_mode"]
        total_minutes = request["total_minutes"]
        if type(input_mode) is not str or type(total_minutes) is not int:
            raise InputError("allocation workflow input is invalid")
        allocations = _operation_allocations(request["allocations"])
        plan = store.plan_resource_allocation_revise(
            current.entity_id, base_hash, input_mode,
            total_minutes, allocations,
        )
        return plan, {
            "action": action, "kind": kind, "id": current.entity_id,
            "base_hash": base_hash, "input_mode": input_mode,
            "total_minutes": total_minutes, "allocations": allocations,
        }

    if action == "resource_allocation_withdraw":
        _require_exact_keys(request, {"action", "kind", "id", "base_hash"})
        kind, _ = _operation_kind(request["kind"])
        if kind != "time_allocation_plans":
            raise InputError("allocation workflow kind is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        plan = store.plan_resource_allocation_withdraw(
            current.entity_id, base_hash
        )
        return plan, {
            "action": action, "kind": kind, "id": current.entity_id,
            "base_hash": base_hash,
        }

    if action == "create":
        kind, entity_type = _operation_kind(request.get("kind"))
        if kind == "time_allocation_plans":
            raise InputError("Time Allocation Plans require dedicated workflows")
        if kind == "reviews":
            _require_exact_keys(
                request,
                {"action", "kind", "fields", "review_kind"},
                {"body"},
            )
            review_kind = request["review_kind"]
            if review_kind not in {"daily", "weekly"}:
                raise InputError("review kind is invalid")
        else:
            optional = {"body", "id"} if kind == "tasks" else {"body"}
            _require_exact_keys(request, {"action", "kind", "fields"}, optional)
            review_kind = None
        fields = _operation_fields(request["fields"])
        if entity_type == "task" and fields.get("status") == "doing":
            raise InputError("generic Task create cannot start work")
        body = request.get("body", "")
        if not isinstance(body, str):
            raise InputError("mutation body is invalid")
        requested_id = request.get("id")
        if "id" in request and not isinstance(requested_id, str):
            raise InputError("mutation create ID is invalid")
        plan = store.plan_create_entity(
            entity_type,
            fields,
            body,
            review_kind,
            requested_id=requested_id,
        )
        operation: dict[str, object] = {
            "action": "create",
            "kind": kind,
            "fields": dict(fields),
            "body": body,
        }
        if review_kind is not None:
            operation["review_kind"] = review_kind
        if requested_id is not None:
            operation["id"] = requested_id
        return plan, operation

    if action == "update":
        _require_exact_keys(
            request,
            {"action", "kind", "id", "base_hash", "fields"},
            {"body", "confirm_blocked_next"},
        )
        kind, entity_type = _operation_kind(request["kind"])
        if kind == "time_allocation_plans":
            raise InputError("Time Allocation Plans require dedicated workflows")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        fields = _operation_fields(request["fields"])
        body = request.get("body")
        if body is not None and not isinstance(body, str):
            raise InputError("mutation body is invalid")
        confirm_blocked_next = request.get("confirm_blocked_next", False)
        if type(confirm_blocked_next) is not bool:
            raise InputError("blocked Next confirmation must be boolean")
        try:
            plan = store.plan_update_entity(
                current.entity_id,
                base_hash,
                fields,
                body,
                confirm_blocked_next=confirm_blocked_next,
            )
        except ConflictError as error:
            if error.current.entity_type != entity_type:
                raise NotFoundError("entity was not found") from None
            raise
        operation: dict[str, object] = {
            "action": "update",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
            "fields": dict(fields),
            "body": body,
        }
        if confirm_blocked_next:
            operation["confirm_blocked_next"] = True
        return plan, operation

    if action == "archive":
        _require_exact_keys(request, {"action", "kind", "id", "base_hash"})
        kind, entity_type = _operation_kind(request["kind"])
        if kind == "time_allocation_plans":
            raise InputError("Time Allocation Plans cannot be archived")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        try:
            plan = store.plan_archive_entity(current.entity_id, base_hash)
        except ConflictError as error:
            if error.current.entity_type != entity_type:
                raise NotFoundError("entity was not found") from None
            raise
        return plan, {
            "action": "archive",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
        }

    if action == "roadmap_move":
        _require_exact_keys(
            request,
            {"action", "kind", "id", "base_hash", "lane", "position"},
        )
        kind, _ = _operation_kind(request["kind"])
        lane = request["lane"]
        position = request["position"]
        if (
            kind != "roadmap_outcomes"
            or lane not in {"next", "later"}
            or type(position) is not int
            or position < 1
        ):
            raise RoadmapOperationError("roadmap_operation_invalid", 400)
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        plan = store.plan_roadmap_move(
            current.entity_id, base_hash, lane, position
        )
        return plan, {
            "action": "roadmap_move",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
            "lane": lane,
            "position": position,
        }

    if action == "project_move":
        _require_exact_keys(
            request,
            {"action", "kind", "id", "base_hash", "status", "position"},
        )
        kind, _ = _operation_kind(request["kind"])
        status = request["status"]
        position = request["position"]
        if (
            kind != "projects"
            or status not in {
                "not_started", "doing", "on_hold", "completed", "dropped"
            }
            or type(position) is not int
            or position < 1
        ):
            raise InputError("Project move request is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        plan = store.plan_project_move(
            current.entity_id, base_hash, status, position
        )
        return plan, {
            "action": "project_move",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
            "status": status,
            "position": position,
        }

    if action == "roadmap_outcome_bundle_update":
        _require_exact_keys(
            request,
            {
                "action", "kind", "id", "base_hash", "fields",
                "project_changes",
            },
            {"body"},
        )
        kind, _ = _operation_kind(request["kind"])
        if kind != "roadmap_outcomes":
            raise RoadmapOperationError("roadmap_operation_invalid", 400)
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        fields = _operation_fields(request["fields"])
        body = request.get("body")
        changes = request["project_changes"]
        if (
            body is not None and not isinstance(body, str)
        ) or type(changes) is not list:
            raise RoadmapOperationError("roadmap_operation_invalid", 400)

        normalized_changes: list[dict[str, object]] = []
        for change in changes:
            if type(change) is not dict or type(change.get("action")) is not str:
                raise RoadmapOperationError("roadmap_operation_invalid", 400)
            project_action = change["action"]
            if project_action == "create":
                _require_exact_keys(change, {"action", "fields"}, {"body"})
                project_fields = _operation_fields(change["fields"])
                project_body = change.get("body", "")
                if not isinstance(project_body, str):
                    raise RoadmapOperationError("roadmap_operation_invalid", 400)
                normalized_changes.append(
                    {
                        "action": "create",
                        "fields": dict(project_fields),
                        "body": project_body,
                    }
                )
                continue
            if project_action == "update":
                _require_exact_keys(
                    change,
                    {"action", "id", "base_hash", "fields"},
                    {"body"},
                )
                project_body = change.get("body")
                if project_body is not None and not isinstance(project_body, str):
                    raise RoadmapOperationError("roadmap_operation_invalid", 400)
                normalized_changes.append(
                    {
                        "action": "update",
                        "id": _operation_entity_id(change["id"]),
                        "base_hash": _operation_hash(change["base_hash"]),
                        "fields": dict(_operation_fields(change["fields"])),
                        "body": project_body,
                    }
                )
                continue
            if project_action == "move":
                _require_exact_keys(change, {"action", "id", "base_hash"})
                normalized_changes.append(
                    {
                        "action": "move",
                        "id": _operation_entity_id(change["id"]),
                        "base_hash": _operation_hash(change["base_hash"]),
                    }
                )
                continue
            if project_action == "archive":
                _require_exact_keys(
                    change,
                    {"action", "id", "base_hash", "task_cascade"},
                )
                cascade = change["task_cascade"]
                if type(cascade) is not list:
                    raise RoadmapOperationError("roadmap_operation_invalid", 400)
                normalized_cascade: list[dict[str, str]] = []
                for task in cascade:
                    if type(task) is not dict:
                        raise RoadmapOperationError(
                            "roadmap_operation_invalid", 400
                        )
                    _require_exact_keys(task, {"id", "base_hash"})
                    normalized_cascade.append(
                        {
                            "id": _operation_entity_id(task["id"]),
                            "base_hash": _operation_hash(task["base_hash"]),
                        }
                    )
                normalized_changes.append(
                    {
                        "action": "archive",
                        "id": _operation_entity_id(change["id"]),
                        "base_hash": _operation_hash(change["base_hash"]),
                        "task_cascade": normalized_cascade,
                    }
                )
                continue
            raise RoadmapOperationError("roadmap_operation_invalid", 400)

        plan = store.plan_roadmap_outcome_bundle_update(
            current.entity_id,
            base_hash,
            fields,
            body,
            normalized_changes,
        )
        return plan, {
            "action": "roadmap_outcome_bundle_update",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
            "fields": dict(fields),
            "body": body,
            "project_changes": normalized_changes,
        }

    if action == "cycle_activate":
        _require_exact_keys(request, {"action", "kind", "id", "base_hash"})
        kind, _ = _operation_kind(request["kind"])
        if kind != "cycles":
            raise RoadmapOperationError("roadmap_operation_invalid", 400)
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        plan = store.plan_cycle_activate(current.entity_id, base_hash)
        return plan, {
            "action": "cycle_activate",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
        }

    if action == "cycle_close":
        required = {
            "action", "kind", "id", "base_hash", "status",
            "outcome_results", "retrospective",
        }
        optional = {"cancellation_reason", "carryover_cycle_id"}
        _require_exact_keys(request, required, optional)
        kind, _ = _operation_kind(request["kind"])
        status = request["status"]
        retrospective = request["retrospective"]
        results_value = request["outcome_results"]
        if (
            kind != "cycles"
            or status not in {"completed", "cancelled"}
            or not isinstance(retrospective, str)
            or type(results_value) is not dict
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in results_value.items()
            )
            or any(
                key in request and not isinstance(request[key], str)
                for key in optional
            )
        ):
            raise RoadmapOperationError("roadmap_operation_invalid", 400)
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        outcome_results = dict(results_value)
        cancellation_reason = request.get("cancellation_reason")
        carryover_cycle_id = request.get("carryover_cycle_id")
        plan = store.plan_cycle_close(
            current.entity_id,
            base_hash,
            status=status,
            outcome_results=outcome_results,
            retrospective=retrospective,
            cancellation_reason=cancellation_reason,
            carryover_cycle_id=carryover_cycle_id,
        )
        operation: dict[str, object] = {
            "action": "cycle_close",
            "kind": kind,
            "id": current.entity_id,
            "base_hash": base_hash,
            "status": status,
            "outcome_results": outcome_results,
            "retrospective": retrospective,
        }
        if cancellation_reason is not None:
            operation["cancellation_reason"] = cancellation_reason
        if carryover_cycle_id is not None:
            operation["carryover_cycle_id"] = carryover_cycle_id
        return plan, operation

    if action == "project_task_plan_create":
        tree_mode = request.get("mode") == "tree"
        required = {"action", "kind", "id", "base_hash", "tasks"}
        if tree_mode:
            required.add("mode")
        _require_exact_keys(request, required)
        kind, _ = _operation_kind(request["kind"])
        if (
            kind != "projects"
            or type(request["tasks"]) is not list
            or ("mode" in request and not tree_mode)
        ):
            raise InputError("Project Task plan request is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        normalized_tasks: list[dict[str, object]] = []
        for task in request["tasks"]:
            if type(task) is not dict:
                raise InputError("Project Task plan item is invalid")
            if tree_mode:
                item_keys = {"key", "title", "parent_key"}
                if "status" in task:
                    item_keys.add("status")
                _require_exact_keys(task, item_keys)
            else:
                _require_exact_keys(task, {"key", "title", "depends_on_keys"})
            key = task["key"]
            title = task["title"]
            parent_key = task.get("parent_key")
            depends_on_keys = task.get("depends_on_keys")
            task_status = task.get("status")
            if (
                not isinstance(key, str)
                or not isinstance(title, str)
                or (
                    tree_mode
                    and parent_key is not None
                    and not isinstance(parent_key, str)
                )
                or (
                    not tree_mode
                    and (
                        type(depends_on_keys) is not list
                        or any(not isinstance(item, str) for item in depends_on_keys)
                    )
                )
                or (
                    tree_mode
                    and (
                        (parent_key is not None and "status" in task)
                        or ("status" in task and task_status not in {"planned", "next"})
                    )
                )
            ):
                raise InputError("Project Task plan item is invalid")
            normalized_tasks.append(
                {
                    "key": key,
                    "title": title,
                    **(
                        {
                            "parent_key": parent_key,
                            **({"status": task_status} if "status" in task else {}),
                        }
                        if tree_mode
                        else {"depends_on_keys": list(depends_on_keys)}
                    ),
                }
            )
        plan = store.plan_project_task_plan_create(
            current.entity_id,
            base_hash,
            normalized_tasks,
            mode="tree" if tree_mode else None,
        )
        operation: dict[str, object] = {
            "action": "project_task_plan_create",
            "kind": "projects",
            "id": current.entity_id,
            "base_hash": base_hash,
            "tasks": normalized_tasks,
        }
        if tree_mode:
            operation["mode"] = "tree"
        return plan, operation

    if action == "project_task_plan_update":
        _require_exact_keys(
            request,
            {"action", "kind", "id", "base_hash", "nodes", "archives"},
        )
        kind, _ = _operation_kind(request["kind"])
        if (
            kind != "projects"
            or type(request["nodes"]) is not list
            or type(request["archives"]) is not list
        ):
            raise InputError("Project Task plan update request is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        normalized_nodes: list[dict[str, object]] = []
        for node in request["nodes"]:
            if type(node) is not dict:
                raise InputError("Project Task plan update node is invalid")
            existing = "id" in node or "base_hash" in node
            required = (
                {"key", "id", "base_hash", "title", "parent_key", "status"}
                if existing
                else {"key", "title", "parent_key", "status"}
            )
            _require_exact_keys(node, required)
            key = node["key"]
            title = node["title"]
            parent_key = node["parent_key"]
            status = node["status"]
            if (
                not isinstance(key, str)
                or not isinstance(title, str)
                or (parent_key is not None and not isinstance(parent_key, str))
                or not isinstance(status, str)
            ):
                raise InputError("Project Task plan update node is invalid")
            normalized: dict[str, object] = {
                "key": key,
                "title": title,
                "parent_key": parent_key,
                "status": status,
            }
            if existing:
                normalized["id"] = _operation_entity_id(node["id"])
                normalized["base_hash"] = _operation_hash(node["base_hash"])
            normalized_nodes.append(normalized)
        normalized_archives: list[dict[str, str]] = []
        for archive in request["archives"]:
            if type(archive) is not dict:
                raise InputError("Project Task plan archive is invalid")
            _require_exact_keys(archive, {"id", "base_hash"})
            normalized_archives.append(
                {
                    "id": _operation_entity_id(archive["id"]),
                    "base_hash": _operation_hash(archive["base_hash"]),
                }
            )
        plan = store.plan_project_task_plan_update(
            current.entity_id,
            base_hash,
            normalized_nodes,
            normalized_archives,
        )
        return plan, {
            "action": "project_task_plan_update",
            "kind": "projects",
            "id": current.entity_id,
            "base_hash": base_hash,
            "nodes": normalized_nodes,
            "archives": normalized_archives,
        }

    if action == "project_task_move":
        _require_exact_keys(
            request,
            {
                "action", "kind", "id", "base_hash", "project_id", "status",
                "primary_parent_id", "position",
            },
        )
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Project Task move request is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        project_id = _operation_entity_id(request["project_id"])
        task_status = request["status"]
        primary_parent_id = request["primary_parent_id"]
        position = request["position"]
        if (
            not isinstance(task_status, str)
            or (
                primary_parent_id is not None
                and not isinstance(primary_parent_id, str)
            )
            or type(position) is not int
        ):
            raise InputError("Project Task move request is invalid")
        if primary_parent_id is not None:
            primary_parent_id = _operation_entity_id(primary_parent_id)
        plan = store.plan_project_task_move(
            current.entity_id,
            base_hash,
            project_id=project_id,
            status=task_status,
            primary_parent_id=primary_parent_id,
            position=position,
        )
        return plan, {
            "action": "project_task_move",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": base_hash,
            "project_id": project_id,
            "status": task_status,
            "primary_parent_id": primary_parent_id,
            "position": position,
        }

    if action == "project_task_plan_migrate":
        _require_exact_keys(request, {"action", "kind", "id", "base_hash"})
        kind, _ = _operation_kind(request["kind"])
        if kind != "projects":
            raise InputError("Project Task migration request is invalid")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        plan = store.plan_project_task_plan_migrate(current.entity_id, base_hash)
        return plan, {
            "action": "project_task_plan_migrate",
            "kind": "projects",
            "id": current.entity_id,
            "base_hash": base_hash,
        }

    if action == "pause_notifications":
        _require_exact_keys(
            request, {"action", "kind", "id", "base_hash", "minutes"}
        )
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        minutes = request["minutes"]
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise InputError("pause duration is invalid")
        plan = store.plan_task_pause_notifications(
            current.entity_id, base_hash, minutes
        )
        return plan, {
            "action": "pause_notifications",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": base_hash,
            "minutes": minutes,
        }

    if action == "resume_notifications":
        _require_exact_keys(request, {"action", "kind", "id", "base_hash"})
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        plan = store.plan_task_resume_notifications(current.entity_id, base_hash)
        return plan, {
            "action": "resume_notifications",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": base_hash,
        }

    if action == "correct_work_session":
        kind, _ = _operation_kind(request.get("kind"))
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        current = _current_of_requested_kind(store, kind, request.get("id"))
        required = {"action", "kind", "id", "base_hash", "work_started_at"}
        if current.frontmatter.get("status") == "done":
            required.add("work_ended_at")
        _require_exact_keys(request, required)
        base_hash = _operation_hash(request["base_hash"])
        work_started_at = request["work_started_at"]
        if not isinstance(work_started_at, str):
            raise InputError("work_started_at is invalid")
        work_ended_at = request.get("work_ended_at")
        if (
            "work_ended_at" in request
            and not isinstance(work_ended_at, str)
        ):
            raise InputError("work_ended_at is invalid")
        plan = store.plan_task_correct_work_session(
            current.entity_id,
            base_hash,
            work_started_at=work_started_at,
            work_ended_at=work_ended_at,
        )
        operation: dict[str, object] = {
            "action": "correct_work_session",
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": base_hash,
            "work_started_at": work_started_at,
        }
        if work_ended_at is not None:
            operation["work_ended_at"] = work_ended_at
        return plan, operation

    if action == "start":
        _require_exact_keys(
            request,
            {"action", "kind", "id", "base_hash"},
            {"active_resolution"},
        )
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        target = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        if "active_resolution" not in request:
            plan = store.plan_task_start(target.entity_id, base_hash)
            return plan, {
                "action": "start",
                "kind": "tasks",
                "id": target.entity_id,
                "base_hash": base_hash,
            }
        active_resolution = request["active_resolution"]
        if type(active_resolution) is not dict:
            raise InputError("active resolution is invalid")
        _require_exact_keys(
            active_resolution,
            {"id", "base_hash", "action"},
            {"work_ended_at"},
        )
        active_id = _operation_entity_id(active_resolution["id"])
        active_hash = _operation_hash(active_resolution["base_hash"])
        resolution = active_resolution["action"]
        work_ended_at = active_resolution.get("work_ended_at")
        if (
            "work_ended_at" in active_resolution
            and not isinstance(work_ended_at, str)
        ):
            raise InputError("active resolution work_ended_at is invalid")
        if resolution not in {"interrupt", "complete"}:
            raise InputError("active resolution action is invalid")
        if active_id == target.entity_id:
            raise InputError("active resolution must reference another Task")
        plan = store.plan_task_start(
            target.entity_id,
            base_hash,
            active_entity_id=active_id,
            active_base_hash=active_hash,
            resolution=resolution,
            active_work_ended_at=work_ended_at,
        )
        normalized_resolution = {
            "id": active_id,
            "base_hash": active_hash,
            "action": resolution,
        }
        if work_ended_at is not None:
            normalized_resolution["work_ended_at"] = work_ended_at
        operation = {
            "action": "start",
            "kind": "tasks",
            "id": target.entity_id,
            "base_hash": base_hash,
            "active_resolution": normalized_resolution,
        }
        return plan, operation

    if action == "create_and_start":
        _require_exact_keys(
            request,
            {"action", "kind", "fields", "body"},
            {"active_resolution"},
        )
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        fields = _operation_fields(request["fields"])
        if "title" not in fields or not set(fields) <= {"title", "area_id"}:
            raise InputError("create-and-start fields are invalid")
        body = request["body"]
        if body != "":
            raise InputError("create-and-start body must be empty")

        if "active_resolution" not in request:
            plan = store.plan_task_create_and_start(
                fields["title"], body, area_id=fields.get("area_id")
            )
            return plan, {
                "action": "create_and_start",
                "kind": "tasks",
                "fields": dict(fields),
                "body": body,
            }
        active_resolution = request["active_resolution"]
        if type(active_resolution) is not dict:
            raise InputError("active resolution is invalid")
        _require_exact_keys(
            active_resolution,
            {"id", "base_hash", "action"},
            {"work_ended_at"},
        )
        active_id = _operation_entity_id(active_resolution["id"])
        active_hash = _operation_hash(active_resolution["base_hash"])
        resolution = active_resolution["action"]
        work_ended_at = active_resolution.get("work_ended_at")
        if (
            "work_ended_at" in active_resolution
            and not isinstance(work_ended_at, str)
        ):
            raise InputError("active resolution work_ended_at is invalid")
        if resolution not in {"complete", "interrupt"}:
            raise InputError("active resolution action is invalid")
        plan = store.plan_task_create_and_start(
            fields["title"],
            body,
            area_id=fields.get("area_id"),
            active_entity_id=active_id,
            active_base_hash=active_hash,
            resolution=resolution,
            active_work_ended_at=work_ended_at,
        )
        normalized_resolution = {
            "id": active_id,
            "base_hash": active_hash,
            "action": resolution,
        }
        if work_ended_at is not None:
            normalized_resolution["work_ended_at"] = work_ended_at
        return plan, {
            "action": "create_and_start",
            "kind": "tasks",
            "fields": dict(fields),
            "body": body,
            "active_resolution": normalized_resolution,
        }

    if action == "start_break":
        _require_exact_keys(
            request,
            {"action", "kind"},
            {"active_resolution"},
        )
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        if "active_resolution" not in request:
            plan = store.plan_task_start_break()
            return plan, {"action": "start_break", "kind": "tasks"}
        active_resolution = request["active_resolution"]
        if type(active_resolution) is not dict:
            raise InputError("active resolution is invalid")
        _require_exact_keys(active_resolution, {"id", "base_hash", "action"})
        active_id = _operation_entity_id(active_resolution["id"])
        active_hash = _operation_hash(active_resolution["base_hash"])
        resolution = active_resolution["action"]
        if resolution not in {"complete", "interrupt"}:
            raise InputError("active resolution action is invalid")
        plan = store.plan_task_start_break(
            active_entity_id=active_id,
            active_base_hash=active_hash,
            resolution=resolution,
        )
        return plan, {
            "action": "start_break",
            "kind": "tasks",
            "active_resolution": {
                "id": active_id,
                "base_hash": active_hash,
                "action": resolution,
            },
        }

    if action in {"interrupt", "complete"}:
        _require_exact_keys(request, {"action", "kind", "id", "base_hash"})
        kind, _ = _operation_kind(request["kind"])
        if kind != "tasks":
            raise InputError("Task workflow kind must be tasks")
        current = _current_of_requested_kind(store, kind, request["id"])
        base_hash = _operation_hash(request["base_hash"])
        if action == "interrupt":
            plan = store.plan_task_interrupt(current.entity_id, base_hash)
        else:
            plan = store.plan_task_complete(current.entity_id, base_hash)
        return plan, {
            "action": action,
            "kind": "tasks",
            "id": current.entity_id,
            "base_hash": base_hash,
        }

    raise InputError("mutation action is invalid")


def _unified_plan_diff(plan: MutationPlan) -> str:
    before = "" if plan.before_bytes is None else plan.before_bytes.decode("utf-8")
    after = plan.after_bytes.decode("utf-8")
    from_label = "/dev/null" if plan.source_relative_path is None else plan.source_relative_path
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=from_label,
            tofile=plan.destination_relative_path,
            lineterm="\n",
        )
    )
    if not diff and from_label != plan.destination_relative_path:
        return f"--- {from_label}\n+++ {plan.destination_relative_path}\n"
    return diff


def _unified_effect_diff(effect: WorkflowEffect) -> str:
    before = (
        "" if effect.before_bytes is None else effect.before_bytes.decode("utf-8")
    )
    after = effect.after_bytes.decode("utf-8")
    from_label = (
        "/dev/null"
        if effect.source_relative_path is None
        else effect.source_relative_path
    )
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=from_label,
            tofile=effect.destination_relative_path,
            lineterm="\n",
        )
    )
    if not diff and from_label != effect.destination_relative_path:
        return f"--- {from_label}\n+++ {effect.destination_relative_path}\n"
    return diff


def _workflow_effect_document(effect: WorkflowEffect) -> dict[str, object]:
    return {
        "role": effect.role,
        "before": (
            None if effect.before_entity is None else _detail(effect.before_entity)
        ),
        "proposed": _detail(effect.planned_entity),
        "diff": _unified_effect_diff(effect),
        "path_change": {
            "from": effect.source_relative_path,
            "to": effect.destination_relative_path,
        },
    }


def _compact_identity(entity: Entity) -> dict[str, object]:
    kind = _kind(entity)
    if kind is None:
        raise InputError("repository contains an unsupported entity type")
    return {
        "id": entity.entity_id,
        "kind": kind,
        "path": entity.relative_path,
        "content_hash": entity.content_hash,
    }


def _compact_resource_allocation_expand_preview(
    plan: WorkflowPlan, operation: dict[str, object]
) -> dict[str, object]:
    inputs = dict(plan.operation_inputs)
    targets = json.loads(inputs["targets"])
    target_kinds = json.loads(inputs["target_period_kinds"])
    periods: list[dict[str, object]] = []
    effect_index = 0
    overwrite_count = 0
    for target in targets:
        current = target["current"]
        current_effect = None
        if current is not None:
            current_effect = plan.effects[effect_index]
            effect_index += 1
            overwrite_count += 1
        proposed_effect = plan.effects[effect_index]
        effect_index += 1
        periods.append({
            "period_kind": target["period_kind"],
            "period_start": target["period_start"],
            "period_end": target["period_end"],
            "change": "revise" if current is not None else "create",
            "current_id": None if current is None else current["entity_id"],
            "current_revision": (
                None
                if current_effect is None
                else int(current_effect.before_entity.frontmatter["revision"])
            ),
            "proposed_id": proposed_effect.planned_entity.entity_id,
            "proposed_revision": int(
                proposed_effect.planned_entity.frontmatter["revision"]
            ),
            "total_minutes": target["total_minutes"],
        })
    if effect_index != len(plan.effects):
        raise InputError("allocation expansion effects are invalid")
    summary = {
        "source_id": inputs["source_id"],
        "axis": inputs["axis"],
        "source_period_kind": inputs["source_period_kind"],
        "source_period_start": inputs["source_period_start"],
        "target_period_kinds": target_kinds,
        "target_count": len(targets),
        "create_count": len(targets) - overwrite_count,
        "overwrite_count": overwrite_count,
        "effect_count": len(plan.effects),
    }
    return {
        "operation": _copy_json_object(operation),
        "summary": summary,
        "periods": periods,
        "effects": [
            {
                "role": effect.role,
                "before": (
                    None
                    if effect.before_entity is None
                    else _compact_identity(effect.before_entity)
                ),
                "proposed": _compact_identity(effect.planned_entity),
            }
            for effect in plan.effects
        ],
        "validation": {"ok": True, "errors": []},
    }


def _compact_resource_allocation_expand_result(
    plan: WorkflowPlan,
    operation: dict[str, object],
    results: tuple[Entity, ...],
) -> dict[str, object]:
    preview = _compact_resource_allocation_expand_preview(plan, operation)
    return {
        "operation": preview["operation"],
        "summary": preview["summary"],
        "effects": [
            {
                "role": effect.role,
                "before": (
                    None
                    if effect.before_entity is None
                    else _compact_identity(effect.before_entity)
                ),
                "proposed": _compact_identity(result),
            }
            for effect, result in zip(plan.effects, results, strict=True)
        ],
    }


def _preview_document(
    plan: MutationPlan | WorkflowPlan, operation: dict[str, object]
) -> dict[str, object]:
    if isinstance(plan, WorkflowPlan):
        return {
            "operation": _copy_json_object(operation),
            "effects": [
                _workflow_effect_document(effect) for effect in plan.effects
            ],
            "source_paths": [
                effect.source_relative_path
                for effect in plan.effects
                if effect.source_relative_path is not None
            ],
            "destination_paths": [
                effect.destination_relative_path for effect in plan.effects
            ],
            "validation": {"ok": True, "errors": []},
            "base_hash": plan.target_base_hash,
        }
    source_paths = (
        [] if plan.source_relative_path is None else [plan.source_relative_path]
    )
    return {
        "operation": _copy_json_object(operation),
        "before": None if plan.before_entity is None else _detail(plan.before_entity),
        "proposed": _detail(plan.planned_entity),
        "diff": _unified_plan_diff(plan),
        "path_change": {
            "from": plan.source_relative_path,
            "to": plan.destination_relative_path,
        },
        "source_paths": source_paths,
        "destination_paths": [plan.destination_relative_path],
        "validation": {"ok": True, "errors": []},
        "base_hash": plan.base_hash,
    }


def _safe_schema_details(
    error: SchemaError, plan: MutationPlan | None
) -> list[object]:
    """Expose only server-known relative paths and value-free error codes."""
    if plan is None:
        return [{"code": "repository_validation_failed"}]
    allowed_paths = {
        path
        for path in (
            plan.source_relative_path,
            plan.destination_relative_path,
        )
        if path is not None
    }
    details: list[object] = []
    reported_paths: set[str] = set()
    has_unmatched = False
    for item in error.errors:
        prefix = item.partition(": ")[0] if isinstance(item, str) else ""
        if prefix in allowed_paths:
            if prefix not in reported_paths:
                details.append({"path": prefix, "code": "invalid_document"})
                reported_paths.add(prefix)
        else:
            has_unmatched = True
    if has_unmatched or not details:
        details.append({"code": "repository_validation_failed"})
    return details


def _conflict_details(plan: MutationPlan | None, current: Entity) -> list[object]:
    detail: dict[str, object] = {
        "reason": "current_state_changed",
        "latest": _detail(current),
        "latest_hash": current.content_hash,
    }
    if plan is not None:
        detail["proposed"] = _detail(plan.planned_entity)
    return [detail]


def _mapped_mutation_error(
    error: Exception,
    *,
    plan: MutationPlan | WorkflowPlan | None = None,
    operation_action: str | None = None,
) -> tuple[int, dict[str, object]]:
    if isinstance(error, ProjectOperationError):
        return _error(
            error.http_status,
            error.code,
            "Project mutation was rejected",
        )
    if isinstance(error, RoadmapOperationError):
        return _error(
            error.http_status,
            error.code,
            "Roadmap mutation was rejected",
        )
    if isinstance(error, WorkflowPostCommitCleanupError):
        return _error(
            500,
            "mutation_committed_cleanup_failed",
            "mutation committed but cleanup could not be completed",
            {"committed": True},
        )
    if isinstance(error, (WorkflowRollbackError, MutationRecoveryRequired)):
        return _error(
            503,
            "mutation_recovery_required",
            "repository recovery is required before another mutation",
            {"recovery_required": True},
        )
    if isinstance(error, ConflictError):
        if isinstance(plan, WorkflowPlan) or (
            plan is None and operation_action in _WORKFLOW_MUTATION_ACTIONS
        ):
            return _error(
                409,
                "conflict",
                "mutation conflicts with repository state",
            )
        if plan is None and operation_action not in _LEGACY_MUTATION_ACTIONS:
            return _error(
                409,
                "conflict",
                "mutation conflicts with repository state",
            )
        if isinstance(plan, MutationPlan) and (
            error.current.entity_type != plan.entity_type
        ):
            return _error(
                409,
                "conflict",
                "mutation conflicts with repository state",
            )
        return _error(
            409,
            "conflict",
            "repository state changed; create a new preview",
            _conflict_details(
                plan if isinstance(plan, MutationPlan) else None,
                error.current,
            ),
        )
    if isinstance(
        error,
        (
            DestinationConflict,
            DestinationCollision,
            SourceOwnershipConflict,
            MutationPlanConflict,
        ),
    ):
        return _error(409, "conflict", "mutation conflicts with repository state")
    if isinstance(error, SchemaError):
        return _error(
            422,
            "validation_failed",
            "mutation failed repository validation",
            _safe_schema_details(
                error, plan if isinstance(plan, MutationPlan) else None
            ),
        )
    if isinstance(error, StoreLockTimeout):
        return _error(503, "busy", "repository is busy; retry with a new preview")
    if isinstance(error, NotFoundError):
        return _error(404, "not_found", "entity was not found")
    if (
        operation_action == "resource_allocation_expand"
        and isinstance(error, InputError)
        and str(error) == "time allocation plan ID suffix exhausted"
    ):
        return _error(409, "conflict", "mutation conflicts with repository state")
    if (
        isinstance(error, (InputError, SecurityInputError, TypeError, ValueError))
        and operation_action in _ROADMAP_MUTATION_ACTIONS
    ):
        return _error(
            400,
            "roadmap_operation_invalid",
            "Roadmap mutation request is invalid",
        )
    if isinstance(error, (InputError, SecurityInputError, TypeError, ValueError)):
        return _error(400, "invalid_request", "mutation request is invalid")
    return _error(500, "mutation_failed", "mutation could not be completed")


def _handle_mutation_preview(
    store: Store, request: dict[str, object], state: ApiState
) -> tuple[int, dict[str, object]]:
    try:
        store.require_mutations_available()
        plan, operation = _plan_operation(store, request)
        preview = (
            _compact_resource_allocation_expand_preview(plan, operation)
            if isinstance(plan, WorkflowPlan)
            and plan.action == "resource_allocation_expand"
            else _preview_document(plan, operation)
        )
        preview_bytes = _canonical_json_bytes(preview)
        preview_hash = hashlib.sha256(preview_bytes).hexdigest()
        state.register(store, plan, preview_bytes, operation, preview_hash)
        response = _copy_json_object(preview)
        response["preview_hash"] = preview_hash
        return 200, response
    except Exception as error:
        action = request.get("action")
        return _mapped_mutation_error(
            error,
            operation_action=action if isinstance(action, str) else None,
        )


def _handle_mutation_apply(
    store: Store, request: dict[str, object], state: ApiState
) -> tuple[int, dict[str, object]]:
    try:
        if set(request) == {"preview_hash"}:
            preview_hash = _operation_hash(request["preview_hash"])
            action = state.token_action(store, preview_hash)
            if action is not None and action != "resource_allocation_expand":
                return _error(
                    400,
                    "invalid_request",
                    "token-only apply is not allowed",
                )
            record = state.consume_token(store, preview_hash)
            if record is None:
                return _error(
                    409, "stale_preview", "preview is unknown or stale"
                )
            try:
                if (
                    not isinstance(record.plan, WorkflowPlan)
                    or record.plan.action != "resource_allocation_expand"
                ):
                    raise InputError("allocation expansion preview is invalid")
                results = store.apply_task_workflow(record.plan)
                return 200, _compact_resource_allocation_expand_result(
                    record.plan, record.operation, results
                )
            except Exception as error:
                return _mapped_mutation_error(error, plan=record.plan)
        _require_exact_keys(request, {"preview", "preview_hash"})
        preview = request["preview"]
        preview_hash = _operation_hash(request["preview_hash"])
        if type(preview) is not dict:
            raise InputError("submitted preview is invalid")
        submitted_bytes = _canonical_json_bytes(preview)
        computed = hashlib.sha256(submitted_bytes).hexdigest()
        if not hmac.compare_digest(computed, preview_hash):
            return _error(409, "stale_preview", "preview is unknown or stale")
        try:
            record = state.consume_legacy(
                store, preview_hash, submitted_bytes
            )
        except _TokenOnlyPreview:
            return _error(
                400,
                "invalid_request",
                "allocation expansion requires token-only apply",
            )
        if record is None:
            return _error(409, "stale_preview", "preview is unknown or stale")
        try:
            if isinstance(record.plan, WorkflowPlan):
                results = store.apply_task_workflow(record.plan)
                return 200, {
                    "operation": _copy_json_object(record.operation),
                    "effects": [
                        {"role": effect.role, **_detail(result)}
                        for effect, result in zip(
                            record.plan.effects, results, strict=True
                        )
                    ],
                }
            result = store.apply_mutation_plan(record.plan)
        except Exception as error:
            return _mapped_mutation_error(error, plan=record.plan)
        return (201 if record.plan.action == "create" else 200), _detail(result)
    except (InputError, SecurityInputError, TypeError, ValueError, UnicodeError):
        return _error(400, "invalid_request", "mutation request is invalid")
    except Exception:
        return _error(500, "mutation_failed", "mutation could not be completed")


def _single_query_values(
    query: Mapping[str, str | Sequence[str]] | None,
    allowed_keys: frozenset[str] | set[str] = _SNAPSHOT_QUERY_KEYS,
) -> dict[str, str]:
    if query is None:
        return {}
    if not isinstance(query, Mapping):
        raise _InvalidQuery

    result: dict[str, str] = {}
    for key, raw_value in query.items():
        if not isinstance(key, str) or key not in allowed_keys:
            raise _InvalidQuery
        if isinstance(raw_value, str):
            value = raw_value
        elif isinstance(raw_value, Sequence) and not isinstance(
            raw_value, (bytes, bytearray)
        ):
            if len(raw_value) != 1 or not isinstance(raw_value[0], str):
                raise _InvalidQuery
            value = raw_value[0]
        else:
            raise _InvalidQuery
        if not value or value.isspace():
            raise _InvalidQuery
        result[key] = value
    return result


def _parse_date(value: str) -> datetime.date:
    if _ISO_DATE.fullmatch(value) is None:
        raise _InvalidQuery
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as error:
        raise _InvalidQuery from error


def _parse_aware_timestamp(value: str) -> datetime.datetime:
    if _ISO_TIMESTAMP.fullmatch(value) is None:
        raise _InvalidQuery
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as error:
        raise _InvalidQuery from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidQuery
    return parsed


def _end_of_mokvia_date(value: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(
        value, datetime.time.max, tzinfo=_MOKVIA_LOCAL_TIMEZONE
    )


def _parse_due_cutoff(value: str) -> datetime.datetime:
    if _ISO_DATE.fullmatch(value) is not None:
        return _end_of_mokvia_date(_parse_date(value))
    return _parse_aware_timestamp(value)


def _mokvia_today() -> datetime.date:
    return datetime.datetime.now(_MOKVIA_LOCAL_TIMEZONE).date()


def _is_on_mokvia_date(value: str | None, day: datetime.date, *, date_allowed: bool) -> bool:
    if not value:
        return False
    try:
        if date_allowed and _ISO_DATE.fullmatch(value) is not None:
            return _parse_date(value) == day
        return _parse_aware_timestamp(value).astimezone(_MOKVIA_LOCAL_TIMEZONE).date() == day
    except (_InvalidQuery, OverflowError):
        return False


def _due_is_within(value: str | None, cutoff: datetime.datetime) -> bool:
    if not value:
        return False
    try:
        if _ISO_DATE.fullmatch(value) is not None:
            due = _end_of_mokvia_date(_parse_date(value))
        else:
            due = _parse_aware_timestamp(value)
    except _InvalidQuery:
        return False
    return due <= cutoff


def _ascii_positive_integer_at_most(value: str, cutoff: str) -> bool:
    """Compare canonical positive integers without converting arbitrary digits."""
    if len(value) != len(cutoff):
        return len(value) < len(cutoff)
    return value <= cutoff


def _parse_contexts(value: str | None) -> tuple[str, ...]:
    if value is None or len(value) < 2 or value[0] != "[" or value[-1] != "]":
        return ()
    contents = value[1:-1]
    if not contents.strip():
        return ()
    tokens = tuple(part.strip() for part in contents.split(","))
    if any(not token or _CONTEXT_TOKEN.fullmatch(token) is None for token in tokens):
        return ()
    return tokens


def _validated_filters(values: dict[str, str]) -> dict[str, object]:
    filters: dict[str, object] = dict(values)
    try:
        for key in ("project_id", "goal_id", "roadmap_outcome_id", "area_id"):
            if key in values:
                filters[key] = validate_entity_id(values[key])
    except SecurityInputError as error:
        raise _InvalidQuery from error

    if "context" in values and _CONTEXT_TOKEN.fullmatch(values["context"]) is None:
        raise _InvalidQuery
    if "availability" in values:
        if values["availability"] not in {"now", "later"}:
            raise _InvalidQuery
        filters["availability"] = (values["availability"], _mokvia_today())
    if "today" in values:
        if values["today"] != "1":
            raise _InvalidQuery
        filters["today"] = _mokvia_today()
    if "unassigned" in values:
        if values["unassigned"] != "1" or "project_id" in values:
            raise _InvalidQuery
        filters["unassigned"] = True
    if "max_minutes" in values:
        if _POSITIVE_ASCII_INTEGER.fullmatch(values["max_minutes"]) is None:
            raise _InvalidQuery
        filters["max_minutes"] = values["max_minutes"]
    if "due_before" in values:
        filters["due_before"] = _parse_due_cutoff(values["due_before"])
    if "scheduled_before" in values:
        filters["scheduled_before"] = _parse_aware_timestamp(
            values["scheduled_before"]
        )
    return filters


def _matches(
    entity: Entity,
    filters: dict[str, object],
    projects_by_id: Mapping[str, Entity],
) -> bool:
    frontmatter = entity.frontmatter
    if "availability" in filters:
        if entity.entity_type != "task" or frontmatter.get("status") != "next":
            return False
        availability, day = filters["availability"]  # type: ignore[misc]
        action_date = frontmatter.get("action_date")
        if action_date is None:
            if availability == "later":
                return False
        else:
            try:
                action_day = _parse_date(action_date)
            except _InvalidQuery:
                return False
            if (availability == "now" and action_day > day) or (
                availability == "later" and action_day <= day
            ):
                return False
    if "today" in filters:
        if (
            entity.entity_type != "task"
            or frontmatter.get("status") in {"doing", "planned"}
        ):
            return False
        day = filters["today"]  # type: ignore[assignment]
        if frontmatter.get("status") == "done":
            if not _is_on_mokvia_date(frontmatter.get("completed_at"), day, date_allowed=False):
                return False
        elif not (
            _is_on_mokvia_date(frontmatter.get("scheduled_start"), day, date_allowed=False)
            or _is_on_mokvia_date(frontmatter.get("due"), day, date_allowed=True)
            or _is_on_mokvia_date(frontmatter.get("action_date"), day, date_allowed=True)
            or (
                bool(frontmatter.get("continuation_of"))
                and _is_on_mokvia_date(frontmatter.get("created_at"), day, date_allowed=False)
            )
        ):
            return False
    if "status" in filters and frontmatter.get("status") != filters["status"]:
        return False

    if "project_id" in filters:
        if entity.entity_type != "task" or frontmatter.get("project_id") != filters["project_id"]:
            return False
    if "unassigned" in filters:
        if entity.entity_type != "task" or frontmatter.get("project_id"):
            return False
    if "goal_id" in filters:
        if entity.entity_type != "project" or frontmatter.get("goal_id") != filters["goal_id"]:
            return False
    if "roadmap_outcome_id" in filters:
        roadmap_outcome_id = filters["roadmap_outcome_id"]
        if entity.entity_type == "project":
            if frontmatter.get("roadmap_outcome_id") != roadmap_outcome_id:
                return False
        elif entity.entity_type == "task":
            project = projects_by_id.get(frontmatter.get("project_id", ""))
            if project is None or project.frontmatter.get("roadmap_outcome_id") != roadmap_outcome_id:
                return False
        else:
            return False
    if "area_id" in filters:
        if entity.entity_type != "task":
            return False
        project_id = frontmatter.get("project_id")
        if project_id:
            project = projects_by_id.get(project_id)
            effective_area_id = (
                None if project is None else project.frontmatter.get("area_id")
            )
        else:
            effective_area_id = frontmatter.get("area_id")
        if effective_area_id != filters["area_id"]:
            return False
    if "context" in filters:
        if entity.entity_type != "task" or filters["context"] not in _parse_contexts(
            frontmatter.get("contexts")
        ):
            return False
    if "max_minutes" in filters:
        if entity.entity_type != "task":
            return False
        estimate = frontmatter.get("estimated_minutes")
        if estimate is None or _POSITIVE_ASCII_INTEGER.fullmatch(estimate) is None:
            return False
        if not _ascii_positive_integer_at_most(
            estimate, filters["max_minutes"]  # type: ignore[arg-type]
        ):
            return False
    if "due_before" in filters:
        if entity.entity_type != "task" or not _due_is_within(
            frontmatter.get("due"), filters["due_before"]  # type: ignore[arg-type]
        ):
            return False
    if "scheduled_before" in filters:
        if entity.entity_type != "task":
            return False
        scheduled_start = frontmatter.get("scheduled_start")
        if scheduled_start is None:
            return False
        try:
            scheduled = _parse_aware_timestamp(scheduled_start)
        except _InvalidQuery:
            return False
        if scheduled > filters["scheduled_before"]:
            return False
    return True


def _facts(entities: list[Entity]) -> dict[str, object]:
    active = [entity for entity in entities if not _is_archived(entity)]
    tasks = [entity for entity in active if entity.entity_type == "task"]
    now = _mokvia_now()
    today = _mokvia_today()
    task_status_by_id = {
        entity.entity_id: entity.frontmatter.get("status")
        for entity in entities if entity.entity_type == "task"
    }
    actionable_next_ids = []
    for task in tasks:
        fields = task.frontmatter
        if fields.get("status") != "next" or fields.get("waiting_for"):
            continue
        action_date = fields.get("action_date")
        if action_date and _parse_date(action_date) > today:
            continue
        available_from = fields.get("available_from")
        if available_from:
            threshold = (
                datetime.datetime.combine(_parse_date(available_from), datetime.time(), _MOKVIA_LOCAL_TIMEZONE)
                if len(available_from) == 10 else _parse_aware_timestamp(available_from)
            )
            if threshold > now:
                continue
        if any(task_status_by_id.get(dependency_id) != "done" for dependency_id in _parse_contexts(fields.get("depends_on"))):
            continue
        actionable_next_ids.append(task.entity_id)
    doing_tasks = [
        task for task in tasks if task.frontmatter.get("status") == "doing"
    ]
    focus_pause: dict[str, str] | None = None
    if len(doing_tasks) == 1:
        task = doing_tasks[0]
        deadline = focus_monitor_pause_deadline(task.body, now)
        if deadline is not None:
            focus_pause = {"task_id": task.entity_id, "deadline": deadline}
    counts = {status: 0 for status in _TASK_STATUSES}
    for task in tasks:
        status = task.frontmatter.get("status")
        if status in counts:
            counts[status] += 1

    covered_project_ids = {
        task.frontmatter["project_id"]
        for task in tasks
        if task.frontmatter.get("status") in {"next", "doing"}
        and task.frontmatter.get("project_id")
    }
    missing_next_action = sorted(
        project.entity_id
        for project in active
        if project.entity_type == "project"
        and project.frontmatter.get("status") == "doing"
        and project.entity_id not in covered_project_ids
    )
    purpose = next(
        (entity for entity in active if entity.entity_type == "purpose"),
        None,
    )
    purpose_fact = None
    if purpose is not None:
        excerpt = next(
            (
                line.strip()
                for line in purpose.body.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
            "",
        )
        purpose_fact = {
            "id": purpose.entity_id,
            "title": purpose.frontmatter["title"],
            "excerpt": excerpt,
        }
    vision_status_counts = {
        "active": 0,
        "on_hold": 0,
        "realized": 0,
        "dropped": 0,
    }
    for vision in active:
        if vision.entity_type != "vision":
            continue
        status = vision.frontmatter.get("status")
        if status in vision_status_counts:
            vision_status_counts[status] += 1
    active_goals = [
        entity
        for entity in active
        if entity.entity_type == "goal"
        and entity.frontmatter.get("status") == "active"
    ]
    goal_target_dates = sorted(
        goal.frontmatter["target_date"]
        for goal in active_goals
        if goal.frontmatter.get("target_date")
    )
    area_health_counts = {
        "maintained": 0,
        "needs_attention": 0,
        "unreviewed": 0,
    }
    for area in active:
        if area.entity_type != "area":
            continue
        health = area.frontmatter.get("health")
        if health in {"maintained", "needs_attention"}:
            area_health_counts[health] += 1
        else:
            area_health_counts["unreviewed"] += 1
    active_cycles = [
        entity for entity in active
        if entity.entity_type == "cycle" and entity.frontmatter.get("status") == "active"
    ]
    active_cycle = active_cycles[0] if len(active_cycles) == 1 else None
    now_outcome_ids = (
        list(_parse_contexts(active_cycle.frontmatter.get("outcome_ids")))
        if active_cycle is not None
        else []
    )
    lanes: dict[str, list[tuple[int, str]]] = {"next": [], "later": []}
    for outcome in active:
        lane = outcome.frontmatter.get("roadmap_lane")
        position = outcome.frontmatter.get("roadmap_position")
        if (
            outcome.entity_type == "roadmap_outcome"
            and outcome.frontmatter.get("status") == "active"
            and lane in lanes
            and isinstance(position, str)
            and _POSITIVE_ASCII_INTEGER.fullmatch(position)
        ):
            lanes[lane].append((int(position), outcome.entity_id))
    planned_cycles = sorted(
        (
            entity for entity in active
            if entity.entity_type == "cycle" and entity.frontmatter.get("status") == "planned"
        ),
        key=lambda entity: (entity.frontmatter.get("start_date", ""), entity.entity_id),
    )
    return {
        "mokvia_today": today.isoformat(),
        "inbox_count": counts["inbox"],
        "task_status_counts": counts,
        "active_projects_without_next_action": missing_next_action,
        "focus_actionable_next_ids": sorted(actionable_next_ids),
        "focus_done_task_ids": sorted(
            entity.entity_id for entity in entities
            if entity.entity_type == "task" and entity.frontmatter.get("status") == "done"
        ),
        "archive_count": sum(_is_archived(entity) for entity in entities),
        "focus_notification_pause": focus_pause,
        "direction": {
            "purpose": purpose_fact,
            "vision_status_counts": vision_status_counts,
            "active_goal_count": len(active_goals),
            "next_goal_target_date": (
                goal_target_dates[0] if goal_target_dates else None
            ),
            "area_health_counts": area_health_counts,
            "active_project_count": sum(
                entity.entity_type == "project"
                and entity.frontmatter.get("status") in {"not_started", "doing"}
                for entity in active
            ),
        },
        "roadmap": {
            "active_cycle_id": None if active_cycle is None else active_cycle.entity_id,
            "now_outcome_ids": now_outcome_ids,
            "next_outcome_ids": [entity_id for _, entity_id in sorted(lanes["next"])],
            "later_outcome_ids": [entity_id for _, entity_id in sorted(lanes["later"])],
            "planned_cycle_ids": [entity.entity_id for entity in planned_cycles],
        },
    }


def _handle_snapshot(
    store: Store,
    query: Mapping[str, str | Sequence[str]] | None,
) -> tuple[int, dict[str, object]]:
    try:
        filters = _validated_filters(_single_query_values(query))
    except _InvalidQuery:
        return _error(400, "invalid_query", "query parameters are invalid")

    try:
        repository_snapshot = store.read_snapshot()
        all_entities = list(repository_snapshot.entities)
        active = [
            entity
            for entity in all_entities
            if not _is_archived(entity) and entity.entity_type != "progress"
        ]
        projects_by_id = {
            entity.entity_id: entity
            for entity in active
            if entity.entity_type == "project"
        }
        selected = sorted(
            (
                entity
                for entity in active
                if _matches(entity, filters, projects_by_id)
            ),
            key=lambda entity: (entity.relative_path, entity.entity_id),
        )
        summaries = [_summary(entity) for entity in selected]
        facts = _facts(all_entities)
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry snapshot")
    except (InputError, OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")
    return 200, {
        "entities": summaries,
        "facts": facts,
        "mutation_state": {
            "recovery_required": repository_snapshot.recovery_required
        },
    }


def _handle_quick_start_suggestions(
    store: Store,
    query: Mapping[str, str | Sequence[str]] | None,
) -> tuple[int, dict[str, object]]:
    try:
        if query is None:
            value = ""
        elif not isinstance(query, Mapping) or set(query) - {"q"}:
            raise _InvalidQuery
        elif "q" not in query:
            value = ""
        else:
            raw_value = query["q"]
            if isinstance(raw_value, str):
                value = raw_value
            elif (
                isinstance(raw_value, Sequence)
                and not isinstance(raw_value, (bytes, bytearray))
                and len(raw_value) == 1
                and isinstance(raw_value[0], str)
            ):
                value = raw_value[0]
            else:
                raise _InvalidQuery
        if len(value) > 300:
            raise _InvalidQuery
        repository_snapshot = store.read_snapshot()
        suggestions = build_quick_start_suggestions(
            repository_snapshot.entities, _mokvia_now(), value
        )
    except _InvalidQuery:
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry suggestions")
    except (InputError, OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")
    return 200, {"suggestions": suggestions}


def _task_search_terms(value: str) -> tuple[str, ...]:
    return tuple(term for term in unicodedata.normalize("NFKC", value).casefold().split() if term)


def _handle_task_search(
    store: Store, query: Mapping[str, str | Sequence[str]] | None,
) -> tuple[int, dict[str, object]]:
    try:
        if query is None:
            values: dict[str, str] = {}
        elif not isinstance(query, Mapping) or set(query) - {"q", "include_archived", "offset"}:
            raise _InvalidQuery
        else:
            values = {}
            for key, raw_value in query.items():
                if not isinstance(raw_value, str):
                    raise _InvalidQuery
                values[key] = raw_value
        value = values.get("q", "")
        archived = values.get("include_archived", "false")
        offset = values.get("offset", "0")
        if len(value) > 300 or archived not in {"true", "false"} or len(offset) > 7 or not re.fullmatch(r"0|[1-9][0-9]*", offset):
            raise _InvalidQuery
        terms = _task_search_terms(value)
        snapshot = store.read_snapshot()
        project_titles = {
            entity.entity_id: entity.frontmatter.get("title", entity.entity_id)
            for entity in snapshot.entities if entity.entity_type == "project"
        }
        selected = []
        for entity in snapshot.entities:
            if entity.entity_type != "task" or (_is_archived(entity) and archived != "true"):
                continue
            haystack = _task_search_terms(" ".join((entity.entity_id, entity.frontmatter.get("title", ""), entity.body)))
            text = " ".join(haystack)
            if all(term in text for term in terms):
                selected.append(entity)
        selected.sort(key=lambda entity: (entity.frontmatter.get("title", "").casefold(), entity.entity_id))
        start = int(offset)
        items = []
        for entity in selected[start:start + 50]:
            item = _summary(entity)
            item["archived"] = _is_archived(entity)
            project_id = entity.frontmatter.get("project_id", "")
            if project_id in project_titles:
                item["project_title"] = project_titles[project_id]
            elif project_id:
                item["project_title"] = project_id
            items.append(item)
        return 200, {"items": items, "total": len(selected), "offset": start, "limit": 50}
    except _InvalidQuery:
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry search")
    except (InputError, OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def _handle_detail(store: Store, kind: str, entity_id: str) -> tuple[int, dict[str, object]]:
    if kind not in _KINDS_BY_TYPE.values():
        return _error(400, "invalid_request", "entity request is invalid")
    try:
        validate_entity_id(entity_id)
    except SecurityInputError:
        return _error(400, "invalid_request", "entity request is invalid")

    try:
        entity = store.get_entity(entity_id)
        if _kind(entity) != kind:
            raise NotFoundError("kind mismatch")
        return 200, _detail(entity)
    except NotFoundError:
        return _error(404, "not_found", "entity was not found")
    except (InputError, OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def _handle_progress(
    store: Store, query: Mapping[str, str | Sequence[str]] | None
) -> tuple[int, dict[str, object]]:
    try:
        values = _single_query_values(query, {"from", "to"})
        if set(values) != {"from", "to"}:
            raise _InvalidQuery
        start = _parse_date(values["from"])
        end = _parse_date(values["to"])
        if start > end or (end - start).days > 30:
            raise _InvalidQuery
        snapshot = store.read_snapshot()
        return 200, {"items": list(progress_period(snapshot.entities, start, end))}
    except _InvalidQuery:
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry snapshot")
    except (InputError, OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def _handle_progress_report(
    store: Store, query: Mapping[str, str | Sequence[str]] | None
) -> tuple[int, dict[str, object]]:
    try:
        values = _single_query_values(query, {"month"})
        if set(values) != {"month"} or _ISO_MONTH.fullmatch(values["month"]) is None:
            raise _InvalidQuery
        try:
            datetime.date.fromisoformat(f"{values['month']}-01")
        except ValueError as error:
            raise _InvalidQuery from error
        snapshot = store.read_snapshot()
        return 200, monthly_progress_report(snapshot.entities, values["month"])
    except _InvalidQuery:
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry snapshot")
    except (InputError, OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def _resource_allocation_query(
    query: Mapping[str, str | Sequence[str]] | None,
    *,
    extra: set[str] = set(),
) -> dict[str, str]:
    values = _single_query_values(
        query, {"axis", "period_kind", "period_start", *extra}
    )
    required = {"axis", "period_kind", "period_start", *extra}
    if set(values) != required:
        raise _InvalidQuery
    return values


def _handle_resource_allocation_report(
    store: Store, query: Mapping[str, str | Sequence[str]] | None
) -> tuple[int, dict[str, object]]:
    try:
        values = _resource_allocation_query(query)
        with store.mutation_lock():
            report = store.resource_allocation_report(
                values["axis"], values["period_kind"], values["period_start"],
                generated_at=_mokvia_now(),
            )
        return 200, report
    except (_InvalidQuery, InputError, ValueError):
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry report")
    except (OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def _handle_resource_allocation_tasks(
    store: Store, query: Mapping[str, str | Sequence[str]] | None
) -> tuple[int, dict[str, object]]:
    try:
        values = _resource_allocation_query(
            query, extra={"target", "limit", "cursor"}
            if query is not None and "cursor" in query
            else {"target", "limit"},
        )
        if (
            re.fullmatch(r"[A-Za-z0-9_-]+", values["target"]) is None
            or _POSITIVE_ASCII_INTEGER.fullmatch(values["limit"]) is None
        ):
            raise _InvalidQuery
        limit = int(values["limit"])
        if limit > 100:
            raise _InvalidQuery
        with store.mutation_lock():
            report = store.resource_allocation_tasks(
                values["axis"], values["period_kind"], values["period_start"],
                values["target"], limit=limit, cursor=values.get("cursor"),
                generated_at=_mokvia_now(),
            )
        return 200, report
    except (_InvalidQuery, InputError, ValueError):
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry report")
    except (OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def _handle_resource_allocation_draft(
    store: Store, query: Mapping[str, str | Sequence[str]] | None
) -> tuple[int, dict[str, object]]:
    try:
        values = _single_query_values(
            query, {"plan_id", "period_kind", "period_start"}
        )
        if set(values) != {"plan_id", "period_kind", "period_start"}:
            raise _InvalidQuery
        validate_entity_id(values["plan_id"])
        with store.mutation_lock():
            draft = store.resource_allocation_plan_draft(
                values["plan_id"], values["period_kind"], values["period_start"]
            )
        return 200, draft
    except NotFoundError:
        return _error(404, "not_found", "entity was not found")
    except (_InvalidQuery, InputError, SecurityInputError, ValueError):
        return _error(400, "invalid_query", "query parameters are invalid")
    except StoreLockTimeout:
        return _error(503, "busy", "repository is busy; retry draft")
    except (OSError, UnicodeError):
        return _error(500, "repository_error", "repository data could not be read")


def handle(
    store: Store,
    method: str,
    path: str,
    query: Mapping[str, str | Sequence[str]] | None = None,
    headers: object | None = None,
    body: bytes | None = None,
    state: ApiState | None = None,
    status_reader: Callable[[], Mapping[str, object]] | None = None,
    bind_origin: str = BIND_ORIGIN,
    mutation_origins: Sequence[str] | None = None,
    health_details: Mapping[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    """Handle one API request without reading sockets."""
    normalized_method = method.upper() if isinstance(method, str) else ""

    if path in _MUTATION_ROUTES:
        if normalized_method != "POST":
            return _error(405, "method_not_allowed", "method is not allowed")
        if query:
            return _error(400, "invalid_request", "query parameters are not allowed")
        try:
            validate_mutation_headers(
                headers,
                allowed_origin=None if mutation_origins is not None else bind_origin,
                allowed_origins=mutation_origins,
            )
        except ForbiddenRequestError:
            return _error(403, "forbidden", "mutation request is forbidden")
        except SecurityInputError:
            return _error(400, "invalid_request", "mutation request is invalid")
        try:
            request = _strict_json_object(body)
        except OverflowError:
            return _error(413, "payload_too_large", "request body is too large")
        except _InvalidJson:
            return _error(400, "invalid_request", "mutation request is invalid")
        active_state = _state_for(store, state)
        if active_state is None:
            return _error(409, "stale_preview", "preview is unknown or stale")
        if path == "/api/v1/mutations/preview":
            return _handle_mutation_preview(store, request, active_state)
        return _handle_mutation_apply(store, request, active_state)

    if path == "/api/v1/calendar-sync/status":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        if query:
            return _error(400, "invalid_query", "query parameters are not allowed")
        try:
            value = validate_status_document(
                dict((status_reader or read_calendar_sync_status)())
            )
        except Exception:
            value = unavailable_status()
        return 200, value

    if path == "/api/v1/health":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        if query:
            return _error(400, "invalid_query", "query parameters are not allowed")
        health: dict[str, object] = {
            "status": "ok",
            "bind_origin": bind_origin,
            "validator_available": True,
        }
        if health_details is not None:
            health.update(health_details)
        if mutation_origins is not None:
            health["mutation_origins"] = list(mutation_origins)
        return 200, health

    if path == "/api/v1/snapshot":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_snapshot(store, query)

    if path == "/api/v1/progress":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_progress(store, query)

    if path == "/api/v1/reports/progress":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_progress_report(store, query)

    if path == "/api/v1/quick-start-suggestions":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_quick_start_suggestions(store, query)

    if path == "/api/v1/tasks/search":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_task_search(store, query)

    if path == "/api/v1/reports/resource-allocation":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_resource_allocation_report(store, query)

    if path == "/api/v1/reports/resource-allocation/tasks":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_resource_allocation_tasks(store, query)

    if path == "/api/v1/time-allocation-plan-draft":
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        return _handle_resource_allocation_draft(store, query)

    entity_prefix = "/api/v1/entities/"
    if isinstance(path, str) and path.startswith(entity_prefix):
        components = path[len(entity_prefix) :].split("/")
        if len(components) != 2 or not all(components):
            return _error(400, "invalid_request", "entity request is invalid")
        kind, entity_id = components
        if kind not in _KINDS_BY_TYPE.values():
            return _error(400, "invalid_request", "entity request is invalid")
        try:
            validate_entity_id(entity_id)
        except SecurityInputError:
            return _error(400, "invalid_request", "entity request is invalid")
        if normalized_method != "GET":
            return _error(405, "method_not_allowed", "method is not allowed")
        if query:
            return _error(400, "invalid_query", "query parameters are not allowed")
        return _handle_detail(store, kind, entity_id)

    return _error(404, "not_found", "route was not found")
