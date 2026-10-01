"""Storage primitives for the repository's constrained Markdown format."""

import contextlib
import ctypes
import dataclasses
import datetime
import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import stat
import tempfile
import threading
import time
import types
from collections.abc import Callable, Iterator, Mapping

from scripts.validate_frontmatter import (
    CALENDAR_IDENTITY_KEYS,
    DATE_PATTERN,
    DONE_CALENDAR_ID,
    EntityContract,
    FORBIDDEN_KEYS,
    OPTIONAL_KEY_ORDER,
    POSITIVE_INTEGER_PATTERN,
    PROJECT_KANBAN_POSITION_PATTERN,
    PROJECT_TASK_POSITION_PATTERN,
    OPTIONAL_KEYS,
    parse_inline_list,
    REFERENCE_CONTRACTS,
    REQUIRED_KEYS,
    TIMESTAMP_PATTERN,
    TIME_ALLOCATION_AXES,
    TIME_ALLOCATION_INPUT_MODES,
    TIME_ALLOCATION_PERIOD_KINDS,
    WORK_TIMESTAMP_PATTERN,
    _validate_values,
    parse_time_allocation_allocations,
    time_allocation_period_end,
    validate_document_map,
    validate_repository,
)


_FOCUS_MONITOR_PAUSE_MARKER_LINE = re.compile(
    r"(?m)^\[mokvia-focus-monitor\] 通知停止期限: "
    r"(?P<deadline>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+09:00)(?:\n|\Z)"
)
_BREAK_TITLE = "5分休憩"
_BREAK_TIMER_KIND = "break"
_BREAK_DURATION = datetime.timedelta(seconds=300)
_SERVER_MANAGED_TASK_FIELDS = {"timer_kind", "timer_ends_at"}
_PROJECT_KANBAN_LANES = (
    "not_started", "doing", "on_hold", "completed", "dropped"
)
_MOKVIA_LOCAL_TIMEZONE = datetime.timezone(datetime.timedelta(hours=9))
_RESOURCE_ALLOCATION_PERIOD_ORDER = ("day", "week", "month", "year")
_RESOURCE_ALLOCATION_EXPAND_MAX_TARGETS = 431
_RESOURCE_ALLOCATION_EXPAND_MAX_EFFECTS = 862


def _remove_valid_focus_monitor_pause_markers(body: str) -> str:
    def remove_if_valid(match: re.Match[str]) -> str:
        try:
            deadline = datetime.datetime.fromisoformat(match.group("deadline"))
        except ValueError:
            return match.group(0)
        if deadline.utcoffset() != datetime.timedelta(hours=9):
            return match.group(0)
        return ""

    return _FOCUS_MONITOR_PAUSE_MARKER_LINE.sub(remove_if_valid, body)


def focus_monitor_pause_deadline(
    body: str, now: datetime.datetime
) -> str | None:
    """Return the latest unexpired exact JST pause marker, if any."""
    latest: datetime.datetime | None = None
    for match in _FOCUS_MONITOR_PAUSE_MARKER_LINE.finditer(body):
        try:
            deadline = datetime.datetime.fromisoformat(match.group("deadline"))
        except ValueError:
            continue
        if deadline.utcoffset() != datetime.timedelta(hours=9) or deadline <= now:
            continue
        if latest is None or deadline > latest:
            latest = deadline
    return None if latest is None else latest.isoformat(timespec="seconds")


def _replace_valid_focus_monitor_pause_markers(body: str, deadline: str) -> str:
    retained = _remove_valid_focus_monitor_pause_markers(body).rstrip("\n")
    marker = f"[mokvia-focus-monitor] 通知停止期限: {deadline}"
    return f"{retained}\n\n{marker}" if retained else marker


class InputError(Exception):
    """Raised when repository document input is outside the supported format."""


class RoadmapOperationError(InputError):
    """Raised with the stable public code for a Roadmap workflow rejection."""

    def __init__(self, code: str, http_status: int) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class ProjectOperationError(InputError):
    """Raised with the stable public code for a Project workflow rejection."""

    def __init__(self, code: str, http_status: int = 409) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _validate_purpose_body(body: str) -> None:
    lines = body.splitlines()
    if "## Purpose" not in lines or "## Principles" not in lines:
        raise InputError("Purpose body headings are invalid")


class MutationPlanConflict(InputError):
    """Raised when a preview can no longer be applied to current state."""


class DestinationConflict(MutationPlanConflict):
    """Raised when a planned ID or destination is no longer available."""


class DestinationCollision(InputError):
    """Raised when an atomic no-replace destination is already occupied."""


class SourceOwnershipConflict(InputError):
    """Raised when a move source no longer has the inode we read."""

    def __init__(self, path: pathlib.Path, message: str) -> None:
        self.path = path
        super().__init__(message)


class RenamePostEffectError(RuntimeError):
    """Raised when rename succeeded but its durability cleanup failed."""

    def __init__(
        self,
        source: pathlib.Path,
        destination: pathlib.Path,
        errors: list[BaseException],
    ) -> None:
        self.source = source
        self.destination = destination
        details = "; ".join(str(error) for error in errors)
        super().__init__(
            f"rename post-effect failure; entry moved to {destination}: {details}"
        )


class NotFoundError(Exception):
    """Raised when an entity ID does not exist in the repository."""


class StoreLockTimeout(Exception):
    """Raised when the repository mutation lock cannot be acquired in time."""


class SchemaError(Exception):
    """Raised when a mutation introduces repository schema errors."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = sorted(errors)
        super().__init__("; ".join(self.errors))


@dataclasses.dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_type: str
    relative_path: str
    frontmatter: dict[str, str]
    body: str
    content_hash: str


@dataclasses.dataclass(frozen=True)
class RepositorySnapshot:
    """One mutation-lock-consistent repository read boundary."""

    entities: tuple[Entity, ...]
    recovery_required: bool


@dataclasses.dataclass(frozen=True)
class _AllocationSession:
    task: Entity
    start: datetime.datetime
    end: datetime.datetime
    provisional: bool
    target: str


@dataclasses.dataclass(frozen=True)
class WorkflowPlanningSnapshot:
    """One ephemeral, lock-consistent entity set for workflow planning."""

    entities: tuple[Entity, ...]
    entities_by_id: Mapping[str, tuple[Entity, ...]]

    def find(self, entity_id: str) -> Entity:
        matches = self.entities_by_id.get(entity_id, ())
        if not matches:
            raise NotFoundError(f"unknown entity id: {entity_id}")
        if len(matches) > 1:
            raise InputError(f"duplicate entity id: {entity_id}")
        return matches[0]


@dataclasses.dataclass(frozen=True)
class MutationPlan:
    """An exact, previewable repository mutation with no dynamic apply values."""

    action: str
    entity_id: str
    entity_type: str
    source_relative_path: str | None
    destination_relative_path: str
    base_hash: str | None
    before_bytes: bytes | None
    after_bytes: bytes
    before_entity: Entity | None
    planned_entity: Entity
    operation_inputs: tuple[tuple[str, str], ...]
    validation_errors: tuple[str, ...]
    integrity_tag: str


@dataclasses.dataclass(frozen=True)
class PublicationToken:
    """Identity of the exact inode published or read by one mutation."""

    st_dev: int
    st_ino: int

    @classmethod
    def from_stat(cls, result: os.stat_result) -> "PublicationToken":
        return cls(st_dev=result.st_dev, st_ino=result.st_ino)


@dataclasses.dataclass(frozen=True)
class _FileSignature:
    """Metadata that invalidates one cached canonical document."""

    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int

    @classmethod
    def from_stat(cls, result: os.stat_result) -> "_FileSignature":
        return cls(
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns,
            st_ctime_ns=result.st_ctime_ns,
        )


@dataclasses.dataclass(frozen=True)
class _CachedDocument:
    """One path-owned parsed entity and its authoritative source bytes."""

    entity: Entity | None
    raw_bytes: bytes
    signature: _FileSignature


@dataclasses.dataclass(frozen=True)
class WorkflowEffect:
    """One exact ordered entity effect inside a Task workflow plan."""

    role: str
    entity_id: str
    source_relative_path: str | None
    destination_relative_path: str
    base_hash: str | None
    before_bytes: bytes | None
    after_bytes: bytes
    before_entity: Entity | None
    planned_entity: Entity
    source_token: PublicationToken | None
    destination_token: PublicationToken | None


@dataclasses.dataclass(frozen=True)
class WorkflowPlan:
    """An immutable, store-bound, multi-entity Task workflow preview."""

    action: str
    target_entity_id: str
    target_base_hash: str
    active_entity_id: str | None
    active_base_hash: str | None
    resolution: str | None
    operation_inputs: tuple[tuple[str, str], ...]
    effects: tuple[WorkflowEffect, ...]
    validation_errors: tuple[str, ...]
    integrity_tag: str


class WorkflowPostCommitCleanupError(RuntimeError):
    """Canonical workflow effects committed, but old-source cleanup failed."""

    def __init__(
        self,
        committed_entities: tuple[Entity, ...],
        errors: tuple[BaseException, ...],
        remaining_quarantines: tuple[pathlib.Path, ...],
        protected_paths: tuple[pathlib.Path, ...] = (),
        uncertain_paths: tuple[pathlib.Path, ...] = (),
    ) -> None:
        self.committed_entities = committed_entities
        self.errors = errors
        self.remaining_quarantines = remaining_quarantines
        self.protected_paths = protected_paths
        self.uncertain_paths = uncertain_paths
        self.recovery_paths = tuple(
            dict.fromkeys(
                (*remaining_quarantines, *protected_paths, *uncertain_paths)
            )
        )
        super().__init__(
            "workflow committed but post-commit quarantine cleanup failed: "
            + "; ".join(str(error) for error in errors)
        )


class WorkflowRollbackError(RuntimeError):
    """Best-effort rollback retained winners or recovery artifacts."""

    def __init__(
        self,
        errors: tuple[BaseException, ...],
        protected_paths: tuple[pathlib.Path, ...],
    ) -> None:
        self.errors = errors
        self.protected_paths = protected_paths
        super().__init__(
            "workflow rollback incomplete: "
            + "; ".join(str(error) for error in errors)
        )


class MutationRecoveryRequired(RuntimeError):
    """Raised while a persistent operator-recovery gate blocks writes."""


class QuarantineCleanupError(RuntimeError):
    """Owned cleanup failed and may have left named recovery artifacts."""

    def __init__(
        self,
        message: str,
        errors: tuple[BaseException, ...],
        residual_paths: tuple[pathlib.Path, ...],
        protected_paths: tuple[pathlib.Path, ...] = (),
        uncertain_paths: tuple[pathlib.Path, ...] = (),
    ) -> None:
        self.errors = errors
        self.residual_paths = residual_paths
        self.protected_paths = protected_paths
        self.uncertain_paths = uncertain_paths
        self.recovery_paths = tuple(
            dict.fromkeys((*residual_paths, *protected_paths, *uncertain_paths))
        )
        super().__init__(message)


class QuarantinePreservationError(RuntimeError):
    """Preservation publication failed with a classified candidate name."""

    def __init__(
        self,
        message: str,
        cause: BaseException,
        *,
        protected_paths: tuple[pathlib.Path, ...] = (),
        uncertain_paths: tuple[pathlib.Path, ...] = (),
    ) -> None:
        self.cause = cause
        self.protected_paths = protected_paths
        self.uncertain_paths = uncertain_paths
        super().__init__(message)


class ConflictError(Exception):
    """Raised when an entity changed since the caller last read it."""

    def __init__(self, current: Entity) -> None:
        super().__init__(f"entity changed: {current.entity_id}")
        self.current = current


LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_INTERVAL_SECONDS = 0.05
_RECOVERY_STATE_NAME = ".webapp-mutation-state"
_RECOVERY_STATE_IDLE = b"idle\n"
_RECOVERY_STATE_ARMED = b"armed\n"

_SCAN_DIRECTORIES = (
    "inbox",
    "tasks",
    "purposes",
    "visions",
    "areas",
    "projects",
    "goals",
    "roadmap-outcomes",
    "cycles",
    "progress",
    "time-allocation-plans",
    "reviews/daily",
    "reviews/weekly",
    "archive",
)
_FAST_TASK_WORKFLOW_ACTIONS = frozenset(
    {"start", "create_and_start", "complete", "interrupt"}
)
_BARE_VALUE_PATTERN = re.compile(r"[A-Za-z0-9_./+:-]+")
_INLINE_LIST_PATTERN = re.compile(r"\[.*\]")
_CONTENT_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_CALENDAR_IMPORT_TASK_ID_PATTERN = re.compile(
    r"task-calendar-[0-9a-f]{24}\Z", re.ASCII
)
_GENERATED_KEYS = {"id", "type", "created_at", "updated_at"}
_CREATE_FIELDS = {
    "task": (
        {"title", "status", *OPTIONAL_KEYS["task"]}
        - _SERVER_MANAGED_TASK_FIELDS
    ),
    "purpose": {"title"},
    "vision": {"title", "status"},
    "area": {"title", *OPTIONAL_KEYS["area"]},
    "project": {"title", "status", *OPTIONAL_KEYS["project"]},
    "goal": {"title", "status", *OPTIONAL_KEYS["goal"]},
    "roadmap_outcome": {
        "title", "goal_id", "target_date", "roadmap_lane", "roadmap_position"
    },
    "cycle": {"title", "start_date", "end_date", "outcome_ids"},
    "progress": {"title", "occurred_on", "project_id", "goal_id", "area_id"},
    "review": {"title", "period_start"},
}
_UPDATE_FIELDS = {
    entity_type: (set(REQUIRED_KEYS[entity_type]) | OPTIONAL_KEYS[entity_type])
    - _GENERATED_KEYS
    - ({"review_kind"} if entity_type == "review" else set())
    for entity_type in REQUIRED_KEYS
}
_UPDATE_FIELDS["task"] -= _SERVER_MANAGED_TASK_FIELDS
_UPDATE_FIELDS["roadmap_outcome"] = {"title", "goal_id", "target_date"}
_CREATE_FIELDS["project"].discard("kanban_position")
_UPDATE_FIELDS["project"].discard("kanban_position")
_UPDATE_FIELDS["cycle"] = {"title", "start_date", "end_date", "outcome_ids"}
_UPDATE_FIELDS["progress"].discard("visibility")


def current_time() -> datetime.datetime:
    """Return the server-local aware time used by generated timestamps."""
    return datetime.datetime.now().astimezone()


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    """Atomically replace one regular path with fully synced bytes."""
    destination = pathlib.Path(path)
    parent = destination.parent
    proc_fd_root = pathlib.Path("/proc/self/fd")
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or nofollow is None or not proc_fd_root.is_dir():
        raise InputError(
            "atomic writes require Linux /proc, O_DIRECTORY, and O_NOFOLLOW"
        )
    if parent.is_symlink():
        raise InputError(f"atomic write parent must not be a symlink: {parent}")
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InputError(f"atomic write parent does not exist: {parent}") from error
    if not resolved_parent.is_dir():
        raise InputError(f"atomic write parent is not a directory: {parent}")
    if destination.is_symlink():
        raise InputError(f"atomic write destination must not be a symlink: {path}")
    try:
        resolved_destination = destination.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise InputError(f"cannot resolve atomic write destination: {path}") from error
    expected_destination = resolved_parent / destination.name
    if not destination.name or resolved_destination != expected_destination:
        raise InputError(f"atomic write destination escaped its parent: {path}")
    if destination.exists() and not destination.is_file():
        raise InputError(f"atomic write destination is not a file: {path}")

    parent_flags = os.O_RDONLY | directory_flag | nofollow
    parent_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        parent_descriptor = os.open(parent, parent_flags)
    except OSError as error:
        raise InputError(
            f"cannot securely open atomic write parent: {parent}"
        ) from error

    temporary_descriptor = -1
    temporary_name: str | None = None
    replaced = False
    parent_synced = False
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        if not stat.S_ISDIR(os.fstat(parent_descriptor).st_mode):
            raise InputError(f"atomic write parent is not a directory: {parent}")
        try:
            opened_parent = (proc_fd_root / str(parent_descriptor)).resolve(
                strict=True
            )
        except (OSError, RuntimeError) as error:
            raise InputError(
                f"cannot verify atomic write parent: {parent}"
            ) from error
        if opened_parent != resolved_parent:
            raise InputError(f"atomic write parent changed path: {parent}")

        temporary_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=proc_fd_root / str(parent_descriptor),
        )
        temporary_name = pathlib.Path(temporary_path).name
        try:
            opened_temporary = (
                proc_fd_root / str(temporary_descriptor)
            ).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise InputError(
                f"cannot verify atomic write temporary file: {path}"
            ) from error
        if (
            opened_temporary.parent != resolved_parent
            or opened_temporary.name != temporary_name
            or not stat.S_ISREG(os.fstat(temporary_descriptor).st_mode)
        ):
            raise InputError(f"atomic write temporary file changed path: {path}")

        with os.fdopen(temporary_descriptor, "wb") as temporary_file:
            temporary_descriptor = -1
            written = temporary_file.write(data)
            if written != len(data):
                raise OSError("incomplete atomic write")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        replaced = True
        try:
            os.fsync(parent_descriptor)
        except BaseException as first_sync_error:
            try:
                os.fsync(parent_descriptor)
            except BaseException as retry_sync_error:
                raise retry_sync_error from first_sync_error
        parent_synced = True
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            if temporary_descriptor >= 0:
                try:
                    os.close(temporary_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
        finally:
            try:
                if temporary_name is not None and not replaced:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                    except BaseException as error:
                        cleanup_errors.append(error)
            finally:
                try:
                    os.close(parent_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
        if primary_error is None and cleanup_errors and not parent_synced:
            raise cleanup_errors[0]


def _resolve_atomic_create_parent(
    destination: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve and constrain the parent for a no-replace create."""
    parent = destination.parent
    proc_fd_root = pathlib.Path("/proc/self/fd")
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or nofollow is None or not proc_fd_root.is_dir():
        raise InputError(
            "atomic creates require Linux /proc, O_DIRECTORY, and O_NOFOLLOW"
        )
    if parent.is_symlink():
        raise InputError(f"atomic create parent must not be a symlink: {parent}")
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InputError(f"atomic create parent does not exist: {parent}") from error
    if not resolved_parent.is_dir():
        raise InputError(f"atomic create parent is not a directory: {parent}")
    if destination.is_symlink():
        raise InputError(
            f"atomic create destination must not be a symlink: {destination}"
        )
    try:
        resolved_destination = destination.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise InputError(
            f"cannot resolve atomic create destination: {destination}"
        ) from error
    if (
        not destination.name
        or resolved_destination != resolved_parent / destination.name
    ):
        raise InputError(
            f"atomic create destination escaped its parent: {destination}"
        )
    return resolved_parent, proc_fd_root


def _open_verified_atomic_create_parent(
    destination: pathlib.Path,
    resolved_parent: pathlib.Path,
    proc_fd_root: pathlib.Path,
) -> int:
    """Open the stable parent inode used by every later create stage."""
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or nofollow is None:
        raise InputError("atomic create platform support changed")
    flags = os.O_RDONLY | directory_flag | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(destination.parent, flags)
    except OSError as error:
        raise InputError(
            f"cannot securely open atomic create parent: {destination.parent}"
        ) from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise InputError(
                f"atomic create parent is not a directory: {destination.parent}"
            )
        opened_parent = (proc_fd_root / str(descriptor)).resolve(strict=True)
        if opened_parent != resolved_parent:
            raise InputError(
                f"atomic create parent changed path: {destination.parent}"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_synced_atomic_create_temporary(
    destination: pathlib.Path,
    resolved_parent: pathlib.Path,
    proc_fd_root: pathlib.Path,
    parent_descriptor: int,
    data: bytes,
) -> tuple[str, PublicationToken]:
    """Write and fsync the same-directory inode that will be hard-linked."""
    descriptor = -1
    temporary_name: str | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=proc_fd_root / str(parent_descriptor),
        )
        temporary_name = pathlib.Path(temporary_path).name
        opened_temporary = (proc_fd_root / str(descriptor)).resolve(strict=True)
        if (
            opened_temporary.parent != resolved_parent
            or opened_temporary.name != temporary_name
            or not stat.S_ISREG(os.fstat(descriptor).st_mode)
        ):
            raise InputError(
                f"atomic create temporary file changed path: {destination}"
            )
        with os.fdopen(descriptor, "wb") as temporary_file:
            descriptor = -1
            written = temporary_file.write(data)
            if written != len(data):
                raise OSError("incomplete atomic create write")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            publication_token = PublicationToken.from_stat(
                os.fstat(temporary_file.fileno())
            )
        return temporary_name, publication_token
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if primary_error is not None:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
                except BaseException as error:
                    cleanup_errors.append(error)
        if cleanup_errors:
            if temporary_name is not None:
                residual = resolved_parent / temporary_name
                message = (
                    "atomic create cleanup failed; residual temporary path: "
                    f"{residual}: {cleanup_errors[0]}"
                )
                if primary_error is not None:
                    raise RuntimeError(message) from primary_error
                raise RuntimeError(message) from cleanup_errors[0]
            if primary_error is None:
                raise cleanup_errors[0]


def _link_atomic_create_no_replace(
    temporary_name: str,
    destination: pathlib.Path,
    parent_descriptor: int,
) -> None:
    """Publish the temporary inode atomically, rejecting any existing name."""
    try:
        os.link(
            temporary_name,
            destination.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as error:
        raise DestinationCollision(
            f"destination already exists: {destination}"
        ) from error


def atomic_create_no_replace(
    path: pathlib.Path, data: bytes
) -> PublicationToken:
    """Atomically create one synced regular file without replacing any entry."""
    destination = pathlib.Path(path)
    resolved_parent, proc_fd_root = _resolve_atomic_create_parent(destination)
    parent_descriptor = _open_verified_atomic_create_parent(
        destination, resolved_parent, proc_fd_root
    )
    temporary_name: str | None = None
    publication_token: PublicationToken | None = None
    published = False
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        temporary_name, publication_token = _write_synced_atomic_create_temporary(
            destination,
            resolved_parent,
            proc_fd_root,
            parent_descriptor,
            data,
        )
        _link_atomic_create_no_replace(
            temporary_name, destination, parent_descriptor
        )
        published = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_name = None
        os.fsync(parent_descriptor)
        return publication_token
    except BaseException as error:
        primary_error = error
        if published and publication_token is not None:
            setattr(error, "publication_token", publication_token)
        raise
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            os.close(parent_descriptor)
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if temporary_name is not None:
                residual = resolved_parent / temporary_name
                message = (
                    "atomic create cleanup failed; residual temporary path: "
                    f"{residual}: {cleanup_errors[0]}"
                )
                if primary_error is not None:
                    cleanup_error = RuntimeError(message)
                    if published and publication_token is not None:
                        setattr(
                            cleanup_error,
                            "publication_token",
                            publication_token,
                        )
                    raise cleanup_error from primary_error
                raise RuntimeError(message) from cleanup_errors[0]
            if primary_error is None:
                cleanup_error = cleanup_errors[0]
                if published and publication_token is not None:
                    setattr(
                        cleanup_error,
                        "publication_token",
                        publication_token,
                    )
                raise cleanup_error


def _rename_no_replace(
    source: pathlib.Path,
    destination: pathlib.Path,
    root: pathlib.Path,
) -> None:
    """Atomically rename one in-root entry without replacing a winner."""
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    resolved_root = pathlib.Path(root).resolve(strict=True)
    if (
        source.parent != destination.parent
        or not source.name
        or not destination.name
    ):
        raise InputError("no-replace rename requires same-parent named paths")
    parent = source.parent
    if parent.is_symlink():
        raise InputError(f"no-replace rename parent must not be a symlink: {parent}")
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError(
            f"no-replace rename escaped repository root: {source}"
        ) from error

    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    proc_fd_root = pathlib.Path("/proc/self/fd")
    if directory_flag is None or nofollow is None or not proc_fd_root.is_dir():
        raise InputError(
            "no-replace rename requires Linux /proc, O_DIRECTORY, and O_NOFOLLOW"
        )
    flags = os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(parent, flags)
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    renamed = False
    try:
        opened_stat = os.fstat(descriptor)
        candidate_stat = os.stat(resolved_parent, follow_symlinks=False)
        opened_parent = (proc_fd_root / str(descriptor)).resolve(strict=True)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or opened_parent != resolved_parent
            or not stat.S_ISDIR(candidate_stat.st_mode)
            or opened_stat.st_dev != candidate_stat.st_dev
            or opened_stat.st_ino != candidate_stat.st_ino
        ):
            raise InputError(f"no-replace rename parent changed path: {parent}")

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise InputError("no-replace rename requires Linux renameat2")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            descriptor,
            os.fsencode(source.name),
            descriptor,
            os.fsencode(destination.name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise DestinationCollision(
                    f"destination already exists: {destination}"
                )
            raise OSError(error_number, os.strerror(error_number), str(source))
        renamed = True
        os.fsync(descriptor)
    except BaseException as error:
        primary_error = error
    finally:
        try:
            os.close(descriptor)
        except BaseException as error:
            if primary_error is None:
                primary_error = error
            else:
                cleanup_errors.append(error)
    if primary_error is not None:
        if renamed:
            post_effect_error = RenamePostEffectError(
                source,
                destination,
                [primary_error, *cleanup_errors],
            )
            raise post_effect_error from primary_error
        raise primary_error


def safe_unlink(path: pathlib.Path, root: pathlib.Path) -> None:
    """Remove one verified in-root regular file and sync its parent directory."""
    target = pathlib.Path(path)
    resolved_root = pathlib.Path(root).resolve(strict=True)
    parent = target.parent
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    proc_fd_root = pathlib.Path("/proc/self/fd")
    if directory_flag is None or nofollow is None or not proc_fd_root.is_dir():
        raise InputError("safe deletes require Linux /proc, O_DIRECTORY, and O_NOFOLLOW")
    if target.is_symlink() or parent.is_symlink():
        raise InputError(f"safe delete path must not be a symlink: {target}")
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError(f"safe delete path escaped repository root: {target}") from error
    if target.name == "" or target.resolve(strict=False) != resolved_parent / target.name:
        raise InputError(f"safe delete path escaped its parent: {target}")

    flags = os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(parent, flags)
    primary_error: BaseException | None = None
    try:
        opened_stat = os.fstat(descriptor)
        opened_parent = (proc_fd_root / str(descriptor)).resolve(strict=True)
        candidate_stat = os.stat(resolved_parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or opened_parent != resolved_parent
            or not stat.S_ISDIR(candidate_stat.st_mode)
            or opened_stat.st_dev != candidate_stat.st_dev
            or opened_stat.st_ino != candidate_stat.st_ino
        ):
            raise InputError(f"safe delete parent changed path: {parent}")
        target_stat = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(target_stat.st_mode):
            raise InputError(f"safe delete target is not a regular file: {target}")
        os.unlink(target.name, dir_fd=descriptor)
        os.fsync(descriptor)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException:
            if primary_error is None:
                raise


class Store:
    """Access entities through one resolved or descriptor-bound repository root."""

    def __init__(
        self,
        root: pathlib.Path,
        *,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> None:
        candidate_root = pathlib.Path(root)
        root_descriptor = -1
        try:
            resolved_root = candidate_root.resolve(strict=True)
            root_status = resolved_root.stat(follow_symlinks=False)
        except (OSError, RuntimeError) as error:
            raise InputError(f"repository root does not exist: {root}") from error
        if not stat.S_ISDIR(root_status.st_mode):
            raise InputError(f"repository root is not a directory: {root}")
        root_identity = (root_status.st_dev, root_status.st_ino)
        if expected_root_identity is not None:
            directory_flag = getattr(os, "O_DIRECTORY", None)
            nofollow = getattr(os, "O_NOFOLLOW", None)
            close_on_exec = getattr(os, "O_CLOEXEC", None)
            proc_fd_root = pathlib.Path("/proc/self/fd")
            if (
                directory_flag is None
                or nofollow is None
                or close_on_exec is None
                or not proc_fd_root.is_dir()
            ):
                raise InputError(
                    "bound repository roots require Linux /proc, O_DIRECTORY, "
                    "O_NOFOLLOW, and O_CLOEXEC"
                )
            try:
                root_descriptor = os.open(
                    candidate_root,
                    os.O_RDONLY | directory_flag | nofollow | close_on_exec,
                )
                opened_status = os.fstat(root_descriptor)
                candidate_status = os.stat(
                    candidate_root, follow_symlinks=False
                )
                opened_root = (proc_fd_root / str(root_descriptor)).resolve(
                    strict=True
                )
                if (
                    not stat.S_ISDIR(opened_status.st_mode)
                    or not stat.S_ISDIR(candidate_status.st_mode)
                    or (opened_status.st_dev, opened_status.st_ino)
                    != expected_root_identity
                    or (candidate_status.st_dev, candidate_status.st_ino)
                    != expected_root_identity
                    or opened_root != resolved_root
                ):
                    raise InputError(f"repository root identity changed: {root}")
            except BaseException:
                if root_descriptor >= 0:
                    os.close(root_descriptor)
                raise
            root_identity = expected_root_identity
            self._root = proc_fd_root / str(root_descriptor)
        else:
            self._root = resolved_root
        self._root_descriptor = root_descriptor
        self._root_identity = root_identity
        self._mutation_thread_lock = threading.Lock()
        self._repository_cache_lock = threading.RLock()
        self._repository_cache: dict[str, _CachedDocument] = {}
        self._repository_cache_generation = 0
        self._repository_validation_generation = -1
        self._repository_validation_errors: tuple[str, ...] = ()
        self._fast_workflow_validations: dict[
            str, tuple[int, frozenset[str]]
        ] = {}
        self._fast_workflow_source_generations: dict[str, int] = {}
        self._plan_integrity_key = secrets.token_bytes(32)
        self._mutation_recovery_latched = False

    def close(self) -> None:
        """Release the optional lifetime repository directory descriptor."""
        with self._mutation_thread_lock:
            descriptor = self._root_descriptor
            if descriptor < 0:
                return
            os.close(descriptor)
            self._root_descriptor = -1
            self._root = pathlib.Path("/proc/self/fd/-1")

    def list_entities(self) -> list[Entity]:
        """Return every supported Markdown entity sorted by repository path."""
        with self._repository_cache_lock:
            self._refresh_repository_cache_locked()
            entities: list[Entity] = []
            for relative_path in sorted(self._repository_cache):
                entity = self._repository_cache[relative_path].entity
                if entity is None:
                    raise InputError(
                        f"cannot parse repository entity: {self._root / relative_path}"
                    )
                entities.append(entity)
            return entities

    def repository_document_map(self) -> dict[str, bytes]:
        """Return a refreshed in-memory copy of every canonical document."""
        documents, _ = self._repository_document_snapshot()
        return documents

    def _repository_document_snapshot(self) -> tuple[dict[str, bytes], int]:
        """Return cached bytes and their monotonic in-process generation."""
        with self._repository_cache_lock:
            self._refresh_repository_cache_locked()
            return (
                {
                    relative_path: self._repository_cache[relative_path].raw_bytes
                    for relative_path in sorted(self._repository_cache)
                },
                self._repository_cache_generation,
            )

    def _validation_errors_for_document_snapshot(
        self, documents: Mapping[str, bytes], generation: int
    ) -> tuple[str, ...]:
        """Reuse repository validation only for the exact cache generation."""
        with self._repository_cache_lock:
            if self._repository_validation_generation == generation:
                return self._repository_validation_errors
        errors = tuple(sorted(validate_document_map(documents)))
        self._cache_validation_errors_for_exact_snapshot(
            documents, generation, errors
        )
        return errors

    def _cache_validation_errors_for_exact_snapshot(
        self,
        documents: Mapping[str, bytes],
        generation: int,
        errors: tuple[str, ...] | frozenset[str] | set[str],
    ) -> bool:
        """Cache validation only while generation and every byte stay exact."""
        with self._repository_cache_lock:
            if (
                self._repository_cache_generation != generation
                or set(documents) != set(self._repository_cache)
                or any(
                    documents[relative_path] != cached.raw_bytes
                    for relative_path, cached in self._repository_cache.items()
                )
            ):
                return False
            self._repository_validation_generation = generation
            self._repository_validation_errors = tuple(sorted(errors))
            return True

    def get_entity(self, entity_id: str) -> Entity:
        """Return one uniquely matching entity, failing closed on duplicates."""
        matches = [
            entity
            for entity in self.list_entities()
            if entity.entity_id == entity_id
        ]
        if not matches:
            raise NotFoundError(f"unknown entity id: {entity_id}")
        if len(matches) > 1:
            raise InputError(f"duplicate entity id: {entity_id}")
        return matches[0]

    def mutation_recovery_required(self) -> bool:
        """Report persistent recovery only after any in-flight write settles."""
        with self.mutation_lock():
            return self._recovery_required_locked()

    def read_snapshot(self) -> RepositorySnapshot:
        """Read entities and recovery state inside one mutation boundary."""
        with self.mutation_lock():
            entities = tuple(self.list_entities())
            recovery_required = self._recovery_required_locked()
            return RepositorySnapshot(entities, recovery_required)

    def _workflow_planning_snapshot_locked(self) -> WorkflowPlanningSnapshot:
        return self._workflow_planning_snapshot_from_entities(
            tuple(self.list_entities())
        )

    @staticmethod
    def _workflow_planning_snapshot_from_entities(
        entities: tuple[Entity, ...],
    ) -> WorkflowPlanningSnapshot:
        grouped: dict[str, list[Entity]] = {}
        for entity in entities:
            grouped.setdefault(entity.entity_id, []).append(entity)
        return WorkflowPlanningSnapshot(
            entities,
            types.MappingProxyType(
                {
                    entity_id: tuple(matches)
                    for entity_id, matches in grouped.items()
                }
            ),
        )

    def _cached_workflow_planning_snapshot(
        self,
    ) -> WorkflowPlanningSnapshot:
        """Build a planning index from the already-refreshed path cache."""
        with self._repository_cache_lock:
            entities: list[Entity] = []
            for relative_path in sorted(self._repository_cache):
                entity = self._repository_cache[relative_path].entity
                if entity is None:
                    raise InputError(
                        f"cannot parse repository entity: {self._root / relative_path}"
                    )
                entities.append(entity)
        return self._workflow_planning_snapshot_from_entities(tuple(entities))

    def _recovery_required_locked(self) -> bool:
        return (
            self._mutation_recovery_latched
            or self._recovery_state_locked() != "idle"
        )

    def _recovery_state_locked(self) -> str:
        """Read the fixed state file securely; unknown shapes fail closed."""
        state_path = self._root / _RECOVERY_STATE_NAME
        try:
            state_stat = os.lstat(state_path)
        except FileNotFoundError:
            return "idle"
        except OSError:
            return "unknown"
        if not stat.S_ISREG(state_stat.st_mode):
            return "unknown"
        try:
            state_bytes, state_token = self._read_regular_file_with_token(
                state_path
            )
        except BaseException:
            return "unknown"
        if (
            state_token.st_dev != state_stat.st_dev
            or state_token.st_ino != state_stat.st_ino
        ):
            return "unknown"
        if state_bytes == _RECOVERY_STATE_IDLE:
            return "idle"
        if state_bytes == _RECOVERY_STATE_ARMED:
            return "armed"
        return "unknown"

    def require_mutations_available(self) -> None:
        """Fail closed when an operator must inspect repository recovery state."""
        with self.mutation_lock():
            self._require_mutations_available_locked()

    def _require_mutations_available_locked(self) -> None:
        if self._recovery_required_locked():
            raise MutationRecoveryRequired("mutation recovery is required")

    def _transition_recovery_state_locked(
        self, expected: str, target: str
    ) -> None:
        states = {
            "idle": _RECOVERY_STATE_IDLE,
            "armed": _RECOVERY_STATE_ARMED,
        }
        if expected not in states or target not in states or expected == target:
            raise RuntimeError("invalid recovery state transition")
        if self._recovery_state_locked() != expected:
            raise MutationRecoveryRequired("mutation recovery is required")
        state_path = self._root / _RECOVERY_STATE_NAME
        try:
            atomic_write(state_path, states[target])
        except BaseException as transition_error:
            observed = self._recovery_state_locked()
            if observed == expected:
                raise
            self._mutation_recovery_latched = True
            if target == "idle" and observed == "idle":
                try:
                    atomic_write(state_path, _RECOVERY_STATE_ARMED)
                except BaseException:
                    pass
            raise MutationRecoveryRequired(
                "mutation recovery is required"
            ) from transition_error
        if self._recovery_state_locked() != target:
            raise MutationRecoveryRequired("mutation recovery is required")

    def _arm_workflow_recovery_state_locked(self) -> None:
        self._require_mutations_available_locked()
        self._transition_recovery_state_locked("idle", "armed")

    @contextlib.contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Hold the process-local and repository-wide mutation locks."""
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        remaining = max(0.0, deadline - time.monotonic())
        if not self._mutation_thread_lock.acquire(timeout=remaining):
            raise StoreLockTimeout("timed out acquiring in-process store lock")

        root_descriptor = -1
        root_flock_acquired = False
        lock_descriptor = -1
        lock_flock_acquired = False
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            root_descriptor = self._open_root_lock_directory()
            self._acquire_flock(root_descriptor, deadline, "repository root")
            root_flock_acquired = True
            self._verify_root_lock_directory(root_descriptor)

            lock_descriptor = self._open_lock_file()
            self._acquire_flock(lock_descriptor, deadline, "repository lock")
            lock_flock_acquired = True
            self._verify_lock_file(
                lock_descriptor, self._root / ".webapp.lock"
            )

            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                if lock_flock_acquired:
                    try:
                        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    except BaseException as error:
                        cleanup_errors.append(error)
            finally:
                try:
                    if lock_descriptor >= 0:
                        try:
                            os.close(lock_descriptor)
                        except BaseException as error:
                            cleanup_errors.append(error)
                finally:
                    try:
                        if root_flock_acquired:
                            try:
                                fcntl.flock(root_descriptor, fcntl.LOCK_UN)
                            except BaseException as error:
                                cleanup_errors.append(error)
                    finally:
                        try:
                            if root_descriptor >= 0:
                                try:
                                    os.close(root_descriptor)
                                except BaseException as error:
                                    cleanup_errors.append(error)
                        finally:
                            try:
                                self._mutation_thread_lock.release()
                            except BaseException as error:
                                cleanup_errors.append(error)
            if primary_error is None and cleanup_errors:
                raise cleanup_errors[0]

    def require_current(self, entity_id: str, base_hash: str) -> Entity:
        """Return the current entity or raise with its conflicting version."""
        if not isinstance(base_hash, str) or _CONTENT_HASH_PATTERN.fullmatch(
            base_hash
        ) is None:
            raise InputError(
                "base_hash must be 64 lowercase hexadecimal characters"
            )
        current = self.get_entity(entity_id)
        if current.content_hash != base_hash:
            raise ConflictError(current)
        return current

    def next_entity_id(
        self,
        entity_type: str,
        *,
        review_kind: str | None = None,
        on_date: datetime.date | None = None,
    ) -> str:
        """Return the next unused date-local entity ID suffix."""
        if entity_type == "review":
            if review_kind not in {"daily", "weekly"}:
                raise InputError("review_kind must be daily or weekly for review")
            base_prefix = f"review-{review_kind}"
        elif entity_type in {
            "task", "purpose", "vision", "area", "project", "goal",
            "roadmap_outcome", "cycle", "progress", "time_allocation_plan",
        }:
            if review_kind is not None:
                raise InputError("review_kind is only supported for review")
            base_prefix = entity_type.replace("_", "-")
        else:
            raise InputError(f"unknown entity type: {entity_type}")

        local_date = datetime.date.today() if on_date is None else on_date
        if not isinstance(local_date, datetime.date):
            raise InputError("on_date must be a date")
        prefix = f"{base_prefix}-{local_date:%Y%m%d}"
        id_pattern = re.compile(rf"{re.escape(prefix)}-([0-9]{{3}})")
        suffixes: list[int] = []
        for directory in _SCAN_DIRECTORIES:
            scan_root = self._root / directory
            if scan_root.is_symlink() or not scan_root.is_dir():
                continue
            for path in self._walk_directory(scan_root):
                try:
                    entity = self._read_entity(path)
                except InputError:
                    continue
                if entity is None:
                    continue
                match = id_pattern.fullmatch(entity.entity_id)
                if match is not None:
                    suffixes.append(int(match.group(1)))
        next_suffix = max(suffixes, default=0) + 1
        if next_suffix > 999:
            raise InputError(f"entity id suffix exhausted for {prefix}")
        return f"{prefix}-{next_suffix:03d}"

    @staticmethod
    def _next_task_id_from_planning(
        planning: WorkflowPlanningSnapshot,
        on_date: datetime.date,
    ) -> str:
        """Allocate a Task ID from the already-refreshed workflow index."""
        prefix = f"task-{on_date:%Y%m%d}"
        id_pattern = re.compile(rf"{re.escape(prefix)}-([0-9]{{3}})")
        suffixes = [
            int(match.group(1))
            for entity in planning.entities
            if (match := id_pattern.fullmatch(entity.entity_id)) is not None
        ]
        next_suffix = max(suffixes, default=0) + 1
        if next_suffix > 999:
            raise InputError(f"entity id suffix exhausted for {prefix}")
        return f"{prefix}-{next_suffix:03d}"

    def create_entity(
        self,
        entity_type: str,
        fields: dict[str, str],
        body: str,
        review_kind: str | None = None,
        *,
        requested_id: str | None = None,
    ) -> Entity:
        if entity_type == "time_allocation_plan":
            raise InputError("Time Allocation Plans require dedicated workflows")
        """Create one canonical entity under the common mutation lock."""
        with self.mutation_lock():
            plan = self._plan_order_aware_create_locked(
                entity_type, fields, body, review_kind, requested_id
            )
            if isinstance(plan, WorkflowPlan):
                return next(
                    result
                    for result in self._apply_task_workflow_with_recovery_locked(plan)
                    if result.entity_id == plan.target_entity_id
                )
            return self._apply_mutation_plan_locked(plan)

    def plan_create_entity(
        self,
        entity_type: str,
        fields: dict[str, str],
        body: str,
        review_kind: str | None = None,
        *,
        requested_id: str | None = None,
    ) -> MutationPlan | WorkflowPlan:
        """Preview one exact create without changing canonical entity files."""
        if entity_type == "time_allocation_plan":
            raise InputError("Time Allocation Plans require dedicated workflows")
        with self.mutation_lock():
            self._require_mutations_available_locked()
            return self._plan_order_aware_create_locked(
                entity_type, fields, body, review_kind, requested_id
            )

    def update_entity(
        self,
        entity_id: str,
        base_hash: str,
        fields: dict[str, str],
        body: str | None,
    ) -> Entity:
        """Apply one canonical partial update with optimistic concurrency."""
        plan = self.plan_update_entity(entity_id, base_hash, fields, body)
        if isinstance(plan, WorkflowPlan):
            results = self.apply_task_workflow(plan)
            return next(
                result for result in results if result.entity_id == entity_id
            )
        return self.apply_mutation_plan(plan)

    def plan_update_entity(
        self,
        entity_id: str,
        base_hash: str,
        fields: dict[str, str],
        body: str | None,
    ) -> MutationPlan | WorkflowPlan:
        """Preview one exact update without changing canonical entity files."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            plan = self._plan_dependency_aware_update_locked(
                entity_id, base_hash, fields, body
            )
            if not isinstance(plan, WorkflowPlan):
                return plan
            preview = plan
        return self._finalize_workflow_preview(preview)

    def plan_task_pause_notifications(
        self, entity_id: str, base_hash: str, minutes: int
    ) -> MutationPlan:
        """Preview replacing valid pause markers for the sole doing Task."""
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise InputError("pause duration is invalid")
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            current = self._require_current_doing_task(entity_id, base_hash)
            now = current_time().astimezone(_MOKVIA_LOCAL_TIMEZONE)
            deadline = (now + datetime.timedelta(minutes=minutes)).isoformat(
                timespec="seconds"
            )
            return self._plan_update_entity_locked(
                current.entity_id,
                base_hash,
                {},
                _replace_valid_focus_monitor_pause_markers(current.body, deadline),
            )

    def plan_task_resume_notifications(
        self, entity_id: str, base_hash: str
    ) -> MutationPlan:
        """Preview removing only valid pause markers for the sole doing Task."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            current = self._require_current_doing_task(entity_id, base_hash)
            return self._plan_update_entity_locked(
                current.entity_id,
                base_hash,
                {},
                _remove_valid_focus_monitor_pause_markers(current.body),
            )

    def plan_task_correct_work_session(
        self,
        entity_id: str,
        base_hash: str,
        *,
        work_started_at: str,
        work_ended_at: str | None = None,
    ) -> MutationPlan:
        """Preview a correction of one already-recorded ordinary Task session."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            current = self._require_workflow_task(entity_id, base_hash)
            if current.frontmatter.get("timer_kind") == _BREAK_TIMER_KIND:
                raise InputError("break Tasks cannot have work sessions corrected")
            status = current.frontmatter.get("status")
            if status not in {"doing", "done"}:
                raise InputError("work session correction requires a recorded doing or done Task")
            existing_started_at = current.frontmatter.get("work_started_at", "")
            existing_started = self._parsed_work_session_timestamp(existing_started_at)
            if existing_started is None:
                raise InputError("existing work session record is invalid")
            if status == "doing" and "work_ended_at" in current.frontmatter:
                raise InputError("existing work session record is invalid")
            existing_ended_at = current.frontmatter.get("work_ended_at", "")
            existing_ended = (
                None
                if status == "doing"
                else self._parsed_work_session_timestamp(existing_ended_at)
            )
            if status == "done" and (
                existing_ended is None or "resume_status" in current.frontmatter
            ):
                raise InputError("existing work session record is invalid")
            if status == "done" and work_ended_at is None:
                raise InputError("done work session correction requires work_ended_at")
            if status == "doing" and work_ended_at is not None:
                raise InputError("doing work session correction does not allow work_ended_at")

            started = self._parsed_work_session_timestamp(work_started_at)
            if started is None:
                raise InputError("invalid work_started_at")
            now = current_time()
            if started > now:
                raise InputError("work_started_at is in the future")
            fields = {"work_started_at": work_started_at}
            if work_ended_at is not None:
                ended = self._parsed_work_session_timestamp(work_ended_at)
                if ended is None:
                    raise InputError("invalid work_ended_at")
                if ended > now:
                    raise InputError("work_ended_at is in the future")
                if ended < started:
                    raise InputError("work_ended_at is earlier than work_started_at")
                fields["work_ended_at"] = work_ended_at

            calendar_linked = self._require_calendar_lifecycle_state(
                dict(current.frontmatter)
            )
            if existing_ended is not None and (
                existing_ended < existing_started
                or (calendar_linked and existing_ended <= existing_started)
            ):
                raise InputError("existing work session record is invalid")
            if calendar_linked and work_ended_at is not None and ended <= started:
                raise InputError("Calendar work_ended_at must be after work_started_at")
            if calendar_linked:
                fields["started_at"] = work_started_at
                fields["calendar_event_kind"] = "timed"
                if work_ended_at is not None:
                    fields["completed_at"] = work_ended_at
            return self._plan_update_entity_locked(
                current.entity_id, base_hash, fields, None
            )

    def archive_entity(self, entity_id: str, base_hash: str) -> Entity:
        """Move one entity to a deterministic flat archive path unchanged."""
        plan = self.plan_archive_entity(entity_id, base_hash)
        if isinstance(plan, WorkflowPlan):
            return self.apply_task_workflow(plan)[0]
        return self.apply_mutation_plan(plan)

    def plan_archive_entity(
        self, entity_id: str, base_hash: str
    ) -> MutationPlan | WorkflowPlan:
        """Preview one exact archive without changing canonical entity files."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            return self._plan_order_aware_archive_locked(entity_id, base_hash)

    def plan_resource_allocation_create(
        self,
        axis: str,
        period_kind: str,
        period_start: str,
        input_mode: str,
        total_minutes: int,
        allocations: dict[str, int],
    ) -> WorkflowPlan:
        """Preview one dedicated active allocation-plan creation."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            normalized = self._validate_resource_allocation_inputs(
                axis, period_kind, period_start, input_mode,
                total_minutes, allocations,
            )
            key = (axis, period_kind, period_start)
            if self._active_resource_allocation_plan(key) is not None:
                raise InputError("an active Time Allocation Plan already exists")
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            entity_id = self.next_entity_id(
                "time_allocation_plan", on_date=now.date()
            )
            frontmatter = self._resource_allocation_frontmatter(
                entity_id=entity_id,
                axis=axis,
                period_kind=period_kind,
                period_start=period_start,
                revision=1,
                input_mode=input_mode,
                total_minutes=total_minutes,
                timestamp=timestamp,
            )
            effect = self._resource_allocation_create_effect(
                "plan_created", frontmatter,
                self._resource_allocation_body(normalized),
            )
            return self._build_resource_allocation_workflow_plan(
                "resource_allocation_create", effect.planned_entity,
                (effect,), {
                    "axis": axis,
                    "period_kind": period_kind,
                    "period_start": period_start,
                    "input_mode": input_mode,
                    "total_minutes": str(total_minutes),
                },
            )

    def plan_resource_allocation_revise(
        self,
        entity_id: str,
        base_hash: str,
        input_mode: str,
        total_minutes: int,
        allocations: dict[str, int],
    ) -> WorkflowPlan:
        """Preview superseding one active revision and creating its successor."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            current = self._require_mutation_current(entity_id, base_hash)
            if (
                current.entity_type != "time_allocation_plan"
                or current.frontmatter.get("status") != "active"
            ):
                raise InputError("only an active Time Allocation Plan can be revised")
            axis = current.frontmatter["axis"]
            period_kind = current.frontmatter["period_kind"]
            period_start = current.frontmatter["period_start"]
            normalized = self._validate_resource_allocation_inputs(
                axis, period_kind, period_start, input_mode,
                total_minutes, allocations,
            )
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            old_frontmatter = dict(current.frontmatter)
            old_frontmatter["status"] = "superseded"
            old_frontmatter["updated_at"] = timestamp
            superseded = self._workflow_effect_from_entity(
                "plan_superseded", current, old_frontmatter, current.body,
                preserve_path=True,
            )
            new_id = self.next_entity_id(
                "time_allocation_plan", on_date=now.date()
            )
            new_frontmatter = self._resource_allocation_frontmatter(
                entity_id=new_id,
                axis=axis,
                period_kind=period_kind,
                period_start=period_start,
                revision=int(current.frontmatter["revision"]) + 1,
                input_mode=input_mode,
                total_minutes=total_minutes,
                timestamp=timestamp,
                supersedes_id=current.entity_id,
            )
            created = self._resource_allocation_create_effect(
                "plan_created", new_frontmatter,
                self._resource_allocation_body(normalized),
            )
            return self._build_resource_allocation_workflow_plan(
                "resource_allocation_revise", current,
                (superseded, created), {
                    "input_mode": input_mode,
                    "total_minutes": str(total_minutes),
                },
            )

    def plan_resource_allocation_withdraw(
        self, entity_id: str, base_hash: str
    ) -> WorkflowPlan:
        """Preview withdrawing one active allocation revision."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            current = self._require_mutation_current(entity_id, base_hash)
            if (
                current.entity_type != "time_allocation_plan"
                or current.frontmatter.get("status") != "active"
            ):
                raise InputError("only an active Time Allocation Plan can be withdrawn")
            frontmatter = dict(current.frontmatter)
            frontmatter["status"] = "withdrawn"
            frontmatter["updated_at"] = current_time().isoformat(timespec="seconds")
            effect = self._workflow_effect_from_entity(
                "plan_withdrawn", current, frontmatter, current.body,
                preserve_path=True,
            )
            return self._build_resource_allocation_workflow_plan(
                "resource_allocation_withdraw", current, (effect,), {}
            )

    def plan_resource_allocation_expand(
        self,
        source_id: str,
        source_base_hash: str,
        target_period_kinds: tuple[str, ...],
    ) -> WorkflowPlan:
        """Preview one bounded, atomic expansion from an active source plan."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            source = planning.find(source_id)
            if source.content_hash != source_base_hash:
                raise ConflictError(source)
            if (
                source.entity_type != "time_allocation_plan"
                or source.relative_path.startswith("archive/")
                or source.frontmatter.get("status") != "active"
            ):
                raise InputError(
                    "allocation expansion requires a non-archived active source"
                )
            source_path = self._relative_plan_path(source.relative_path)
            source_bytes, source_token = self._read_regular_file_with_token(
                source_path
            )
            if hashlib.sha256(source_bytes).hexdigest() != source.content_hash:
                raise MutationPlanConflict("allocation expansion source changed")

            axis = source.frontmatter["axis"]
            source_kind = source.frontmatter["period_kind"]
            source_start = source.frontmatter["period_start"]
            source_end = source.frontmatter["period_end"]
            normalized_kinds = self._normalize_resource_allocation_expand_kinds(
                source_kind, target_period_kinds
            )
            periods = self._resource_allocation_expand_periods(
                source_kind, source_start, normalized_kinds
            )

            active_by_key: dict[tuple[str, str, str], list[Entity]] = {}
            for entity in planning.entities:
                if (
                    entity.entity_type != "time_allocation_plan"
                    or entity.relative_path.startswith("archive/")
                    or entity.frontmatter.get("status") != "active"
                ):
                    continue
                key = (
                    entity.frontmatter.get("axis", ""),
                    entity.frontmatter.get("period_kind", ""),
                    entity.frontmatter.get("period_start", ""),
                )
                active_by_key.setdefault(key, []).append(entity)

            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            generated_ids = self._reserve_resource_allocation_ids(
                planning, len(periods), now.date()
            )
            effects: list[WorkflowEffect] = []
            target_facts: list[dict[str, object]] = []
            for (period_kind, period_start, period_end), generated_id in zip(
                periods, generated_ids, strict=True
            ):
                key = (axis, period_kind, period_start)
                matches = active_by_key.get(key, [])
                if len(matches) > 1:
                    raise InputError("multiple active Time Allocation Plans")
                current = matches[0] if matches else None
                total_minutes, allocations = self._prorate_resource_allocation(
                    source, period_kind, period_start
                )
                normalized_allocations = self._validate_resource_allocation_inputs(
                    axis,
                    period_kind,
                    period_start,
                    source.frontmatter["input_mode"],
                    total_minutes,
                    allocations,
                )
                current_identity: dict[str, object] | None = None
                revision = 1
                supersedes_id: str | None = None
                if current is not None:
                    old_frontmatter = dict(current.frontmatter)
                    old_frontmatter["status"] = "superseded"
                    old_frontmatter["updated_at"] = timestamp
                    superseded = self._workflow_effect_from_entity(
                        "plan_superseded",
                        current,
                        old_frontmatter,
                        current.body,
                        preserve_path=True,
                    )
                    effects.append(superseded)
                    assert superseded.source_token is not None
                    current_identity = {
                        "entity_id": current.entity_id,
                        "relative_path": current.relative_path,
                        "content_hash": current.content_hash,
                        "source_token": (
                            f"{superseded.source_token.st_dev}:"
                            f"{superseded.source_token.st_ino}"
                        ),
                    }
                    try:
                        revision = int(current.frontmatter["revision"]) + 1
                    except (KeyError, ValueError) as error:
                        raise InputError(
                            "active allocation revision is invalid"
                        ) from error
                    supersedes_id = current.entity_id

                frontmatter = self._resource_allocation_frontmatter(
                    entity_id=generated_id,
                    axis=axis,
                    period_kind=period_kind,
                    period_start=period_start,
                    revision=revision,
                    input_mode=source.frontmatter["input_mode"],
                    total_minutes=total_minutes,
                    timestamp=timestamp,
                    supersedes_id=supersedes_id,
                )
                created = self._resource_allocation_create_effect(
                    "plan_created",
                    frontmatter,
                    self._resource_allocation_body(normalized_allocations),
                )
                effects.append(created)
                target_facts.append(
                    {
                        "period_kind": period_kind,
                        "period_start": period_start,
                        "period_end": period_end,
                        "current": current_identity,
                        "generated_id": generated_id,
                        "total_minutes": total_minutes,
                        "allocations": normalized_allocations,
                    }
                )

            source_allocations = parse_time_allocation_allocations(source.body)
            if source_allocations is None:
                raise InputError("source allocation body is invalid")
            extra_inputs = {
                "source_id": source.entity_id,
                "source_base_hash": source.content_hash,
                "source_relative_path": source.relative_path,
                "source_token": f"{source_token.st_dev}:{source_token.st_ino}",
                "axis": axis,
                "source_period_kind": source_kind,
                "source_period_start": source_start,
                "source_period_end": source_end,
                "source_input_mode": source.frontmatter["input_mode"],
                "source_total_minutes": source.frontmatter["total_minutes"],
                "source_allocations": json.dumps(
                    source_allocations, sort_keys=True, separators=(",", ":")
                ),
                "target_period_kinds": json.dumps(
                    normalized_kinds, separators=(",", ":")
                ),
                "targets": json.dumps(
                    target_facts, sort_keys=True, separators=(",", ":")
                ),
                "generated_ids": json.dumps(
                    generated_ids, separators=(",", ":")
                ),
            }
            return self._build_resource_allocation_workflow_plan(
                "resource_allocation_expand",
                source,
                tuple(effects),
                extra_inputs,
            )

    def _validate_resource_allocation_inputs(
        self,
        axis: str,
        period_kind: str,
        period_start: str,
        input_mode: str,
        total_minutes: int,
        allocations: dict[str, int],
    ) -> dict[str, int]:
        if axis not in TIME_ALLOCATION_AXES:
            raise InputError("allocation axis is invalid")
        if period_kind not in TIME_ALLOCATION_PERIOD_KINDS:
            raise InputError("allocation period kind is invalid")
        if time_allocation_period_end(period_kind, period_start) is None:
            raise InputError("allocation period start is invalid")
        if input_mode not in TIME_ALLOCATION_INPUT_MODES:
            raise InputError("allocation input mode is invalid")
        if type(total_minutes) is not int or total_minutes < 1:
            raise InputError("allocation total minutes is invalid")
        if type(allocations) is not dict or not allocations:
            raise InputError("allocations are invalid")
        normalized: dict[str, int] = {}
        expected_type = {
            "area": "area", "goal": "goal",
            "roadmap_outcome": "roadmap_outcome",
        }[axis]
        for target, value in allocations.items():
            if (
                type(target) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]+", target) is None
                or type(value) is not int
                or value < 0
            ):
                raise InputError("allocation entry is invalid")
            if target != "none":
                linked = self._find_mutation_entity(target)
                if (
                    linked.entity_type != expected_type
                    or linked.relative_path.startswith("archive/")
                ):
                    raise InputError("allocation target is invalid")
            normalized[target] = value
        expected_total = 10000 if input_mode == "ratio" else total_minutes
        if sum(normalized.values()) != expected_total:
            raise InputError("allocation values have an invalid sum")
        return dict(sorted(normalized.items()))

    def _active_resource_allocation_plan(
        self, key: tuple[str, str, str]
    ) -> Entity | None:
        matches = [
            entity for entity in self.list_entities()
            if entity.entity_type == "time_allocation_plan"
            and not entity.relative_path.startswith("archive/")
            and entity.frontmatter.get("status") == "active"
            and (
                entity.frontmatter.get("axis", ""),
                entity.frontmatter.get("period_kind", ""),
                entity.frontmatter.get("period_start", ""),
            ) == key
        ]
        if len(matches) > 1:
            raise InputError("multiple active Time Allocation Plans")
        return matches[0] if matches else None

    @staticmethod
    def _resource_allocation_body(allocations: dict[str, int]) -> str:
        lines = ["## Allocations", ""]
        lines.extend(f"- {target}: {value}" for target, value in sorted(allocations.items()))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _resource_allocation_frontmatter(
        *,
        entity_id: str,
        axis: str,
        period_kind: str,
        period_start: str,
        revision: int,
        input_mode: str,
        total_minutes: int,
        timestamp: str,
        supersedes_id: str | None = None,
    ) -> dict[str, str]:
        period_end = time_allocation_period_end(period_kind, period_start)
        assert period_end is not None
        frontmatter = {
            "id": entity_id,
            "type": "time_allocation_plan",
            "title": f"{axis} {period_kind} {period_start}",
            "axis": axis,
            "period_kind": period_kind,
            "period_start": period_start,
            "period_end": period_end.isoformat(),
            "revision": str(revision),
            "status": "active",
            "input_mode": input_mode,
            "total_minutes": str(total_minutes),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if supersedes_id is not None:
            frontmatter["supersedes_id"] = supersedes_id
        return frontmatter

    def _resource_allocation_create_effect(
        self, role: str, frontmatter: dict[str, str], body: str
    ) -> WorkflowEffect:
        self._validate_complete_frontmatter(frontmatter, "time_allocation_plan")
        destination = self._canonical_destination(frontmatter)
        if destination.exists() or destination.is_symlink():
            raise DestinationConflict("planned workflow destination is unavailable")
        after_bytes = self._canonical_document(
            frontmatter, "time_allocation_plan", body
        )
        planned = self._entity_from_bytes(
            destination.relative_to(self._root).as_posix(), after_bytes
        )
        planned_copy = self._copy_entity(planned)
        assert planned_copy is not None
        return WorkflowEffect(
            role, planned.entity_id, None, planned.relative_path, None, None,
            after_bytes, None, planned_copy, None, None,
        )

    def _build_resource_allocation_workflow_plan(
        self,
        action: str,
        target: Entity,
        effects: tuple[WorkflowEffect, ...],
        extra_inputs: dict[str, str],
    ) -> WorkflowPlan:
        operation_inputs = tuple(sorted({
            "action": action,
            "target_entity_id": target.entity_id,
            "target_base_hash": target.content_hash,
            **extra_inputs,
        }.items()))
        validation_errors = self._virtual_workflow_validation_errors(effects)
        if validation_errors:
            raise SchemaError(list(validation_errors))
        integrity_tag = self._workflow_plan_integrity_tag(
            action=action,
            target_entity_id=target.entity_id,
            target_base_hash=target.content_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=operation_inputs,
            effects=effects,
            validation_errors=validation_errors,
        )
        return WorkflowPlan(
            action, target.entity_id, target.content_hash, None, None, None,
            operation_inputs, effects, validation_errors, integrity_tag,
        )

    @staticmethod
    def _largest_remainder(
        weights: dict[str, int], total: int
    ) -> dict[str, int]:
        weight_total = sum(weights.values())
        if total < 0 or weight_total <= 0:
            raise InputError("allocation prorating inputs are invalid")
        floors = {
            key: total * value // weight_total
            for key, value in weights.items()
        }
        remaining = total - sum(floors.values())
        ranked = sorted(
            weights,
            key=lambda key: (-(total * weights[key] % weight_total), key),
        )
        for key in ranked[:remaining]:
            floors[key] += 1
        return dict(sorted(floors.items()))

    @staticmethod
    def _resource_allocation_period(
        period_kind: str, period_start: str
    ) -> tuple[datetime.datetime, datetime.datetime]:
        end_date = time_allocation_period_end(period_kind, period_start)
        try:
            start_date = datetime.date.fromisoformat(period_start)
        except (TypeError, ValueError) as error:
            raise InputError("allocation period is invalid") from error
        if end_date is None:
            raise InputError("allocation period is invalid")
        return (
            datetime.datetime.combine(start_date, datetime.time(), _MOKVIA_LOCAL_TIMEZONE),
            datetime.datetime.combine(end_date, datetime.time(), _MOKVIA_LOCAL_TIMEZONE),
        )

    @staticmethod
    def _normalize_resource_allocation_expand_kinds(
        source_kind: str, requested: object
    ) -> tuple[str, ...]:
        if source_kind not in TIME_ALLOCATION_PERIOD_KINDS:
            raise InputError("source allocation period kind is invalid")
        if not isinstance(requested, (list, tuple)) or not requested:
            raise InputError("expanded allocation period kinds are invalid")
        if any(
            not isinstance(kind, str)
            or kind not in TIME_ALLOCATION_PERIOD_KINDS
            or kind == source_kind
            for kind in requested
        ) or len(set(requested)) != len(requested):
            raise InputError("expanded allocation period kinds are invalid")
        return tuple(
            kind for kind in _RESOURCE_ALLOCATION_PERIOD_ORDER if kind in requested
        )

    def _resource_allocation_expand_periods(
        self,
        source_kind: str,
        source_start: str,
        target_kinds: tuple[str, ...],
    ) -> tuple[tuple[str, str, str], ...]:
        source_start_at, source_end = self._resource_allocation_period(
            source_kind, source_start
        )
        kinds = self._normalize_resource_allocation_expand_kinds(
            source_kind, target_kinds
        )
        periods: list[tuple[str, str, str]] = []
        source_position = _RESOURCE_ALLOCATION_PERIOD_ORDER.index(source_kind)
        for target_kind in kinds:
            target_position = _RESOURCE_ALLOCATION_PERIOD_ORDER.index(target_kind)
            if target_position < source_position:
                target_date = source_start_at.date()
                if target_kind == "week":
                    target_date += datetime.timedelta(days=(-target_date.weekday()) % 7)
                elif target_kind == "month" and target_date.day != 1:
                    target_date = (
                        datetime.date(target_date.year + 1, 1, 1)
                        if target_date.month == 12
                        else datetime.date(target_date.year, target_date.month + 1, 1)
                    )
                elif target_kind == "year" and (target_date.month, target_date.day) != (1, 1):
                    target_date = datetime.date(target_date.year + 1, 1, 1)
                while target_date < source_end.date():
                    target_start_at, target_end = self._resource_allocation_period(
                        target_kind, target_date.isoformat()
                    )
                    periods.append((
                        target_kind,
                        target_start_at.date().isoformat(),
                        target_end.date().isoformat(),
                    ))
                    target_date = target_end.date()
            else:
                target_date = source_start_at.date()
                if target_kind == "week":
                    target_date -= datetime.timedelta(days=target_date.weekday())
                elif target_kind == "month":
                    target_date = target_date.replace(day=1)
                elif target_kind == "year":
                    target_date = target_date.replace(month=1, day=1)
                target_start_at, target_end = self._resource_allocation_period(
                    target_kind, target_date.isoformat()
                )
                periods.append((
                    target_kind,
                    target_start_at.date().isoformat(),
                    target_end.date().isoformat(),
                ))
        if not periods or len(periods) > _RESOURCE_ALLOCATION_EXPAND_MAX_TARGETS:
            raise InputError("expanded allocation target count is invalid")
        return tuple(periods)

    def _prorate_resource_allocation(
        self, source: Entity, target_kind: str, target_start: str
    ) -> tuple[int, dict[str, int]]:
        source_start, source_end = self._resource_allocation_period(
            source.frontmatter["period_kind"], source.frontmatter["period_start"]
        )
        target_start_at, target_end = self._resource_allocation_period(
            target_kind, target_start
        )
        source_days = (source_end.date() - source_start.date()).days
        target_days = (target_end.date() - target_start_at.date()).days
        target_total = (
            int(source.frontmatter["total_minutes"]) * target_days + source_days // 2
        ) // source_days
        if target_total < 1:
            raise InputError("expanded allocation total is invalid")
        weights = parse_time_allocation_allocations(source.body)
        if weights is None:
            raise InputError("source allocation body is invalid")
        allocations = (
            dict(weights)
            if source.frontmatter["input_mode"] == "ratio"
            else self._largest_remainder(weights, target_total)
        )
        return target_total, allocations

    @staticmethod
    def _reserve_resource_allocation_ids(
        planning: WorkflowPlanningSnapshot, count: int, on_date: datetime.date
    ) -> tuple[str, ...]:
        if type(count) is not int or not 1 <= count <= _RESOURCE_ALLOCATION_EXPAND_MAX_TARGETS:
            raise InputError("expanded allocation ID count is invalid")
        prefix = f"time-allocation-plan-{on_date:%Y%m%d}-"
        reserved: set[str] = set()
        for suffix in range(1, 1000):
            candidate = f"{prefix}{suffix:03d}"
            if candidate not in planning.entities_by_id and candidate not in reserved:
                reserved.add(candidate)
                if len(reserved) == count:
                    return tuple(sorted(reserved))
        raise InputError("time allocation plan ID suffix exhausted")

    def _resource_allocation_sessions(
        self,
        axis: str,
        period_start: datetime.datetime,
        period_end: datetime.datetime,
        generated_at: datetime.datetime,
    ) -> list[_AllocationSession]:
        if generated_at.utcoffset() is None:
            raise InputError("generated_at must be timezone-aware")
        entities = self.list_entities()
        by_id = {entity.entity_id: entity for entity in entities}
        sessions: list[_AllocationSession] = []
        for task in entities:
            fields = task.frontmatter
            if (
                task.entity_type != "task"
                or fields.get("timer_kind") == _BREAK_TIMER_KIND
                or not fields.get("work_started_at")
            ):
                continue
            started = self._parsed_work_session_timestamp(fields["work_started_at"])
            if started is None:
                continue
            provisional = fields.get("status") == "doing" and not fields.get("work_ended_at")
            ended = (
                generated_at
                if provisional
                else self._parsed_work_session_timestamp(fields.get("work_ended_at", ""))
            )
            if ended is None:
                continue
            clipped_start = max(started, period_start)
            clipped_end = min(ended, period_end)
            if clipped_end <= clipped_start:
                continue
            project = by_id.get(fields.get("project_id", ""))
            target = "none"
            if axis == "area":
                target = fields.get("area_id") or (
                    project.frontmatter.get("area_id", "")
                    if project is not None and project.entity_type == "project"
                    else ""
                ) or "none"
            elif axis == "roadmap_outcome":
                target = (
                    project.frontmatter.get("roadmap_outcome_id", "")
                    if project is not None and project.entity_type == "project"
                    else ""
                ) or "none"
            elif axis == "goal":
                if project is not None and project.entity_type == "project":
                    target = project.frontmatter.get("goal_id", "")
                    if not target:
                        outcome = by_id.get(project.frontmatter.get("roadmap_outcome_id", ""))
                        if outcome is not None and outcome.entity_type == "roadmap_outcome":
                            target = outcome.frontmatter.get("goal_id", "")
                target = target or "none"
            else:
                raise InputError("allocation axis is invalid")
            sessions.append(
                _AllocationSession(
                    task, clipped_start, clipped_end, provisional, target
                )
            )
        return sorted(
            sessions,
            key=lambda item: (
                item.start, item.end, item.task.entity_id,
            ),
        )

    def resource_allocation_report(
        self,
        axis: str,
        period_kind: str,
        period_start: str,
        *,
        generated_at: datetime.datetime | None = None,
    ) -> dict[str, object]:
        """Build one read-only current-hierarchy allocation report."""
        if axis not in TIME_ALLOCATION_AXES:
            raise InputError("allocation axis is invalid")
        start, end = self._resource_allocation_period(period_kind, period_start)
        generated = current_time() if generated_at is None else generated_at
        generated = generated.astimezone(_MOKVIA_LOCAL_TIMEZONE)
        sessions = self._resource_allocation_sessions(
            axis, start, end, generated
        )
        active_plan = self._active_resource_allocation_plan(
            (axis, period_kind, period_start)
        )
        plan_allocations = (
            {}
            if active_plan is None
            else parse_time_allocation_allocations(active_plan.body) or {}
        )
        target_total = (
            0 if active_plan is None
            else int(active_plan.frontmatter["total_minutes"])
        )
        if active_plan is None:
            target_minutes: dict[str, int] = {}
        elif active_plan.frontmatter["input_mode"] == "ratio":
            target_minutes = self._largest_remainder(
                plan_allocations, target_total
            )
        else:
            target_minutes = dict(plan_allocations)
        actual_seconds: dict[str, int] = {}
        for session in sessions:
            actual_seconds[session.target] = actual_seconds.get(session.target, 0) + int(
                (session.end - session.start).total_seconds()
            )
        entities_by_id = {entity.entity_id: entity for entity in self.list_entities()}
        axis_type = {
            "area": "area", "goal": "goal",
            "roadmap_outcome": "roadmap_outcome",
        }[axis]
        selectable_tokens = {
            entity.entity_id for entity in entities_by_id.values()
            if entity.entity_type == axis_type
            and not entity.relative_path.startswith("archive/")
        }
        tokens = sorted(
            set(target_minutes) | set(actual_seconds) | selectable_tokens | {"none"}
        )
        for token in tokens:
            target_minutes.setdefault(token, 0)
        actual_total_seconds = sum(actual_seconds.values())
        rows: list[dict[str, object]] = []
        for token in tokens:
            seconds = actual_seconds.get(token, 0)
            minutes = seconds / 60
            target_value = target_minutes.get(token, 0)
            target_entity = entities_by_id.get(token)
            target_ratio = (
                plan_allocations.get(token, 0) / 10000
                if active_plan is not None
                and active_plan.frontmatter["input_mode"] == "ratio"
                else (target_value / target_total if target_total else 0.0)
            )
            rows.append({
                "target": token,
                "title": (
                    "未分類"
                    if token == "none"
                    else (
                        target_entity.frontmatter.get("title", token)
                        if target_entity is not None else token
                    )
                ),
                "target_minutes": target_value,
                "target_ratio": target_ratio,
                "actual_seconds": seconds,
                "actual_minutes": round(minutes, 2),
                "actual_ratio": (
                    seconds / actual_total_seconds if actual_total_seconds else 0.0
                ),
                "variance_minutes": round(minutes - target_value, 2),
            })
        warnings: list[dict[str, object]] = []
        for index, first in enumerate(sessions):
            for second in sessions[index + 1 :]:
                if second.start >= first.end:
                    break
                intersection_start = max(first.start, second.start)
                intersection_end = min(first.end, second.end)
                if intersection_end <= intersection_start:
                    continue
                warnings.append({
                    "first_task_id": first.task.entity_id,
                    "second_task_id": second.task.entity_id,
                    "first_session_start": first.start.isoformat(timespec="seconds"),
                    "first_session_end": first.end.isoformat(timespec="seconds"),
                    "second_session_start": second.start.isoformat(timespec="seconds"),
                    "second_session_end": second.end.isoformat(timespec="seconds"),
                    "intersection_start": intersection_start.isoformat(timespec="seconds"),
                    "intersection_end": intersection_end.isoformat(timespec="seconds"),
                })
        trend = self._resource_allocation_trend(
            start, end, sessions, target_minutes
        )
        trend_by_target = {
            token: [
                {
                    "period_start": bucket["period_start"],
                    "bucket_end": bucket["bucket_end"],
                    "target_minutes": next(
                        row["target_minutes"]
                        for row in bucket["rows"]
                        if row["target"] == token
                    ),
                    "actual_seconds": next(
                        row["actual_seconds"]
                        for row in bucket["rows"]
                        if row["target"] == token
                    ),
                    "actual_minutes": next(
                        row["actual_minutes"]
                        for row in bucket["rows"]
                        if row["target"] == token
                    ),
                }
                for bucket in trend
                if any(row["target"] == token for row in bucket["rows"])
            ]
            for token in tokens
        }
        provisional_seconds = sum(
            int((session.end - session.start).total_seconds())
            for session in sessions if session.provisional
        )
        attributed_seconds = actual_total_seconds - actual_seconds.get("none", 0)
        return {
            "axis": axis,
            "period_kind": period_kind,
            "period_start": start.date().isoformat(),
            "period_end": end.date().isoformat(),
            "generated_at": generated.isoformat(timespec="seconds"),
            "plan": (
                None
                if active_plan is None
                else {
                    "id": active_plan.entity_id,
                    "path": active_plan.relative_path,
                    "frontmatter": dict(active_plan.frontmatter),
                    "body": active_plan.body,
                    "content_hash": active_plan.content_hash,
                    "allocations": dict(plan_allocations),
                    "input_mode": active_plan.frontmatter["input_mode"],
                    "total_minutes": int(active_plan.frontmatter["total_minutes"]),
                }
            ),
            "summary": {
                "target_total_minutes": target_total,
                "actual_total_seconds": actual_total_seconds,
                "actual_total_minutes": round(actual_total_seconds / 60, 2),
                "variance_minutes": round(actual_total_seconds / 60 - target_total, 2),
                "attribution_rate": (
                    attributed_seconds / actual_total_seconds
                    if actual_total_seconds else 0.0
                ),
                "provisional_active_seconds": provisional_seconds,
                "overlap_count": len(warnings),
            },
            "rows": rows,
            "trend": trend,
            "trend_by_target": trend_by_target,
            "overlap_warnings": warnings,
        }

    @staticmethod
    def _resource_allocation_trend(
        start: datetime.datetime,
        end: datetime.datetime,
        sessions: list[_AllocationSession],
        target_minutes: dict[str, int],
    ) -> list[dict[str, object]]:
        boundaries: list[datetime.datetime] = []
        cursor = start
        if (end - start).days == 1:
            step = datetime.timedelta(hours=1)
            while cursor < end:
                cursor = min(cursor + step, end)
                boundaries.append(cursor)
        elif (end - start).days <= 31:
            step = datetime.timedelta(days=1)
            while cursor < end:
                cursor = min(cursor + step, end)
                boundaries.append(cursor)
        else:
            while cursor < end:
                next_month = (
                    datetime.datetime(cursor.year + 1, 1, 1, tzinfo=cursor.tzinfo)
                    if cursor.month == 12
                    else datetime.datetime(cursor.year, cursor.month + 1, 1, tzinfo=cursor.tzinfo)
                )
                cursor = min(next_month, end)
                boundaries.append(cursor)
        total_seconds = (end - start).total_seconds()
        trend: list[dict[str, object]] = []
        for boundary in boundaries:
            actual_by_target: dict[str, int] = {}
            for session in sessions:
                clipped_end = min(session.end, boundary)
                if clipped_end <= session.start:
                    continue
                actual_by_target[session.target] = actual_by_target.get(session.target, 0) + int(
                    (clipped_end - session.start).total_seconds()
                )
            elapsed_ratio = (boundary - start).total_seconds() / total_seconds
            tokens = sorted(set(target_minutes) | set(actual_by_target) | {"none"})
            rows = [{
                "target": token,
                "target_minutes": round(target_minutes.get(token, 0) * elapsed_ratio, 2),
                "actual_seconds": actual_by_target.get(token, 0),
                "actual_minutes": round(actual_by_target.get(token, 0) / 60, 2),
            } for token in tokens]
            trend.append({
                "period_start": boundary.date().isoformat(),
                "bucket_end": boundary.isoformat(timespec="seconds"),
                "target_minutes": round(sum(target_minutes.values()) * elapsed_ratio, 2),
                "actual_seconds": sum(actual_by_target.values()),
                "actual_minutes": round(sum(actual_by_target.values()) / 60, 2),
                "rows": rows,
            })
        return trend

    def resource_allocation_tasks(
        self,
        axis: str,
        period_kind: str,
        period_start: str,
        target: str,
        *,
        limit: int,
        cursor: str | None = None,
        generated_at: datetime.datetime | None = None,
    ) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InputError("allocation task limit is invalid")
        start, end = self._resource_allocation_period(period_kind, period_start)
        generated = current_time() if generated_at is None else generated_at
        sessions = [
            session for session in self._resource_allocation_sessions(
                axis, start, end, generated.astimezone(_MOKVIA_LOCAL_TIMEZONE)
            ) if session.target == target
        ]
        offset = 0
        if cursor is not None:
            try:
                offset = int(base64.urlsafe_b64decode(cursor + "==").decode("ascii"))
            except (ValueError, UnicodeError) as error:
                raise InputError("allocation task cursor is invalid") from error
            if offset < 0 or offset > len(sessions):
                raise InputError("allocation task cursor is invalid")
        page = sessions[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            None
            if next_offset >= len(sessions)
            else base64.urlsafe_b64encode(str(next_offset).encode("ascii")).decode("ascii").rstrip("=")
        )
        return {
            "axis": axis,
            "period_kind": period_kind,
            "period_start": start.date().isoformat(),
            "period_end": end.date().isoformat(),
            "target": target,
            "items": [{
                "task_id": session.task.entity_id,
                "title": session.task.frontmatter.get("title", session.task.entity_id),
                "path": session.task.relative_path,
                "archived": session.task.relative_path.startswith("archive/"),
                "session_start": session.start.isoformat(timespec="seconds"),
                "session_end": session.end.isoformat(timespec="seconds"),
                "actual_seconds": int((session.end - session.start).total_seconds()),
                "provisional": session.provisional,
            } for session in page],
            "next_cursor": next_cursor,
        }

    def resource_allocation_plan_draft(
        self, plan_id: str, period_kind: str, period_start: str
    ) -> dict[str, object]:
        source = self.get_entity(plan_id)
        if (
            source.entity_type != "time_allocation_plan"
            or source.frontmatter.get("status") != "active"
        ):
            raise InputError("draft source must be an active Time Allocation Plan")
        source_start, source_end = self._resource_allocation_period(
            source.frontmatter["period_kind"], source.frontmatter["period_start"]
        )
        target_start, target_end = self._resource_allocation_period(
            period_kind, period_start
        )
        source_days = (source_end.date() - source_start.date()).days
        target_days = (target_end.date() - target_start.date()).days
        source_total = int(source.frontmatter["total_minutes"])
        target_total = (source_total * target_days + source_days // 2) // source_days
        source_allocations = parse_time_allocation_allocations(source.body)
        if source_allocations is None:
            raise InputError("draft source allocations are invalid")
        if source.frontmatter["input_mode"] == "ratio":
            allocations = dict(source_allocations)
        else:
            allocations = self._largest_remainder(source_allocations, target_total)
        return {
            "source_plan_id": source.entity_id,
            "axis": source.frontmatter["axis"],
            "period_kind": period_kind,
            "period_start": target_start.date().isoformat(),
            "period_end": target_end.date().isoformat(),
            "input_mode": source.frontmatter["input_mode"],
            "total_minutes": target_total,
            "allocations": allocations,
        }

    def apply_mutation_plan(self, plan: MutationPlan) -> Entity:
        """Apply the exact bytes in a still-current, internally coherent plan."""
        with self.mutation_lock():
            return self._apply_mutation_plan_locked(plan)

    def plan_task_start(
        self,
        entity_id: str,
        base_hash: str,
        *,
        active_entity_id: str | None = None,
        active_base_hash: str | None = None,
        resolution: str | None = None,
        active_work_ended_at: str | None = None,
    ) -> WorkflowPlan:
        """Preview starting one Task, including an explicitly resolved switch."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            target = self._require_workflow_task(
                entity_id, base_hash, planning
            )
            if target.frontmatter.get("status") == "done":
                raise InputError("done Tasks cannot be started")
            doing = self._current_doing_task(planning)
            expected_doing_id = "" if doing is None else doing.entity_id
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            effects: list[WorkflowEffect] = []
            projected_target: Entity | None = None

            if doing is not None and doing.entity_id != target.entity_id:
                if resolution not in {"interrupt", "complete"}:
                    raise InputError(
                        "starting another Task requires interrupt or complete resolution"
                    )
                if active_entity_id != doing.entity_id or active_base_hash is None:
                    raise InputError("current doing Task identity and hash are required")
                current = self._require_workflow_task(
                    active_entity_id, active_base_hash, planning
                )
                if current.entity_id != doing.entity_id:
                    raise MutationPlanConflict("current doing Task changed")
                if resolution == "interrupt":
                    interruption = self._plan_interrupt_effects(
                        current,
                        now,
                        timestamp,
                        planning,
                        work_ended_at=active_work_ended_at,
                    )
                    for effect in interruption:
                        if (
                            effect.role == "dependency_retargeted"
                            and effect.entity_id == target.entity_id
                        ):
                            projected_target = effect.planned_entity
                        else:
                            effects.append(effect)
                else:
                    effects.append(
                        self._plan_close_effect(
                            current,
                            "completed",
                            now,
                            timestamp,
                            work_ended_at=active_work_ended_at,
                        )
                    )
                    releases = self._dependency_release_effects(
                        tuple(effects), timestamp, planning=planning
                    )
                    for release in releases:
                        if release.entity_id == target.entity_id:
                            projected_target = release.planned_entity
                        else:
                            effects.append(release)
            elif any(
                value is not None
                for value in (
                    active_entity_id,
                    active_base_hash,
                    resolution,
                    active_work_ended_at,
                )
            ):
                raise InputError("unexpected switch resolution without another doing Task")

            projected_statuses = {
                effect.entity_id: effect.planned_entity.frontmatter.get("status", "")
                for effect in effects
            }
            dependency_source = (
                dict(projected_target.frontmatter)
                if projected_target is not None
                else dict(target.frontmatter)
            )
            if not self._task_available_from_reached(dependency_source, now):
                raise InputError("Task available_from is in the future")
            if not self._dependencies_satisfied(
                dependency_source, projected_statuses, planning=planning
            ):
                raise InputError("Task has unfinished dependencies")
            if dependency_source.get("waiting_for"):
                raise InputError("Task is still waiting_for an external condition")
            effects.extend(
                self._linked_project_start_effects(
                    target, timestamp, planning=planning
                )
            )
            effects.append(
                self._plan_start_effect(
                    target,
                    timestamp,
                    projected=projected_target,
                    work_started_at=active_work_ended_at,
                )
            )
            preview = self._prepare_workflow_preview_locked(
                action="start",
                target=target,
                active=doing if doing is not None and doing.entity_id != target.entity_id else None,
                resolution=resolution,
                expected_doing_id=expected_doing_id,
                timestamp=timestamp,
                work_event_timestamp=active_work_ended_at,
                effects=tuple(effects),
            )
        return self._finalize_workflow_preview(preview)

    def plan_available_task_releases(
        self, now: datetime.datetime
    ) -> WorkflowPlan | None:
        """Preview all ready auto-gated waiting Tasks as one atomic workflow."""
        if not isinstance(now, datetime.datetime) or now.utcoffset() is None:
            raise InputError("now must be a timezone-aware datetime")
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            candidates = sorted(
                (
                    entity
                    for entity in planning.entities
                    if entity.entity_type == "task"
                    and not entity.relative_path.startswith("archive/")
                    and entity.frontmatter.get("status") == "waiting"
                    and self._task_has_automatic_gate(
                        dict(entity.frontmatter), planning
                    )
                    and self._task_release_gates_satisfied(
                        dict(entity.frontmatter),
                        now,
                        planning=planning,
                    )
                ),
                key=lambda entity: entity.entity_id,
            )
            if not candidates:
                return None
            timestamp = current_time().isoformat(timespec="seconds")
            effects = tuple(
                self._waiting_release_effect(
                    "availability_released", candidate, timestamp
                )
                for candidate in candidates
            )
            doing = self._current_doing_task(planning)
            preview = self._prepare_workflow_preview_locked(
                action="availability_release",
                target=candidates[0],
                active=None,
                resolution=None,
                expected_doing_id="" if doing is None else doing.entity_id,
                timestamp=timestamp,
                effects=effects,
            )
        return self._finalize_workflow_preview(preview)

    def plan_task_create_and_start(
        self,
        title: str,
        body: str = "",
        *,
        area_id: str | None = None,
        active_entity_id: str | None = None,
        active_base_hash: str | None = None,
        resolution: str | None = None,
        active_work_ended_at: str | None = None,
    ) -> WorkflowPlan:
        """Preview creating a fresh Task directly as the sole doing Task."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            create_fields = {"title": title}
            if area_id is not None:
                create_fields["area_id"] = area_id
            self._validate_mutation_fields("task", create_fields, create=True)
            if body != "":
                raise InputError("create-and-start body must be empty")

            doing = self._current_doing_task(planning)
            expected_doing_id = "" if doing is None else doing.entity_id
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            effects: list[WorkflowEffect] = []
            if doing is None:
                if any(
                    value is not None
                    for value in (
                        active_entity_id,
                        active_base_hash,
                        resolution,
                        active_work_ended_at,
                    )
                ):
                    raise InputError(
                        "unexpected active resolution without a doing Task"
                    )
            else:
                if resolution not in {"complete", "interrupt"}:
                    raise InputError(
                        "create-and-start requires resolving the current doing Task"
                    )
                if active_entity_id != doing.entity_id or active_base_hash is None:
                    raise InputError("current doing Task identity and hash are required")
                current = self._require_workflow_task(
                    active_entity_id, active_base_hash, planning
                )
                if current.entity_id != doing.entity_id:
                    raise MutationPlanConflict("current doing Task changed")
                if resolution == "interrupt":
                    effects.extend(
                        self._plan_interrupt_effects(
                            current,
                            now,
                            timestamp,
                            planning,
                            work_ended_at=active_work_ended_at,
                        )
                    )
                else:
                    effects.append(
                        self._plan_close_effect(
                            current,
                            "completed",
                            now,
                            timestamp,
                            work_ended_at=active_work_ended_at,
                        )
                    )
                    effects.extend(
                        self._dependency_release_effects(
                            tuple(effects), timestamp, planning=planning
                        )
                    )

            entity_id = self._next_task_id_from_planning(
                planning, now.date()
            )
            reserved_ids = {effect.entity_id for effect in effects}
            while entity_id in reserved_ids:
                prefix, separator, suffix = entity_id.rpartition("-")
                next_suffix = int(suffix) + 1 if separator else 1000
                if next_suffix > 999:
                    raise InputError("entity id suffix exhausted for create-and-start Task")
                entity_id = f"{prefix}-{next_suffix:03d}"
            try:
                self._find_mutation_entity(entity_id)
            except NotFoundError:
                pass
            else:
                raise DestinationConflict(
                    "planned create-and-start ID is unavailable"
                )
            frontmatter = {
                "id": entity_id,
                "type": "task",
                "title": title,
                "status": "doing",
                "created_at": timestamp,
                "updated_at": timestamp,
                "work_started_at": active_work_ended_at or timestamp,
                "resume_status": "next",
            }
            if area_id is not None:
                frontmatter["area_id"] = area_id
            self._validate_links(frontmatter, "task")
            created = self._workflow_create_effect("started", frontmatter, body)
            effects.append(created)
            preview = self._prepare_workflow_preview_locked(
                action="create_and_start",
                target=created.planned_entity,
                active=doing,
                resolution=resolution,
                expected_doing_id=expected_doing_id,
                timestamp=timestamp,
                work_event_timestamp=active_work_ended_at,
                effects=tuple(effects),
            )
        return self._finalize_workflow_preview(preview)

    def plan_project_task_plan_create(
        self,
        project_id: str,
        base_hash: str,
        tasks: list[dict[str, object]],
        *,
        mode: str | None = None,
    ) -> WorkflowPlan:
        """Preview a legacy DAG or a saved Task forest for one Project."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            project = self._require_mutation_current(project_id, base_hash)
            if (
                project.entity_type != "project"
                or project.relative_path.startswith("archive/")
                or project.frontmatter.get("status")
                not in {"not_started", "doing"}
            ):
                raise ProjectOperationError("project_not_executable")
            if type(tasks) is not list or not tasks or mode not in {None, "tree"}:
                raise InputError("Project Task plan must contain Tasks")

            by_key: dict[str, dict[str, object]] = {}
            input_order: list[str] = []
            for item in tasks:
                expected_keys = (
                    {"key", "title", "parent_key"}
                    if mode == "tree"
                    else {"key", "title", "depends_on_keys"}
                )
                if (
                    type(item) is not dict
                    or not expected_keys <= set(item)
                    or set(item) - expected_keys - ({"status"} if mode == "tree" else set())
                ):
                    raise InputError("Project Task plan item is invalid")
                key = item["key"]
                title = item["title"]
                parent_key = item.get("parent_key")
                task_status = item.get("status")
                dependencies = (
                    [] if parent_key is None else [parent_key]
                ) if mode == "tree" else item["depends_on_keys"]
                if (
                    not isinstance(key, str)
                    or not key.strip()
                    or key in by_key
                    or not isinstance(title, str)
                    or not title.strip()
                    or (
                        mode == "tree"
                        and parent_key is not None
                        and (not isinstance(parent_key, str) or not parent_key.strip())
                    )
                    or (
                        mode == "tree"
                        and (
                            (parent_key is not None and "status" in item)
                            or ("status" in item and task_status not in {"planned", "next"})
                        )
                    )
                    or type(dependencies) is not list
                    or any(
                        not isinstance(dependency, str) or not dependency.strip()
                        for dependency in dependencies
                    )
                    or len(dependencies) != len(set(dependencies))
                ):
                    raise InputError("Project Task plan item is invalid")
                by_key[key] = {
                    "key": key,
                    "title": title,
                    "dependency_keys": list(dependencies),
                    **(
                        {
                            "parent_key": parent_key,
                            **({"status": task_status} if "status" in item else {}),
                        }
                        if mode == "tree"
                        else {"depends_on_keys": list(dependencies)}
                    ),
                }
                input_order.append(key)

            all_keys = set(by_key)
            for key, item in by_key.items():
                dependencies = item["dependency_keys"]
                assert isinstance(dependencies, list)
                if key in dependencies or any(
                    dependency not in all_keys for dependency in dependencies
                ):
                    raise InputError("Project Task plan dependency is invalid")

            ordered_keys: list[str] = []
            resolved: set[str] = set()
            remaining = list(input_order)
            while remaining:
                ready = next(
                    (
                        key
                        for key in remaining
                        if set(by_key[key]["dependency_keys"]) <= resolved
                    ),
                    None,
                )
                if ready is None:
                    raise InputError("Project Task plan contains a cycle")
                remaining.remove(ready)
                ordered_keys.append(ready)
                resolved.add(ready)

            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            next_id = self.next_entity_id("task", on_date=now.date())
            prefix, separator, suffix = next_id.rpartition("-")
            if not separator:
                raise InputError("generated Task ID is invalid")
            first_suffix = int(suffix)
            generated_ids: dict[str, str] = {}
            for offset, key in enumerate(ordered_keys):
                generated_suffix = first_suffix + offset
                if generated_suffix > 999:
                    raise InputError(f"entity id suffix exhausted for {prefix}")
                generated_ids[key] = f"{prefix}-{generated_suffix:03d}"

            effects: list[WorkflowEffect] = []
            encoded_tasks: list[dict[str, object]] = []
            sibling_positions: dict[str | None, int] = {}
            for key in ordered_keys:
                item = by_key[key]
                dependencies = item["dependency_keys"]
                assert isinstance(dependencies, list)
                dependency_ids = [generated_ids[value] for value in dependencies]
                frontmatter = {
                    "id": generated_ids[key],
                    "type": "task",
                    "title": str(item["title"]),
                    "status": (
                        "planned"
                        if mode == "tree" and (dependency_ids or item.get("status") == "planned")
                        else "waiting"
                        if dependency_ids
                        else "next"
                    ),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "project_id": project.entity_id,
                }
                if dependency_ids:
                    frontmatter["depends_on"] = self._inline_list(dependency_ids)
                if mode == "tree":
                    parent_key = item["parent_key"]
                    assert parent_key is None or isinstance(parent_key, str)
                    sibling_positions[parent_key] = sibling_positions.get(parent_key, 0) + 1
                    frontmatter["project_position"] = str(
                        sibling_positions[parent_key]
                    )
                effects.append(
                    self._workflow_create_effect("task_created", frontmatter, "")
                )
                encoded_tasks.append(
                    {
                        **{
                            name: value
                            for name, value in item.items()
                            if name != "dependency_keys"
                        },
                        "generated_id": generated_ids[key],
                    }
                )

            operation_inputs = (
                ("action", "project_task_plan_create"),
                ("target_entity_id", project.entity_id),
                ("target_base_hash", project.content_hash),
                *(((("mode", "tree"),) if mode == "tree" else ())),
                (
                    "tasks",
                    json.dumps(
                        encoded_tasks,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            effect_tuple = tuple(effects)
            validation_errors = self._virtual_workflow_validation_errors(effect_tuple)
            if validation_errors:
                raise SchemaError(list(validation_errors))
            integrity_tag = self._workflow_plan_integrity_tag(
                action="project_task_plan_create",
                target_entity_id=project.entity_id,
                target_base_hash=project.content_hash,
                active_entity_id=None,
                active_base_hash=None,
                resolution=None,
                operation_inputs=operation_inputs,
                effects=effect_tuple,
                validation_errors=validation_errors,
            )
            return WorkflowPlan(
                action="project_task_plan_create",
                target_entity_id=project.entity_id,
                target_base_hash=project.content_hash,
                active_entity_id=None,
                active_base_hash=None,
                resolution=None,
                operation_inputs=operation_inputs,
                effects=effect_tuple,
                validation_errors=validation_errors,
                integrity_tag=integrity_tag,
            )

    def plan_project_task_plan_update(
        self,
        project_id: str,
        base_hash: str,
        nodes: list[dict[str, object]],
        archives: list[dict[str, object]],
    ) -> WorkflowPlan:
        """Preview one exact replacement of a Project's unfinished Task tree."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            project = self._require_mutation_current(project_id, base_hash)
            if (
                project.entity_type != "project"
                or project.relative_path.startswith("archive/")
                or project.frontmatter.get("status") not in {"not_started", "doing"}
            ):
                raise ProjectOperationError("project_not_executable")
            if type(nodes) is not list or type(archives) is not list:
                raise InputError("Project Task plan update is invalid")

            tree_members = tuple(
                entity
                for entity in self._project_task_tree_members(project.entity_id)
                if entity.frontmatter.get("status") in {"planned", "next", "doing"}
            )
            current_by_id = {entity.entity_id: entity for entity in tree_members}
            if len(current_by_id) != len(tree_members):
                raise InputError("duplicate Project Task ID")
            by_key: dict[str, dict[str, object]] = {}
            existing_key_by_id: dict[str, str] = {}
            input_order: list[str] = []
            for raw in nodes:
                if type(raw) is not dict:
                    raise InputError("Project Task plan update node is invalid")
                existing = "id" in raw or "base_hash" in raw
                required = (
                    {"key", "id", "base_hash", "title", "parent_key", "status"}
                    if existing
                    else {"key", "title", "parent_key", "status"}
                )
                if set(raw) != required:
                    raise InputError("Project Task plan update node is invalid")
                key = raw.get("key")
                title = raw.get("title")
                parent_key = raw.get("parent_key")
                status = raw.get("status")
                if (
                    not isinstance(key, str)
                    or not key.strip()
                    or key in by_key
                    or not isinstance(title, str)
                    or not title.strip()
                    or (parent_key is not None and (
                        not isinstance(parent_key, str) or not parent_key.strip()
                    ))
                    or not isinstance(status, str)
                ):
                    raise InputError("Project Task plan update node is invalid")
                item = dict(raw)
                item["key"] = key
                item["title"] = title
                if existing:
                    entity_id = raw.get("id")
                    node_hash = raw.get("base_hash")
                    if (
                        not isinstance(entity_id, str)
                        or not isinstance(node_hash, str)
                        or entity_id in existing_key_by_id
                    ):
                        raise InputError("Project Task plan update node is invalid")
                    current = current_by_id.get(entity_id)
                    if current is None:
                        try:
                            outside = self._find_mutation_entity(entity_id)
                        except NotFoundError as error:
                            raise InputError(
                                "Project Task plan update Task is unavailable"
                            ) from error
                        if outside.entity_type == "task" and outside.frontmatter.get(
                            "status"
                        ) == "done":
                            raise InputError("completed Task cannot be edited")
                        raise InputError("Project Task is outside the editable tree")
                    if current.content_hash != node_hash:
                        raise ConflictError(current)
                    if (
                        status == "doing"
                        and current.frontmatter.get("status") != "doing"
                    ):
                        raise InputError(
                            "Task start requires the dedicated start workflow"
                        )
                    existing_key_by_id[entity_id] = key
                elif status == "doing":
                    raise InputError("new Project Task cannot be doing")
                by_key[key] = item
                input_order.append(key)

            archive_by_id: dict[str, Entity] = {}
            normalized_archives: list[dict[str, str]] = []
            for raw in archives:
                if type(raw) is not dict or set(raw) != {"id", "base_hash"}:
                    raise InputError("Project Task plan archive is invalid")
                entity_id = raw.get("id")
                archive_hash = raw.get("base_hash")
                if (
                    not isinstance(entity_id, str)
                    or not isinstance(archive_hash, str)
                    or entity_id in archive_by_id
                    or entity_id in existing_key_by_id
                ):
                    raise InputError("Project Task plan archive is invalid")
                current = current_by_id.get(entity_id)
                if current is None:
                    try:
                        outside = self._find_mutation_entity(entity_id)
                    except NotFoundError as error:
                        raise InputError(
                            "Project Task plan archive Task is unavailable"
                        ) from error
                    if outside.entity_type == "task" and outside.frontmatter.get(
                        "status"
                    ) == "done":
                        raise InputError("completed Task cannot be archived")
                    raise InputError("Project Task is outside the editable tree")
                if current.content_hash != archive_hash:
                    raise ConflictError(current)
                if current.frontmatter.get("status") in {"doing", "done"}:
                    raise InputError("running or completed Task cannot be archived")
                archive_by_id[entity_id] = current
                normalized_archives.append(
                    {"id": entity_id, "base_hash": archive_hash}
                )

            expected_current = set(current_by_id)
            submitted_current = set(existing_key_by_id) | set(archive_by_id)
            if expected_current != submitted_current:
                raise MutationPlanConflict("Project Task tree membership changed")
            if not nodes and not archives:
                raise InputError("Project Task plan update has no Tasks")

            all_keys = set(by_key)
            for key, item in by_key.items():
                parent_key = item["parent_key"]
                if parent_key == key or (
                    parent_key is not None and parent_key not in all_keys
                ):
                    raise InputError("Project Task plan parent is invalid")
                status = item["status"]
                if parent_key is None:
                    if status not in {"planned", "next", "doing"}:
                        raise InputError("Project Task root status is invalid")
                elif status != "planned":
                    entity_id = item.get("id")
                    current = (
                        current_by_id.get(entity_id)
                        if isinstance(entity_id, str)
                        else None
                    )
                    if current is None or (
                        status != "doing"
                        or current.frontmatter.get("status") != "doing"
                    ):
                        raise InputError("Project Task child must be planned")

            resolved: set[str] = set()
            remaining = list(input_order)
            while remaining:
                ready = next(
                    (
                        key
                        for key in remaining
                        if by_key[key]["parent_key"] is None
                        or by_key[key]["parent_key"] in resolved
                    ),
                    None,
                )
                if ready is None:
                    raise InputError("Project Task plan contains a cycle")
                remaining.remove(ready)
                resolved.add(ready)

            archived_ids = set(archive_by_id)

            def promoted_parent_id(entity: Entity) -> str | None:
                parent_id = self._project_task_primary_parent(
                    dict(entity.frontmatter)
                )
                seen: set[str] = set()
                while parent_id in archived_ids:
                    if parent_id in seen:
                        raise InputError("Project Task archive ancestry is cyclic")
                    seen.add(parent_id)
                    parent_id = self._project_task_primary_parent(
                        dict(archive_by_id[parent_id].frontmatter)
                    )
                return parent_id

            for entity_id, key in existing_key_by_id.items():
                current = current_by_id[entity_id]
                current_parent = self._project_task_primary_parent(
                    dict(current.frontmatter)
                )
                if current_parent in archived_ids:
                    expected_parent_id = promoted_parent_id(current)
                    expected_parent_key = (
                        None
                        if expected_parent_id is None
                        else existing_key_by_id.get(expected_parent_id)
                    )
                    if expected_parent_key is None and expected_parent_id is not None:
                        raise InputError(
                            "archived Task child cannot be promoted outside the tree"
                        )
                    if by_key[key]["parent_key"] != expected_parent_key:
                        raise InputError(
                            "archived Task child must be promoted to its ancestor"
                        )

            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            new_keys = [key for key in input_order if "id" not in by_key[key]]
            generated_ids: dict[str, str] = {}
            if new_keys:
                next_id = self.next_entity_id("task", on_date=now.date())
                prefix, separator, suffix = next_id.rpartition("-")
                if not separator:
                    raise InputError("generated Task ID is invalid")
                first_suffix = int(suffix)
                for offset, key in enumerate(new_keys):
                    generated_suffix = first_suffix + offset
                    if generated_suffix > 999:
                        raise InputError(f"entity id suffix exhausted for {prefix}")
                    generated_ids[key] = f"{prefix}-{generated_suffix:03d}"

            resolved_id_by_key = {
                key: (
                    str(item["id"])
                    if "id" in item
                    else generated_ids[key]
                )
                for key, item in by_key.items()
            }
            sibling_positions: dict[str | None, int] = {}
            effects: list[WorkflowEffect] = []
            encoded_nodes: list[dict[str, object]] = []
            for key in input_order:
                item = by_key[key]
                parent_key = item["parent_key"]
                assert parent_key is None or isinstance(parent_key, str)
                sibling_positions[parent_key] = sibling_positions.get(parent_key, 0) + 1
                position = str(sibling_positions[parent_key])
                parent_id = (
                    None if parent_key is None else resolved_id_by_key[parent_key]
                )
                existing = "id" in item
                secondary_ids: list[str] = []
                current: Entity | None = None
                if existing:
                    current = current_by_id[str(item["id"])]
                    dependencies = parse_inline_list(
                        current.frontmatter.get("depends_on", "")
                    )
                    secondary_ids = list(dependencies[1:] if dependencies else [])
                dependency_ids = ([] if parent_id is None else [parent_id]) + [
                    dependency
                    for dependency in secondary_ids
                    if dependency != parent_id
                ]
                if existing:
                    assert current is not None
                    frontmatter = dict(current.frontmatter)
                    frontmatter["title"] = str(item["title"])
                    frontmatter["status"] = str(item["status"])
                    if (
                        current.frontmatter.get("status")
                        != frontmatter["status"]
                        and frontmatter["status"] == "planned"
                    ):
                        frontmatter.pop("action_date", None)
                    frontmatter["project_position"] = position
                    if dependency_ids:
                        frontmatter["depends_on"] = self._inline_list(dependency_ids)
                    else:
                        frontmatter.pop("depends_on", None)
                    if current.frontmatter.get("status") == "doing" and (
                        frontmatter.get("title")
                        != current.frontmatter.get("title")
                        or frontmatter.get("status")
                        != current.frontmatter.get("status")
                        or dependency_ids
                        != list(
                            parse_inline_list(
                                current.frontmatter.get("depends_on", "")
                            )
                            or []
                        )
                    ):
                        raise InputError("running Task cannot be changed")
                    comparable = dict(frontmatter)
                    comparable["updated_at"] = current.frontmatter.get("updated_at", "")
                    changed = comparable != dict(current.frontmatter)
                    if changed:
                        frontmatter["updated_at"] = timestamp
                        effects.append(
                            self._workflow_effect_from_entity(
                                "task_updated", current, frontmatter, current.body
                            )
                        )
                else:
                    changed = True
                    frontmatter = {
                        "id": resolved_id_by_key[key],
                        "type": "task",
                        "title": str(item["title"]),
                        "status": str(item["status"]),
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "project_id": project.entity_id,
                        "project_position": position,
                    }
                    if dependency_ids:
                        frontmatter["depends_on"] = self._inline_list(dependency_ids)
                    effects.append(
                        self._workflow_create_effect(
                            "task_created", frontmatter, ""
                        )
                    )
                encoded_nodes.append(
                    {
                        "key": key,
                        "id": resolved_id_by_key[key],
                        "existing": existing,
                        "base_hash": str(item.get("base_hash", "")),
                        "title": str(item["title"]),
                        "parent_key": parent_key,
                        "status": str(item["status"]),
                        "project_position": position,
                        "secondary_dependency_ids": secondary_ids,
                        "changed": changed,
                    }
                )

            affected_child_ids = frozenset(
                entity.entity_id
                for entity in tree_members
                if self._project_task_primary_parent(dict(entity.frontmatter))
                in archived_ids
            )
            for archive in normalized_archives:
                archive_plan = self._plan_archive_entity_locked(
                    archive["id"],
                    archive["base_hash"],
                    ignored_child_ids=affected_child_ids,
                )
                effects.append(
                    self._workflow_effect_from_mutation_plan(
                        "task_archived", archive_plan
                    )
                )

            if not effects:
                raise InputError("Project Task plan update has no changes")
            operation_inputs = (
                ("action", "project_task_plan_update"),
                ("target_entity_id", project.entity_id),
                ("target_base_hash", project.content_hash),
                (
                    "nodes",
                    json.dumps(
                        encoded_nodes,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
                (
                    "archives",
                    json.dumps(
                        normalized_archives,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            effect_tuple = tuple(effects)
            validation_errors = self._virtual_workflow_validation_errors(
                effect_tuple
            )
            if validation_errors:
                raise SchemaError(list(validation_errors))
            integrity_tag = self._workflow_plan_integrity_tag(
                action="project_task_plan_update",
                target_entity_id=project.entity_id,
                target_base_hash=project.content_hash,
                active_entity_id=None,
                active_base_hash=None,
                resolution=None,
                operation_inputs=operation_inputs,
                effects=effect_tuple,
                validation_errors=validation_errors,
            )
            return WorkflowPlan(
                action="project_task_plan_update",
                target_entity_id=project.entity_id,
                target_base_hash=project.content_hash,
                active_entity_id=None,
                active_base_hash=None,
                resolution=None,
                operation_inputs=operation_inputs,
                effects=effect_tuple,
                validation_errors=validation_errors,
                integrity_tag=integrity_tag,
            )

    @staticmethod
    def _project_task_primary_parent(frontmatter: dict[str, str]) -> str | None:
        dependencies = parse_inline_list(frontmatter.get("depends_on", ""))
        return dependencies[0] if dependencies else None

    def _project_task_tree_members(self, project_id: str) -> tuple[Entity, ...]:
        """Return Tasks with persisted structural evidence of tree membership."""
        project_tasks = tuple(
            entity
            for entity in self.list_entities()
            if entity.entity_type == "task"
            and not entity.relative_path.startswith("archive/")
            and entity.frontmatter.get("project_id") == project_id
        )
        referenced_parent_ids = {
            parent_id
            for entity in project_tasks
            if (
                parent_id := self._project_task_primary_parent(
                    dict(entity.frontmatter)
                )
            )
        }
        return tuple(
            entity
            for entity in project_tasks
            if entity.frontmatter.get("project_position")
            or self._project_task_primary_parent(dict(entity.frontmatter)) is not None
            or entity.entity_id in referenced_parent_ids
        )

    def plan_project_task_move(
        self,
        entity_id: str,
        base_hash: str,
        *,
        project_id: str,
        status: str,
        primary_parent_id: str | None,
        position: int,
    ) -> WorkflowPlan:
        """Preview one same-project, same-status, same-parent sibling reorder."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            target = self._require_workflow_task(entity_id, base_hash)
            if (
                not isinstance(project_id, str)
                or not isinstance(status, str)
                or (primary_parent_id is not None and not isinstance(primary_parent_id, str))
                or type(position) is not int
                or position < 1
                or target.frontmatter.get("project_id") != project_id
                or target.frontmatter.get("status") != status
                or self._project_task_primary_parent(dict(target.frontmatter))
                != primary_parent_id
            ):
                raise InputError("Project Task move boundary is invalid")
            if primary_parent_id is not None:
                parent = self._find_mutation_entity(primary_parent_id)
                if (
                    parent.entity_type != "task"
                    or parent.frontmatter.get("project_id") != project_id
                ):
                    raise InputError("Project Task parent is outside the Project")

            tree_members = self._project_task_tree_members(project_id)
            if target.entity_id not in {
                entity.entity_id for entity in tree_members
            }:
                raise InputError("Task is outside the Project Task tree")
            siblings = [
                entity
                for entity in tree_members
                if entity.frontmatter.get("status") == status
                and self._project_task_primary_parent(dict(entity.frontmatter))
                == primary_parent_id
            ]
            siblings.sort(
                key=lambda entity: (
                    0
                    if PROJECT_TASK_POSITION_PATTERN.fullmatch(
                        entity.frontmatter.get("project_position", "")
                    )
                    else 1,
                    int(entity.frontmatter.get("project_position", "0"))
                    if PROJECT_TASK_POSITION_PATTERN.fullmatch(
                        entity.frontmatter.get("project_position", "")
                    )
                    else 0,
                    entity.entity_id,
                )
            )
            if position > len(siblings):
                raise InputError("Project Task position is outside the sibling group")
            ordered = [entity for entity in siblings if entity.entity_id != target.entity_id]
            ordered.insert(position - 1, target)
            timestamp = current_time().isoformat(timespec="seconds")
            by_id: dict[str, WorkflowEffect] = {}
            for next_position, entity in enumerate(ordered, start=1):
                frontmatter = dict(entity.frontmatter)
                frontmatter["project_position"] = str(next_position)
                frontmatter["updated_at"] = timestamp
                by_id[entity.entity_id] = self._workflow_effect_from_entity(
                    "task_moved" if entity.entity_id == target.entity_id else "task_reordered",
                    entity,
                    frontmatter,
                    entity.body,
                )
            effects = (
                by_id[target.entity_id],
                *(by_id[entity.entity_id] for entity in ordered if entity.entity_id != target.entity_id and entity.entity_id in by_id),
            )
            return self._build_roadmap_workflow_plan(
                "project_task_move",
                target,
                effects,
                {
                    "position": str(position),
                    "primary_parent_id": primary_parent_id or "",
                    "project_id": project_id,
                    "status": status,
                },
            )

    def plan_project_task_plan_migrate(
        self, project_id: str, base_hash: str
    ) -> WorkflowPlan:
        """Preview the exact bounded legacy dependency-only Waiting subset."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            project = self._require_mutation_current(project_id, base_hash)
            if (
                project.entity_type != "project"
                or project.relative_path.startswith("archive/")
                or project.frontmatter.get("status") not in {"not_started", "doing"}
            ):
                raise ProjectOperationError("project_not_executable")
            candidates = sorted(
                (
                    entity
                    for entity in self.list_entities()
                    if entity.entity_type == "task"
                    and not entity.relative_path.startswith("archive/")
                    and entity.frontmatter.get("project_id") == project.entity_id
                    and entity.frontmatter.get("status") == "waiting"
                    and bool(self._task_dependencies(dict(entity.frontmatter)))
                    and not entity.frontmatter.get("waiting_for")
                ),
                key=lambda entity: entity.entity_id,
            )
            if not candidates:
                raise InputError("Project has no legacy dependency-only Waiting Tasks")
            timestamp = current_time().isoformat(timespec="seconds")
            effects: list[WorkflowEffect] = []
            for candidate in candidates:
                frontmatter = dict(candidate.frontmatter)
                frontmatter["status"] = "planned"
                frontmatter["updated_at"] = timestamp
                effects.append(
                    self._workflow_effect_from_entity(
                        "planned_migrated",
                        candidate,
                        frontmatter,
                        candidate.body,
                    )
                )
            return self._build_roadmap_workflow_plan(
                "project_task_plan_migrate", project, tuple(effects), {}
            )

    def plan_task_complete(
        self, entity_id: str, base_hash: str
    ) -> WorkflowPlan:
        """Preview closing the sole current Task as done."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            current = self._require_current_doing_task(
                entity_id, base_hash, planning
            )
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            effect = self._plan_close_effect(
                current, "completed", now, timestamp
            )
            effects = (effect,) + self._dependency_release_effects(
                (effect,), timestamp, planning=planning
            )
            preview = self._prepare_workflow_preview_locked(
                action="complete",
                target=current,
                active=None,
                resolution=None,
                expected_doing_id=current.entity_id,
                timestamp=timestamp,
                effects=effects,
            )
        return self._finalize_workflow_preview(preview)

    def plan_break_timer_completion(
        self,
        entity_id: str,
        base_hash: str,
        *,
        now: datetime.datetime,
    ) -> WorkflowPlan:
        """Preview a due break completion using its stored deadline."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise InputError("break completion time must be timezone-aware")
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            current = self._require_current_doing_task(
                entity_id, base_hash, planning
            )
            if current.frontmatter.get("timer_kind") != _BREAK_TIMER_KIND:
                raise InputError("Task is not a break timer")
            deadline_text = current.frontmatter.get("timer_ends_at", "")
            deadline = self._parsed_timestamp(deadline_text)
            if deadline is None:
                raise InputError("break timer deadline is invalid")
            if now < deadline:
                raise InputError("break timer deadline has not arrived")
            effect = self._plan_close_effect(
                current,
                "completed",
                deadline,
                deadline_text,
            )
            effects = (effect,) + self._dependency_release_effects(
                (effect,), deadline_text, planning=planning
            )
            preview = self._prepare_workflow_preview_locked(
                action="complete",
                target=current,
                active=None,
                resolution=None,
                expected_doing_id=current.entity_id,
                timestamp=deadline_text,
                effects=effects,
            )
        return self._finalize_workflow_preview(preview)

    def plan_task_start_break(
        self,
        *,
        active_entity_id: str | None = None,
        active_base_hash: str | None = None,
        resolution: str | None = None,
    ) -> WorkflowPlan:
        """Preview resolving the current Task and starting one fixed break."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            doing = self._current_doing_task(planning)
            expected_doing_id = "" if doing is None else doing.entity_id
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            effects: list[WorkflowEffect] = []
            if doing is None:
                if any(
                    value is not None
                    for value in (active_entity_id, active_base_hash, resolution)
                ):
                    raise InputError(
                        "unexpected active resolution without a doing Task"
                    )
            else:
                if resolution not in {"complete", "interrupt"}:
                    raise InputError(
                        "starting a break requires resolving the current doing Task"
                    )
                if active_entity_id != doing.entity_id or active_base_hash is None:
                    raise InputError("current doing Task identity and hash are required")
                current = self._require_workflow_task(
                    active_entity_id, active_base_hash, planning
                )
                if current.entity_id != doing.entity_id:
                    raise MutationPlanConflict("current doing Task changed")
                if resolution == "interrupt":
                    effects.extend(
                        self._plan_interrupt_effects(
                            current, now, timestamp, planning
                        )
                    )
                else:
                    if current.frontmatter.get("timer_kind") == _BREAK_TIMER_KIND:
                        raise InputError("break Tasks cannot be interrupted")
                    effects.append(
                        self._plan_close_effect(current, "completed", now, timestamp)
                    )
                    effects.extend(
                        self._dependency_release_effects(
                            tuple(effects), timestamp, planning=planning
                        )
                    )

            entity_id = self.next_entity_id("task", on_date=now.date())
            reserved_ids = {effect.entity_id for effect in effects}
            while entity_id in reserved_ids:
                prefix, separator, suffix = entity_id.rpartition("-")
                next_suffix = int(suffix) + 1 if separator else 1000
                if next_suffix > 999:
                    raise InputError("entity id suffix exhausted for break Task")
                entity_id = f"{prefix}-{next_suffix:03d}"
            deadline = (now + _BREAK_DURATION).isoformat(timespec="seconds")
            frontmatter = {
                "id": entity_id,
                "type": "task",
                "title": _BREAK_TITLE,
                "status": "doing",
                "created_at": timestamp,
                "updated_at": timestamp,
                "work_started_at": timestamp,
                "timer_kind": _BREAK_TIMER_KIND,
                "timer_ends_at": deadline,
                "resume_status": "next",
            }
            created = self._workflow_create_effect("started", frontmatter, "")
            effects.append(created)
            preview = self._prepare_workflow_preview_locked(
                action="start_break",
                target=created.planned_entity,
                active=doing,
                resolution=resolution,
                expected_doing_id=expected_doing_id,
                timestamp=timestamp,
                effects=tuple(effects),
            )
        return self._finalize_workflow_preview(preview)

    def plan_task_interrupt(
        self, entity_id: str, base_hash: str
    ) -> WorkflowPlan:
        """Preview closing the current Task and creating its continuation."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            planning = self._workflow_planning_snapshot_locked()
            current = self._require_current_doing_task(
                entity_id, base_hash, planning
            )
            now = current_time()
            timestamp = now.isoformat(timespec="seconds")
            effects = self._plan_interrupt_effects(
                current, now, timestamp, planning
            )
            preview = self._prepare_workflow_preview_locked(
                action="interrupt",
                target=current,
                active=None,
                resolution=None,
                expected_doing_id=current.entity_id,
                timestamp=timestamp,
                effects=tuple(effects),
            )
        return self._finalize_workflow_preview(preview)

    @staticmethod
    def _effective_project_lane(status: str | None) -> str | None:
        return "not_started" if status == "active" else status

    def _project_lane_sequences(
        self, entities: tuple[Entity, ...] | None = None
    ) -> dict[str, list[Entity]]:
        lanes = {lane: [] for lane in _PROJECT_KANBAN_LANES}
        for entity in self.list_entities() if entities is None else entities:
            lane = self._effective_project_lane(entity.frontmatter.get("status"))
            if (
                entity.entity_type == "project"
                and not entity.relative_path.startswith("archive/")
                and lane in lanes
            ):
                position = entity.frontmatter.get("kanban_position")
                if position and PROJECT_KANBAN_POSITION_PATTERN.fullmatch(position) is None:
                    raise ProjectOperationError("project_position_invalid", 422)
                lanes[lane].append(entity)
        for values in lanes.values():
            values.sort(
                key=lambda entity: (
                    0 if entity.frontmatter.get("kanban_position") else 1,
                    int(entity.frontmatter.get("kanban_position", "1")),
                    entity.relative_path,
                )
            )
        return lanes

    def _project_order_effects(
        self,
        target: Entity,
        status: str,
        position: int,
        timestamp: str,
        *,
        target_role: str,
        target_frontmatter: dict[str, str] | None = None,
        target_body: str | None = None,
    ) -> tuple[WorkflowEffect, ...]:
        lanes = self._project_lane_sequences()
        source_lane = self._effective_project_lane(target.frontmatter.get("status"))
        if source_lane not in lanes or status not in lanes:
            raise ProjectOperationError("project_not_movable")
        lanes[source_lane] = [
            entity for entity in lanes[source_lane]
            if entity.entity_id != target.entity_id
        ]
        maximum = len(lanes[status]) + 1
        if type(position) is not int or not 1 <= position <= maximum:
            raise ProjectOperationError("project_position_invalid", 422)
        lanes[status].insert(position - 1, target)

        changed: dict[str, WorkflowEffect] = {}
        for lane in dict.fromkeys((source_lane, status)):
            for index, current in enumerate(lanes[lane], start=1):
                if current.entity_id == target.entity_id:
                    frontmatter = dict(
                        current.frontmatter
                        if target_frontmatter is None
                        else target_frontmatter
                    )
                    body = current.body if target_body is None else target_body
                    role = target_role
                else:
                    frontmatter = dict(current.frontmatter)
                    body = current.body
                    role = "project_reordered"
                frontmatter["status"] = lane
                frontmatter["kanban_position"] = str(index)
                if (
                    current.entity_id != target.entity_id
                    and frontmatter == current.frontmatter
                ):
                    continue
                frontmatter["updated_at"] = timestamp
                changed[current.entity_id] = self._workflow_effect_from_entity(
                    role, current, frontmatter, body
                )
        if target.entity_id not in changed:
            frontmatter = dict(
                target.frontmatter if target_frontmatter is None else target_frontmatter
            )
            frontmatter["status"] = status
            frontmatter["kanban_position"] = str(position)
            changed[target.entity_id] = self._workflow_effect_from_entity(
                target_role,
                target,
                frontmatter,
                target.body if target_body is None else target_body,
            )
        return tuple(
            changed[entity_id]
            for entity_id in (
                target.entity_id,
                *sorted(set(changed) - {target.entity_id}),
            )
        )

    def plan_project_move(
        self,
        entity_id: str,
        base_hash: str,
        status: str,
        position: int,
    ) -> WorkflowPlan:
        """Preview one exact atomic Project move and lane normalization."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            target = self._require_mutation_current(entity_id, base_hash)
            if (
                target.entity_type != "project"
                or target.relative_path.startswith("archive/")
                or status not in _PROJECT_KANBAN_LANES
                or type(position) is not int
            ):
                raise ProjectOperationError("project_not_movable")
            proposed = dict(target.frontmatter)
            proposed["status"] = status
            self._validate_project_transition(target, proposed)
            timestamp = current_time().isoformat(timespec="seconds")
            effects = self._project_order_effects(
                target,
                status,
                position,
                timestamp,
                target_role="project_moved",
            )
            return self._build_roadmap_workflow_plan(
                "project_move",
                target,
                effects,
                {"position": str(position), "status": status},
            )

    def plan_roadmap_move(
        self,
        entity_id: str,
        base_hash: str,
        lane: str,
        position: int,
    ) -> WorkflowPlan:
        """Preview one exact atomic reorder across the Next/Later lanes."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            target = self._require_mutation_current(entity_id, base_hash)
            if (
                target.entity_type != "roadmap_outcome"
                or target.relative_path.startswith("archive/")
                or target.frontmatter.get("status") != "active"
                or self._outcome_is_now(entity_id)
            ):
                raise RoadmapOperationError("roadmap_outcome_not_movable", 409)
            if lane not in {"next", "later"} or type(position) is not int:
                raise RoadmapOperationError("roadmap_operation_invalid", 400)

            lanes: dict[str, list[Entity]] = {"next": [], "later": []}
            for entity in self.list_entities():
                if (
                    not entity.relative_path.startswith("archive/")
                    and entity.entity_type == "roadmap_outcome"
                    and entity.frontmatter.get("status") == "active"
                    and not self._outcome_is_now(entity.entity_id)
                    and entity.frontmatter.get("roadmap_lane") in lanes
                ):
                    lanes[entity.frontmatter["roadmap_lane"]].append(entity)
            for values in lanes.values():
                values.sort(
                    key=lambda item: (
                        int(item.frontmatter.get("roadmap_position", "0")),
                        item.entity_id,
                    )
                )
            source_lane = target.frontmatter.get("roadmap_lane")
            if source_lane not in lanes:
                raise RoadmapOperationError("roadmap_outcome_not_movable", 409)
            lanes[source_lane] = [item for item in lanes[source_lane] if item.entity_id != entity_id]
            maximum = len(lanes[lane]) + 1
            if not 1 <= position <= maximum:
                raise RoadmapOperationError("roadmap_position_invalid", 422)
            lanes[lane].insert(position - 1, target)

            timestamp = current_time().isoformat(timespec="seconds")
            changed: dict[str, WorkflowEffect] = {}
            for lane_name in ("next", "later"):
                for index, current in enumerate(lanes[lane_name], start=1):
                    if (
                        current.frontmatter.get("roadmap_lane") == lane_name
                        and current.frontmatter.get("roadmap_position") == str(index)
                    ):
                        continue
                    frontmatter = dict(current.frontmatter)
                    frontmatter["roadmap_lane"] = lane_name
                    frontmatter["roadmap_position"] = str(index)
                    frontmatter["updated_at"] = timestamp
                    changed[current.entity_id] = self._workflow_effect_from_entity(
                        "outcome_moved" if current.entity_id == entity_id else "outcome_reordered",
                        current,
                        frontmatter,
                        current.body,
                    )
            effects = tuple(
                changed[changed_id]
                for changed_id in (entity_id, *sorted(set(changed) - {entity_id}))
                if changed_id in changed
            )
            if not effects:
                # A no-op move still binds the current entity into a visible exact effect.
                frontmatter = dict(target.frontmatter)
                effects = (
                    self._workflow_effect_from_entity(
                        "outcome_moved", target, frontmatter, target.body
                    ),
                )
            return self._build_roadmap_workflow_plan(
                "roadmap_move",
                target,
                effects,
                {
                    "lane": lane,
                    "position": str(position),
                },
            )

    def plan_cycle_activate(self, entity_id: str, base_hash: str) -> WorkflowPlan:
        """Preview activation of one planned Cycle and all of its members."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            cycle = self._require_mutation_current(entity_id, base_hash)
            if cycle.entity_type != "cycle" or cycle.frontmatter.get("status") != "planned":
                raise RoadmapOperationError("roadmap_operation_invalid", 400)
            self._validate_roadmap_crud_state(
                dict(cycle.frontmatter), exclude_entity_id=cycle.entity_id
            )
            if any(
                entity.entity_type == "cycle"
                and not entity.relative_path.startswith("archive/")
                and entity.frontmatter.get("status") == "active"
                for entity in self.list_entities()
            ):
                raise RoadmapOperationError("active_cycle_exists", 409)
            member_ids = parse_inline_list(cycle.frontmatter.get("outcome_ids", "")) or []
            members: list[Entity] = []
            for member_id in member_ids:
                try:
                    member = self._find_mutation_entity(member_id)
                except NotFoundError as error:
                    raise RoadmapOperationError("roadmap_operation_invalid", 400) from error
                if member.entity_type != "roadmap_outcome" or member.frontmatter.get("status") != "active":
                    raise RoadmapOperationError("roadmap_operation_invalid", 400)
                success_condition = self._markdown_section(member.body, "## 成功条件")
                if not success_condition:
                    raise RoadmapOperationError("cycle_success_condition_required", 422)
                members.append(member)

            timestamp = current_time().isoformat(timespec="seconds")
            cycle_frontmatter = dict(cycle.frontmatter)
            cycle_frontmatter["status"] = "active"
            cycle_frontmatter["updated_at"] = timestamp
            effects: list[WorkflowEffect] = [
                self._workflow_effect_from_entity(
                    "cycle_activated", cycle, cycle_frontmatter, cycle.body
                )
            ]
            for member in members:
                frontmatter = dict(member.frontmatter)
                frontmatter.pop("roadmap_lane", None)
                frontmatter.pop("roadmap_position", None)
                frontmatter["updated_at"] = timestamp
                effects.append(
                    self._workflow_effect_from_entity(
                        "outcome_now", member, frontmatter, member.body
                    )
                )
            member_set = set(member_ids)
            for lane in ("next", "later"):
                remaining = sorted(
                    (
                        entity for entity in self.list_entities()
                        if entity.entity_type == "roadmap_outcome"
                        and not entity.relative_path.startswith("archive/")
                        and entity.entity_id not in member_set
                        and entity.frontmatter.get("status") == "active"
                        and entity.frontmatter.get("roadmap_lane") == lane
                    ),
                    key=lambda entity: (
                        int(entity.frontmatter.get("roadmap_position", "0")),
                        entity.entity_id,
                    ),
                )
                for position, entity in enumerate(remaining, start=1):
                    if entity.frontmatter.get("roadmap_position") == str(position):
                        continue
                    frontmatter = dict(entity.frontmatter)
                    frontmatter["roadmap_position"] = str(position)
                    frontmatter["updated_at"] = timestamp
                    effects.append(
                        self._workflow_effect_from_entity(
                            "outcome_reordered", entity, frontmatter, entity.body
                        )
                    )
            return self._build_roadmap_workflow_plan(
                "cycle_activate", cycle, tuple(effects), {}
            )

    def plan_roadmap_outcome_bundle_update(
        self,
        entity_id: str,
        base_hash: str,
        fields: dict[str, str],
        body: str | None,
        project_changes: list[dict[str, object]],
    ) -> WorkflowPlan:
        """Preview one exact Outcome update and its explicit Project changes."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            target = self._require_mutation_current(entity_id, base_hash)
            if (
                target.entity_type != "roadmap_outcome"
                or target.relative_path.startswith("archive/")
                or type(project_changes) is not list
            ):
                raise RoadmapOperationError("roadmap_operation_invalid", 400)

            outcome_plan = self._plan_update_entity_locked(
                target.entity_id, base_hash, fields, body
            )
            effects = [
                self._workflow_effect_from_mutation_plan(
                    "outcome_updated", outcome_plan
                )
            ]
            normalized_changes: list[dict[str, object]] = []
            changed_project_ids: set[str] = set()
            reserved_create_ids: set[str] = set()
            for change in project_changes:
                if type(change) is not dict or type(change.get("action")) is not str:
                    raise RoadmapOperationError("roadmap_operation_invalid", 400)
                action = change["action"]
                if action == "create":
                    if set(change) != {"action", "fields", "body"}:
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    project_fields = change["fields"]
                    project_body = change["body"]
                    if (
                        type(project_fields) is not dict
                        or any(
                            not isinstance(key, str) or not isinstance(value, str)
                            for key, value in project_fields.items()
                        )
                        or project_fields.get("roadmap_outcome_id") != target.entity_id
                        or "goal_id" in project_fields
                        or not isinstance(project_body, str)
                    ):
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    project_plan = self._plan_create_entity_locked(
                        "project",
                        dict(project_fields),
                        project_body,
                        None,
                        None,
                        reserved_ids=reserved_create_ids,
                    )
                    reserved_create_ids.add(project_plan.entity_id)
                    effects.append(
                        self._workflow_effect_from_mutation_plan(
                            "project_created", project_plan
                        )
                    )
                    normalized_changes.append(
                        {
                            "action": "create",
                            "fields": dict(project_fields),
                            "body": project_body,
                            "generated_id": project_plan.entity_id,
                        }
                    )
                    continue

                if action in {"update", "move", "archive"}:
                    required = {"action", "id", "base_hash"}
                    optional = (
                        {"fields", "body"}
                        if action == "update"
                        else set()
                        if action == "move"
                        else {"task_cascade"}
                    )
                    if set(change) != required | optional:
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    project_id = change["id"]
                    project_hash = change["base_hash"]
                    if (
                        not isinstance(project_id, str)
                        or not isinstance(project_hash, str)
                        or project_id in changed_project_ids
                    ):
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    current = self._require_mutation_current(project_id, project_hash)
                    if (
                        current.entity_type != "project"
                        or current.relative_path.startswith("archive/")
                        or (
                            action in {"update", "archive"}
                            and current.frontmatter.get("roadmap_outcome_id")
                            != target.entity_id
                        )
                    ):
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    changed_project_ids.add(project_id)
                else:
                    raise RoadmapOperationError("roadmap_operation_invalid", 400)

                if action == "update":
                    project_fields = change["fields"]
                    project_body = change["body"]
                    if (
                        type(project_fields) is not dict
                        or any(
                            not isinstance(key, str) or not isinstance(value, str)
                            for key, value in project_fields.items()
                        )
                        or {"goal_id", "roadmap_outcome_id"} & set(project_fields)
                        or (project_body is not None and not isinstance(project_body, str))
                    ):
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    project_plan = self._plan_update_entity_locked(
                        current.entity_id,
                        project_hash,
                        dict(project_fields),
                        project_body,
                    )
                    effects.append(
                        self._workflow_effect_from_mutation_plan(
                            "project_updated", project_plan
                        )
                    )
                    normalized_changes.append(dict(change))
                elif action == "move":
                    if current.frontmatter.get("roadmap_outcome_id") == target.entity_id:
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    project_plan = self._plan_update_entity_locked(
                        current.entity_id,
                        project_hash,
                        {"roadmap_outcome_id": target.entity_id, "goal_id": ""},
                        None,
                    )
                    effects.append(
                        self._workflow_effect_from_mutation_plan(
                            "project_moved", project_plan
                        )
                    )
                    normalized_changes.append(dict(change))
                else:
                    cascade = change["task_cascade"]
                    if type(cascade) is not list:
                        raise RoadmapOperationError("roadmap_operation_invalid", 400)
                    children = sorted(
                        (
                            entity
                            for entity in self.list_entities()
                            if entity.entity_type == "task"
                            and not entity.relative_path.startswith("archive/")
                            and entity.frontmatter.get("project_id") == current.entity_id
                        ),
                        key=lambda entity: entity.entity_id,
                    )
                    provided: dict[str, str] = {}
                    for item in cascade:
                        if (
                            type(item) is not dict
                            or set(item) != {"id", "base_hash"}
                            or not isinstance(item["id"], str)
                            or not isinstance(item["base_hash"], str)
                            or item["id"] in provided
                        ):
                            raise RoadmapOperationError(
                                "roadmap_project_cascade_invalid", 409
                            )
                        provided[item["id"]] = item["base_hash"]
                    if set(provided) != {child.entity_id for child in children}:
                        raise RoadmapOperationError(
                            "roadmap_project_cascade_invalid", 409
                        )
                    for child in children:
                        child_plan = self._plan_archive_entity_locked(
                            child.entity_id, provided[child.entity_id]
                        )
                        effects.append(
                            self._workflow_effect_from_mutation_plan(
                                "task_archived", child_plan
                            )
                        )
                    project_plan = self._plan_archive_entity_locked(
                        current.entity_id,
                        project_hash,
                        ignored_child_ids=frozenset(provided),
                    )
                    effects.append(
                        self._workflow_effect_from_mutation_plan(
                            "project_archived", project_plan
                        )
                    )
                    normalized_changes.append(
                        {
                            **dict(change),
                            "task_cascade": [
                                {"id": child.entity_id, "base_hash": provided[child.entity_id]}
                                for child in children
                            ],
                        }
                    )

            effects = list(
                self._normalize_project_order_effects(
                    tuple(effects), current_time().isoformat(timespec="seconds")
                )
            )
            bundle = json.dumps(
                {
                    "fields": fields,
                    "body": body,
                    "project_changes": normalized_changes,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return self._build_roadmap_workflow_plan(
                "roadmap_outcome_bundle_update",
                target,
                tuple(effects),
                {"bundle": bundle},
            )

    def _normalize_project_order_effects(
        self,
        effects: tuple[WorkflowEffect, ...],
        timestamp: str,
    ) -> tuple[WorkflowEffect, ...]:
        """Normalize lanes touched by a mixed workflow without duplicating effects."""
        project_effect_indexes = {
            effect.entity_id: index
            for index, effect in enumerate(effects)
            if effect.planned_entity.entity_type == "project"
        }
        if not project_effect_indexes:
            return effects
        touched_lanes: set[str] = set()
        for entity_id, index in project_effect_indexes.items():
            effect = effects[index]
            before_lane = self._effective_project_lane(
                None if effect.before_entity is None else effect.before_entity.frontmatter.get("status")
            )
            after_lane = (
                None
                if effect.destination_relative_path.startswith("archive/")
                else self._effective_project_lane(effect.planned_entity.frontmatter.get("status"))
            )
            if (
                effect.before_entity is None
                or after_lane is None
                or before_lane != after_lane
            ):
                if before_lane in _PROJECT_KANBAN_LANES:
                    touched_lanes.add(before_lane)
                if after_lane in _PROJECT_KANBAN_LANES:
                    touched_lanes.add(after_lane)
        if not touched_lanes:
            return effects

        incoming_order: dict[str, int] = {}
        for entity_id, index in project_effect_indexes.items():
            effect = effects[index]
            before_lane = self._effective_project_lane(
                None
                if effect.before_entity is None
                else effect.before_entity.frontmatter.get("status")
            )
            after_lane = (
                None
                if effect.destination_relative_path.startswith("archive/")
                else self._effective_project_lane(
                    effect.planned_entity.frontmatter.get("status")
                )
            )
            if after_lane in _PROJECT_KANBAN_LANES and (
                effect.before_entity is None or before_lane != after_lane
            ):
                incoming_order[entity_id] = index

        projected: dict[str, Entity] = {
            entity.entity_id: entity
            for entity in self.list_entities()
            if entity.entity_type == "project"
            and not entity.relative_path.startswith("archive/")
            and entity.entity_id not in project_effect_indexes
        }
        for entity_id, index in project_effect_indexes.items():
            effect = effects[index]
            if not effect.destination_relative_path.startswith("archive/"):
                projected[entity_id] = effect.planned_entity

        lanes = {lane: [] for lane in touched_lanes}
        for entity in projected.values():
            lane = self._effective_project_lane(entity.frontmatter.get("status"))
            if lane in lanes:
                lanes[lane].append(entity)

        def project_order_key(entity: Entity) -> tuple[int, int, int, str]:
            if entity.entity_id in incoming_order:
                return (1, incoming_order[entity.entity_id], 0, entity.relative_path)
            position = entity.frontmatter.get("kanban_position")
            return (
                0,
                0 if position else 1,
                int(position or "1"),
                entity.relative_path,
            )

        for values in lanes.values():
            values.sort(key=project_order_key)

        result = list(effects)
        appended: list[WorkflowEffect] = []
        for lane, entities in lanes.items():
            for position, entity in enumerate(entities, start=1):
                if entity.frontmatter.get("kanban_position") == str(position):
                    continue
                frontmatter = dict(entity.frontmatter)
                frontmatter["status"] = lane
                frontmatter["kanban_position"] = str(position)
                frontmatter["updated_at"] = timestamp
                existing_index = project_effect_indexes.get(entity.entity_id)
                if existing_index is None:
                    current = self._find_mutation_entity(entity.entity_id)
                    appended.append(
                        self._workflow_effect_from_entity(
                            "project_reordered", current, frontmatter, current.body
                        )
                    )
                    continue
                existing = result[existing_index]
                if existing.before_entity is None:
                    after_bytes = (
                        serialize_frontmatter(frontmatter, "project")
                        + existing.planned_entity.body
                    ).encode("utf-8")
                    planned = self._entity_from_bytes(
                        existing.destination_relative_path, after_bytes
                    )
                    planned_copy = self._copy_entity(planned)
                    assert planned_copy is not None
                    result[existing_index] = dataclasses.replace(
                        existing,
                        after_bytes=after_bytes,
                        planned_entity=planned_copy,
                    )
                else:
                    result[existing_index] = self._workflow_effect_from_entity(
                        existing.role,
                        existing.before_entity,
                        frontmatter,
                        existing.planned_entity.body,
                    )
        return tuple(
            (*result, *sorted(appended, key=lambda effect: effect.entity_id))
        )

    def plan_cycle_close(
        self,
        entity_id: str,
        base_hash: str,
        *,
        status: str,
        outcome_results: dict[str, str],
        retrospective: str,
        cancellation_reason: str | None = None,
        carryover_cycle_id: str | None = None,
    ) -> WorkflowPlan:
        """Preview one exact Cycle close, dispositions, and optional carryover."""
        with self.mutation_lock():
            self._require_mutations_available_locked()
            self._reject_workflow_symlinks()
            cycle = self._require_mutation_current(entity_id, base_hash)
            if cycle.entity_type != "cycle" or cycle.frontmatter.get("status") != "active":
                raise RoadmapOperationError("roadmap_operation_invalid", 400)
            if status not in {"completed", "cancelled"} or type(outcome_results) is not dict:
                raise RoadmapOperationError("roadmap_operation_invalid", 400)
            if not isinstance(retrospective, str) or not retrospective.strip():
                raise RoadmapOperationError("cycle_retrospective_required", 422)
            if (
                (status == "cancelled" and not (
                    isinstance(cancellation_reason, str) and cancellation_reason.strip()
                ))
                or (status == "completed" and cancellation_reason is not None)
            ):
                raise RoadmapOperationError("cycle_cancellation_reason_required", 422)
            member_ids = parse_inline_list(cycle.frontmatter.get("outcome_ids", "")) or []
            allowed_results = {"achieved", "carried", "next", "later", "dropped"}
            if set(outcome_results) != set(member_ids) or any(
                type(value) is not str or value not in allowed_results
                for value in outcome_results.values()
            ):
                raise RoadmapOperationError("cycle_dispositions_invalid", 422)
            carried_ids = [member_id for member_id in member_ids if outcome_results[member_id] == "carried"]
            if (
                (carried_ids and not (
                    isinstance(carryover_cycle_id, str) and carryover_cycle_id
                ))
                or (not carried_ids and carryover_cycle_id is not None)
            ):
                raise RoadmapOperationError("cycle_dispositions_invalid", 422)

            carryover: Entity | None = None
            if carried_ids:
                try:
                    carryover = self._find_mutation_entity(carryover_cycle_id or "")
                except NotFoundError as error:
                    raise RoadmapOperationError("carryover_cycle_not_found", 404) from error
                existing_members = parse_inline_list(carryover.frontmatter.get("outcome_ids", "")) or []
                prior_carry = any(
                    other.entity_type == "cycle"
                    and other.entity_id != cycle.entity_id
                    and other.frontmatter.get("carryover_cycle_id") == carryover.entity_id
                    and bool(parse_inline_list(other.frontmatter.get("carried_outcome_ids", "")) or [])
                    for other in self.list_entities()
                )
                if (
                    carryover.entity_type != "cycle"
                    or carryover.entity_id == cycle.entity_id
                    or carryover.frontmatter.get("status") != "planned"
                    or prior_carry
                    or set(carried_ids) & set(existing_members)
                    or len(existing_members) + len(carried_ids) > 2
                ):
                    raise RoadmapOperationError("carryover_cycle_invalid", 409)

            members = [self._find_mutation_entity(member_id) for member_id in member_ids]
            for member in members:
                if outcome_results[member.entity_id] in {"achieved", "dropped"}:
                    unfinished = any(
                        project.entity_type == "project"
                        and not project.relative_path.startswith("archive/")
                        and project.frontmatter.get("roadmap_outcome_id") == member.entity_id
                        and project.frontmatter.get("status")
                        in {"active", "not_started", "doing", "on_hold"}
                        for project in self.list_entities()
                    )
                    if unfinished:
                        raise RoadmapOperationError("unfinished_outcome_projects", 409)

            lane_tails = {"next": 0, "later": 0}
            for entity in self.list_entities():
                lane = entity.frontmatter.get("roadmap_lane")
                if entity.entity_type == "roadmap_outcome" and lane in lane_tails:
                    lane_tails[lane] = max(
                        lane_tails[lane], int(entity.frontmatter.get("roadmap_position", "0"))
                    )
            timestamp = current_time().isoformat(timespec="seconds")
            lists = {
                result: [member_id for member_id in member_ids if outcome_results[member_id] == result]
                for result in allowed_results
            }
            cycle_frontmatter = dict(cycle.frontmatter)
            cycle_frontmatter.update(
                {
                    "status": status,
                    "updated_at": timestamp,
                    "achieved_outcome_ids": self._inline_list(lists["achieved"]),
                    "carried_outcome_ids": self._inline_list(lists["carried"]),
                    "next_outcome_ids": self._inline_list(lists["next"]),
                    "later_outcome_ids": self._inline_list(lists["later"]),
                    "dropped_outcome_ids": self._inline_list(lists["dropped"]),
                }
            )
            if carried_ids:
                cycle_frontmatter["carryover_cycle_id"] = carryover_cycle_id or ""
            if status == "cancelled":
                cycle_frontmatter["cancellation_reason"] = (cancellation_reason or "").strip()
            memo = self._markdown_section(cycle.body, "## メモ")
            cycle_body = (
                f"\n## 振り返り\n\n{retrospective.strip()}\n\n## メモ\n"
                + (f"\n{memo}\n" if memo else "")
            )
            effects: list[WorkflowEffect] = [
                self._workflow_effect_from_entity(
                    "cycle_closed", cycle, cycle_frontmatter, cycle_body
                )
            ]
            for member in members:
                result = outcome_results[member.entity_id]
                frontmatter = dict(member.frontmatter)
                if result in {"achieved", "dropped"}:
                    frontmatter["status"] = result
                    frontmatter.pop("roadmap_lane", None)
                    frontmatter.pop("roadmap_position", None)
                else:
                    lane = "next" if result == "carried" else result
                    lane_tails[lane] += 1
                    frontmatter["roadmap_lane"] = lane
                    frontmatter["roadmap_position"] = str(lane_tails[lane])
                frontmatter["updated_at"] = timestamp
                effects.append(
                    self._workflow_effect_from_entity(
                        f"outcome_{result}", member, frontmatter, member.body
                    )
                )
            if carryover is not None:
                existing = parse_inline_list(carryover.frontmatter.get("outcome_ids", "")) or []
                frontmatter = dict(carryover.frontmatter)
                frontmatter["outcome_ids"] = self._inline_list([*existing, *carried_ids])
                frontmatter["updated_at"] = timestamp
                effects.append(
                    self._workflow_effect_from_entity(
                        "carryover_cycle_updated", carryover, frontmatter, carryover.body
                    )
                )
            return self._build_roadmap_workflow_plan(
                "cycle_close",
                cycle,
                tuple(effects),
                {
                    "status": status,
                    "outcome_results": "|".join(
                        f"{member_id}:{outcome_results[member_id]}" for member_id in member_ids
                    ),
                    "retrospective": retrospective,
                    "cancellation_reason": cancellation_reason or "",
                    "carryover_cycle_id": carryover_cycle_id or "",
                },
            )

    @staticmethod
    def _inline_list(values: list[str]) -> str:
        return "[" + ", ".join(values) + "]"

    @staticmethod
    def _markdown_section(body: str, heading: str) -> str:
        lines = body.splitlines()
        try:
            start = lines.index(heading) + 1
        except ValueError:
            return ""
        section: list[str] = []
        for line in lines[start:]:
            if line.startswith("## "):
                break
            section.append(line)
        return "\n".join(section).strip()

    def _build_roadmap_workflow_plan(
        self,
        action: str,
        target: Entity,
        effects: tuple[WorkflowEffect, ...],
        extra_inputs: dict[str, str],
    ) -> WorkflowPlan:
        operation_inputs = (
            ("action", action),
            ("target_entity_id", target.entity_id),
            ("target_base_hash", target.content_hash),
            *((key, value) for key, value in sorted(extra_inputs.items())),
        )
        validation_errors = self._virtual_workflow_validation_errors(effects)
        if validation_errors:
            raise SchemaError(list(validation_errors))
        integrity_tag = self._workflow_plan_integrity_tag(
            action=action,
            target_entity_id=target.entity_id,
            target_base_hash=target.content_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=operation_inputs,
            effects=effects,
            validation_errors=validation_errors,
        )
        return WorkflowPlan(
            action=action,
            target_entity_id=target.entity_id,
            target_base_hash=target.content_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=operation_inputs,
            effects=effects,
            validation_errors=validation_errors,
            integrity_tag=integrity_tag,
        )

    def apply_task_workflow(
        self, plan: WorkflowPlan
    ) -> tuple[Entity, ...]:
        """Atomically apply every exact effect in one still-current workflow."""
        with self.mutation_lock():
            return self._apply_task_workflow_with_recovery_locked(plan)

    def _apply_task_workflow_with_recovery_locked(
        self, plan: WorkflowPlan
    ) -> tuple[Entity, ...]:
        """Apply an atomic workflow while the caller holds the mutation lock."""
        self._arm_workflow_recovery_state_locked()
        try:
            results = self._apply_task_workflow_locked(plan)
        except (WorkflowPostCommitCleanupError, WorkflowRollbackError):
            # These outcomes require operator inspection. The armed state
            # intentionally survives process and server restarts.
            raise
        except BaseException as primary_error:
            try:
                self._transition_recovery_state_locked("armed", "idle")
            except BaseException as recovery_error:
                raise MutationRecoveryRequired(
                    "mutation recovery is required"
                ) from primary_error
            raise
        try:
            self._transition_recovery_state_locked("armed", "idle")
        except BaseException as transition_error:
            raise WorkflowPostCommitCleanupError(
                tuple(results),
                (transition_error,),
                (),
            ) from transition_error
        return results

    def _require_workflow_task(
        self,
        entity_id: str,
        base_hash: str,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> Entity:
        if planning is None:
            current = self._require_mutation_current(entity_id, base_hash)
        else:
            current = planning.find(entity_id)
            if current.content_hash != base_hash:
                raise ConflictError(current)
        if current.entity_type != "task" or current.relative_path.startswith("archive/"):
            raise InputError("Task workflow requires a non-archived Task")
        return current

    def _task_dependencies(
        self,
        frontmatter: dict[str, str],
        *,
        allowed_projected_ids: set[str] | None = None,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> tuple[str, ...]:
        raw = frontmatter.get("depends_on")
        if raw is None:
            return ()
        dependencies = parse_inline_list(raw)
        if dependencies is None:
            raise InputError("depends_on must be an inline list")
        if len(dependencies) != len(set(dependencies)):
            raise InputError("depends_on must not contain duplicate IDs")
        if frontmatter.get("id") in dependencies:
            raise InputError("depends_on must not reference its own Task")
        projected_ids = (
            set() if allowed_projected_ids is None else allowed_projected_ids
        )
        for dependency_id in dependencies:
            if dependency_id in projected_ids:
                continue
            try:
                dependency = (
                    self._find_mutation_entity(dependency_id)
                    if planning is None
                    else planning.find(dependency_id)
                )
            except NotFoundError as error:
                raise InputError(f"unknown depends_on Task: {dependency_id}") from error
            if dependency.entity_type != "task":
                raise InputError(f"depends_on is not a Task: {dependency_id}")
            if (
                dependency.frontmatter.get("title") == _BREAK_TITLE
                or dependency.frontmatter.get("timer_kind") == _BREAK_TIMER_KIND
            ):
                raise InputError("break Tasks cannot be dependencies")
            if (
                dependency.relative_path.startswith("archive/")
                and dependency.frontmatter.get("status") != "done"
            ):
                raise InputError("archived unfinished Tasks cannot be dependencies")
        return tuple(dependencies)

    def _dependencies_satisfied(
        self,
        frontmatter: dict[str, str],
        projected_statuses: dict[str, str] | None = None,
        *,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> bool:
        projected = {} if projected_statuses is None else projected_statuses
        for dependency_id in self._task_dependencies(
            frontmatter,
            allowed_projected_ids=set(projected),
            planning=planning,
        ):
            if dependency_id in projected:
                status = projected[dependency_id]
            else:
                dependency = (
                    self._find_mutation_entity(dependency_id)
                    if planning is None
                    else planning.find(dependency_id)
                )
                status = dependency.frontmatter.get("status", "")
            if status != "done":
                return False
        return True

    @classmethod
    def _task_available_from(
        cls, frontmatter: dict[str, str]
    ) -> datetime.date | datetime.datetime | None:
        value = frontmatter.get("available_from", "")
        if not value:
            return None
        if DATE_PATTERN.fullmatch(value) is not None:
            try:
                return datetime.date.fromisoformat(value)
            except ValueError as error:
                raise InputError(f"invalid available_from: {value}") from error
        parsed = cls._parsed_timestamp(value)
        if parsed is None:
            raise InputError(f"invalid available_from: {value}")
        return parsed

    @classmethod
    def _task_available_from_reached(
        cls, frontmatter: dict[str, str], now: datetime.datetime
    ) -> bool:
        if not isinstance(now, datetime.datetime) or now.utcoffset() is None:
            raise InputError("now must be a timezone-aware datetime")
        available_from = cls._task_available_from(frontmatter)
        if available_from is None:
            return True
        if isinstance(available_from, datetime.datetime):
            threshold = available_from
        else:
            threshold = datetime.datetime.combine(
                available_from, datetime.time(), _MOKVIA_LOCAL_TIMEZONE
            )
        return threshold <= now

    def _task_has_automatic_gate(
        self,
        frontmatter: dict[str, str],
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> bool:
        return (
            self._task_available_from(frontmatter) is not None
            or bool(self._task_dependencies(frontmatter, planning=planning))
        )

    def _task_release_gates_satisfied(
        self,
        frontmatter: dict[str, str],
        now: datetime.datetime,
        projected_statuses: dict[str, str] | None = None,
        *,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> bool:
        return (
            self._task_available_from_reached(frontmatter, now)
            and self._dependencies_satisfied(
                frontmatter, projected_statuses, planning=planning
            )
            and not frontmatter.get("waiting_for")
        )

    def _waiting_release_effect(
        self, role: str, candidate: Entity, timestamp: str
    ) -> WorkflowEffect:
        frontmatter = dict(candidate.frontmatter)
        frontmatter["status"] = "next"
        frontmatter["updated_at"] = timestamp
        return self._workflow_effect_from_entity(
            role, candidate, frontmatter, candidate.body
        )

    def _dependency_release_effects(
        self,
        effects: tuple[WorkflowEffect, ...],
        timestamp: str,
        *,
        exclude_ids: set[str] | None = None,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> tuple[WorkflowEffect, ...]:
        excluded = set() if exclude_ids is None else set(exclude_ids)
        excluded.update(effect.entity_id for effect in effects)
        projected_statuses = {
            effect.entity_id: effect.planned_entity.frontmatter.get("status", "")
            for effect in effects
        }
        candidates = sorted(
            (
                entity
                for entity in (
                    self.list_entities()
                    if planning is None
                    else planning.entities
                )
                if entity.entity_type == "task"
                and not entity.relative_path.startswith("archive/")
                and entity.entity_id not in excluded
                and entity.frontmatter.get("status") in {"waiting", "planned"}
                and bool(
                    self._task_dependencies(
                        dict(entity.frontmatter), planning=planning
                    )
                )
            ),
            key=lambda entity: entity.entity_id,
        )
        released: list[WorkflowEffect] = []
        now = datetime.datetime.fromisoformat(timestamp)
        for candidate in candidates:
            if not self._task_release_gates_satisfied(
                dict(candidate.frontmatter),
                now,
                projected_statuses,
                planning=planning,
            ):
                continue
            released.append(
                self._waiting_release_effect(
                    "dependency_released", candidate, timestamp
                )
            )
        return tuple(released)

    def _dependency_retarget_effects(
        self,
        source_id: str,
        continuation_id: str,
        timestamp: str,
        *,
        exclude_ids: set[str] | None = None,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> tuple[WorkflowEffect, ...]:
        excluded = set() if exclude_ids is None else set(exclude_ids)
        retargeted: list[WorkflowEffect] = []
        candidates = sorted(
            (
                entity
                for entity in (
                    self.list_entities()
                    if planning is None
                    else planning.entities
                )
                if entity.entity_type == "task"
                and not entity.relative_path.startswith("archive/")
                and entity.entity_id not in excluded
            ),
            key=lambda entity: entity.entity_id,
        )
        for candidate in candidates:
            dependencies = self._task_dependencies(
                dict(candidate.frontmatter), planning=planning
            )
            if source_id not in dependencies:
                continue
            frontmatter = dict(candidate.frontmatter)
            frontmatter["depends_on"] = "[" + ", ".join(
                continuation_id if item == source_id else item
                for item in dependencies
            ) + "]"
            frontmatter["updated_at"] = timestamp
            retargeted.append(
                self._workflow_effect_from_entity(
                    "dependency_retargeted",
                    candidate,
                    frontmatter,
                    candidate.body,
                )
            )
        return tuple(retargeted)

    def _current_doing_task(
        self, planning: WorkflowPlanningSnapshot | None = None
    ) -> Entity | None:
        doing: list[Entity] = []
        if planning is not None:
            doing = [
                entity
                for entity in planning.entities
                if entity.entity_type == "task"
                and not entity.relative_path.startswith("archive/")
                and entity.frontmatter.get("status") == "doing"
            ]
            if len(doing) > 1:
                raise InputError("multiple non-archived doing Tasks")
            return doing[0] if doing else None
        for directory in ("inbox", "tasks"):
            scan_root = self._root / directory
            if scan_root.is_symlink() or not scan_root.is_dir():
                continue
            for path in self._walk_directory(scan_root):
                try:
                    entity = self._read_entity(path)
                except InputError:
                    continue
                if (
                    entity is not None
                    and entity.entity_type == "task"
                    and entity.frontmatter.get("status") == "doing"
                ):
                    doing.append(entity)
        if len(doing) > 1:
            raise InputError("multiple non-archived doing Tasks")
        return doing[0] if doing else None

    def _reject_workflow_symlinks(self) -> None:
        """Reject symlinks in ordinary entity roots without reading targets."""

        def reject_below(directory: pathlib.Path) -> None:
            try:
                children = sorted(directory.iterdir(), key=lambda path: path.name)
            except OSError as error:
                raise InputError(
                    f"cannot scan workflow repository directory: {directory}"
                ) from error
            for child in children:
                if child.is_symlink():
                    raise InputError(
                        "Task workflow does not allow symlinks in entity roots"
                    )
                if child.is_dir():
                    reject_below(child)

        for directory in _SCAN_DIRECTORIES:
            scan_root = self._root / directory
            if scan_root.is_symlink():
                raise InputError(
                    "Task workflow does not allow symlink entity roots"
                )
            if scan_root.is_dir():
                reject_below(scan_root)

    def _require_current_doing_task(
        self,
        entity_id: str,
        base_hash: str,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> Entity:
        current = self._require_workflow_task(
            entity_id, base_hash, planning
        )
        doing = self._current_doing_task(planning)
        if doing is None or doing.entity_id != current.entity_id:
            raise MutationPlanConflict("Task is not the current doing Task")
        return current

    def _plan_start_effect(
        self,
        target: Entity,
        timestamp: str,
        *,
        projected: Entity | None = None,
        work_started_at: str | None = None,
    ) -> WorkflowEffect:
        source_state = target if projected is None else projected
        prior_status = source_state.frontmatter.get("status")
        frontmatter = dict(source_state.frontmatter)
        body = source_state.body
        calendar_linked = self._require_calendar_lifecycle_state(frontmatter)
        frontmatter["status"] = "doing"
        frontmatter["updated_at"] = timestamp
        event_timestamp = work_started_at or timestamp
        frontmatter["work_started_at"] = event_timestamp
        frontmatter.pop("work_ended_at", None)
        if calendar_linked:
            existing_started_at = frontmatter.get("started_at", "")
            existing_work_started_at = target.frontmatter.get("work_started_at", "")
            if (
                existing_started_at
                and existing_work_started_at
                and existing_started_at != existing_work_started_at
            ):
                raise InputError("started_at does not match work_started_at")
            frontmatter["started_at"] = event_timestamp
            frontmatter["calendar_event_kind"] = "timed"
        if prior_status == "scheduled":
            frontmatter["resume_status"] = "scheduled"
        else:
            frontmatter["resume_status"] = "next"
            for key in ("waiting_for", "scheduled_start", "scheduled_end"):
                frontmatter.pop(key, None)
        return self._workflow_effect_from_entity(
            "started", target, frontmatter, body
        )

    def _linked_project_start_effects(
        self,
        target: Entity,
        timestamp: str,
        *,
        planning: WorkflowPlanningSnapshot | None = None,
    ) -> tuple[WorkflowEffect, ...]:
        project_id = target.frontmatter.get("project_id")
        if not project_id:
            return ()
        try:
            project = (
                self._find_mutation_entity(project_id)
                if planning is None
                else planning.find(project_id)
            )
        except NotFoundError as error:
            raise ProjectOperationError("project_not_executable") from error
        if (
            project.entity_type != "project"
            or project.relative_path.startswith("archive/")
        ):
            raise ProjectOperationError("project_not_executable")
        status = project.frontmatter.get("status")
        if status == "doing":
            return ()
        if status not in {"not_started", "active"}:
            raise ProjectOperationError("project_not_executable")
        frontmatter = dict(project.frontmatter)
        frontmatter["status"] = "doing"
        frontmatter["updated_at"] = timestamp
        destination = [
            entity
            for entity in self._project_lane_sequences(
                None if planning is None else planning.entities
            )["doing"]
            if entity.entity_id != project.entity_id
        ]
        return self._project_order_effects(
            project,
            "doing",
            len(destination) + 1,
            timestamp,
            target_role="project_started",
            target_frontmatter=frontmatter,
            target_body=project.body,
        )

    def _plan_close_effect(
        self,
        current: Entity,
        role: str,
        now: datetime.datetime,
        timestamp: str,
        *,
        work_ended_at: str | None = None,
    ) -> WorkflowEffect:
        if current.frontmatter.get("status") != "doing":
            raise MutationPlanConflict("Task is not doing")
        frontmatter = dict(current.frontmatter)
        calendar_linked = self._require_calendar_lifecycle_state(frontmatter)
        work_started_at = frontmatter.get("work_started_at", "")
        event_time = now
        event_timestamp = timestamp
        if work_ended_at is not None:
            parsed_event = self._parsed_work_session_timestamp(work_ended_at)
            if parsed_event is None:
                raise InputError("invalid work_ended_at")
            if parsed_event > now:
                raise InputError("work_ended_at is in the future")
            if not work_started_at:
                raise InputError("work_ended_at requires work_started_at")
            event_time = parsed_event
            event_timestamp = work_ended_at
        if work_started_at:
            parsed_start = self._parsed_timestamp(work_started_at)
            if parsed_start is None:
                raise InputError("invalid work_started_at")
            if event_time < parsed_start:
                raise InputError("work session clock moved before work_started_at")
            if calendar_linked:
                started_at = frontmatter.get("started_at", "")
                if started_at and started_at != work_started_at:
                    raise InputError("started_at does not match work_started_at")
                if event_time <= parsed_start:
                    raise InputError("Calendar completion must be after started_at")
                frontmatter["started_at"] = work_started_at
                frontmatter["calendar_event_kind"] = "timed"
            frontmatter["work_ended_at"] = event_timestamp
        elif calendar_linked and frontmatter.get("started_at"):
            raise InputError("started_at requires work_started_at in Web workflow")
        if calendar_linked:
            frontmatter["completed_at"] = event_timestamp
        frontmatter["status"] = "done"
        frontmatter["updated_at"] = timestamp
        frontmatter.pop("resume_status", None)
        return self._workflow_effect_from_entity(
            role, current, frontmatter, current.body
        )

    @staticmethod
    def _require_calendar_lifecycle_state(
        frontmatter: dict[str, str],
    ) -> bool:
        identity = tuple(frontmatter.get(key, "") for key in CALENDAR_IDENTITY_KEYS)
        metadata_present = any(identity) or any(
            frontmatter.get(key, "")
            for key in ("started_at", "completed_at")
        )
        if not metadata_present:
            return False
        if not all(identity):
            raise InputError("complete Calendar identity is required")
        if frontmatter.get("calendar_id") != DONE_CALENDAR_ID:
            raise InputError("Task must belong to the approved Calendar")
        calendar_errors = [
            error
            for error in _validate_values(frontmatter, EntityContract("task"))
            if any(
                field in error
                for field in (
                    "calendar_",
                    "Calendar identity",
                    "started_at",
                    "completed_at",
                )
            )
        ]
        if calendar_errors:
            raise InputError("; ".join(calendar_errors))
        return True

    def _plan_interrupt_effects(
        self,
        current: Entity,
        now: datetime.datetime,
        timestamp: str,
        planning: WorkflowPlanningSnapshot | None = None,
        *,
        work_ended_at: str | None = None,
    ) -> list[WorkflowEffect]:
        if current.frontmatter.get("timer_kind") == _BREAK_TIMER_KIND:
            raise InputError("break Tasks cannot be interrupted")
        closed = self._plan_close_effect(
            current,
            "interrupted",
            now,
            timestamp,
            work_ended_at=work_ended_at,
        )
        resume_status = current.frontmatter.get("resume_status")
        if resume_status not in {"next", "scheduled"}:
            resume_status = "next"
        continuation_id = self.next_entity_id("task", on_date=now.date())
        frontmatter = {
            "id": continuation_id,
            "type": "task",
            "title": current.frontmatter["title"],
            "status": resume_status,
            "created_at": timestamp,
            "updated_at": timestamp,
            "action_date": now.astimezone(_MOKVIA_LOCAL_TIMEZONE).date().isoformat(),
        }
        for key in (
            "project_id", "area_id", "due", "contexts", "estimated_minutes",
            "depends_on",
        ):
            value = current.frontmatter.get(key)
            if value:
                frontmatter[key] = value
        if resume_status == "scheduled":
            for key in ("scheduled_start", "scheduled_end"):
                value = current.frontmatter.get(key)
                if value:
                    frontmatter[key] = value
        frontmatter["continuation_of"] = current.entity_id
        continuation_body = _remove_valid_focus_monitor_pause_markers(current.body)
        continuation = self._workflow_create_effect(
            "continuation", frontmatter, continuation_body
        )
        retargeted = self._dependency_retarget_effects(
            current.entity_id,
            continuation.entity_id,
            timestamp,
            exclude_ids={current.entity_id, continuation.entity_id},
            planning=planning,
        )
        return [closed, continuation, *retargeted]

    def _workflow_effect_from_entity(
        self,
        role: str,
        current: Entity,
        frontmatter: dict[str, str],
        body: str,
        *,
        preserve_path: bool = False,
    ) -> WorkflowEffect:
        entity_type = current.entity_type
        self._validate_complete_frontmatter(frontmatter, entity_type)
        self._validate_links(frontmatter, entity_type)
        source = self._root / current.relative_path
        before_bytes, source_token = self._read_regular_file_with_token(source)
        if hashlib.sha256(before_bytes).hexdigest() != current.content_hash:
            raise ConflictError(self._find_mutation_entity(current.entity_id))
        destination = source if preserve_path else self._canonical_destination(frontmatter)
        if destination != source and (destination.exists() or destination.is_symlink()):
            raise DestinationConflict("planned workflow destination is unavailable")
        after_bytes = (serialize_frontmatter(frontmatter, entity_type) + body).encode(
            "utf-8"
        )
        destination_token = None
        if destination.exists() and destination == source:
            destination_token = source_token
        planned = self._entity_from_bytes(
            destination.relative_to(self._root).as_posix(), after_bytes
        )
        before_copy = self._copy_entity(current)
        planned_copy = self._copy_entity(planned)
        assert before_copy is not None and planned_copy is not None
        return WorkflowEffect(
            role=role,
            entity_id=current.entity_id,
            source_relative_path=current.relative_path,
            destination_relative_path=planned.relative_path,
            base_hash=current.content_hash,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            before_entity=before_copy,
            planned_entity=planned_copy,
            source_token=source_token,
            destination_token=destination_token,
        )

    def _workflow_create_effect(
        self, role: str, frontmatter: dict[str, str], body: str
    ) -> WorkflowEffect:
        self._validate_complete_frontmatter(frontmatter, "task")
        destination = self._canonical_destination(frontmatter)
        if destination.exists() or destination.is_symlink():
            raise DestinationConflict("planned workflow destination is unavailable")
        after_bytes = (serialize_frontmatter(frontmatter, "task") + body).encode(
            "utf-8"
        )
        planned = self._entity_from_bytes(
            destination.relative_to(self._root).as_posix(), after_bytes
        )
        planned_copy = self._copy_entity(planned)
        assert planned_copy is not None
        return WorkflowEffect(
            role=role,
            entity_id=planned.entity_id,
            source_relative_path=None,
            destination_relative_path=planned.relative_path,
            base_hash=None,
            before_bytes=None,
            after_bytes=after_bytes,
            before_entity=None,
            planned_entity=planned_copy,
            source_token=None,
            destination_token=None,
        )

    def _workflow_effect_from_mutation_plan(
        self, role: str, plan: MutationPlan
    ) -> WorkflowEffect:
        source_token: PublicationToken | None = None
        destination_token: PublicationToken | None = None
        if plan.source_relative_path is not None:
            source = self._root / plan.source_relative_path
            source_bytes, source_token = self._read_regular_file_with_token(source)
            if source_bytes != plan.before_bytes:
                raise ConflictError(self._find_mutation_entity(plan.entity_id))
            if plan.destination_relative_path == plan.source_relative_path:
                destination_token = source_token
        return WorkflowEffect(
            role=role,
            entity_id=plan.entity_id,
            source_relative_path=plan.source_relative_path,
            destination_relative_path=plan.destination_relative_path,
            base_hash=plan.base_hash,
            before_bytes=plan.before_bytes,
            after_bytes=plan.after_bytes,
            before_entity=plan.before_entity,
            planned_entity=plan.planned_entity,
            source_token=source_token,
            destination_token=destination_token,
        )

    def _prepare_workflow_preview_locked(
        self,
        *,
        action: str,
        target: Entity,
        active: Entity | None,
        resolution: str | None,
        expected_doing_id: str,
        timestamp: str,
        effects: tuple[WorkflowEffect, ...],
        work_event_timestamp: str | None = None,
    ) -> WorkflowPlan:
        plan = self._build_unvalidated_workflow_plan(
            action=action,
            target=target,
            active=active,
            resolution=resolution,
            expected_doing_id=expected_doing_id,
            timestamp=timestamp,
            effects=effects,
            work_event_timestamp=work_event_timestamp,
        )
        if action in _FAST_TASK_WORKFLOW_ACTIONS:
            with self._repository_cache_lock:
                self._fast_workflow_source_generations[plan.integrity_tag] = (
                    self._repository_cache_generation
                )
                while len(self._fast_workflow_source_generations) > 256:
                    self._fast_workflow_source_generations.pop(
                        next(iter(self._fast_workflow_source_generations))
                    )
        return plan

    def _finalize_workflow_preview(
        self,
        plan: WorkflowPlan,
    ) -> WorkflowPlan:
        if plan.action in _FAST_TASK_WORKFLOW_ACTIONS:
            with self._repository_cache_lock:
                prepared_generation = (
                    self._fast_workflow_source_generations.get(
                        plan.integrity_tag
                    )
                )
                if prepared_generation == self._repository_cache_generation:
                    documents = {
                        relative_path: cached.raw_bytes
                        for relative_path, cached in sorted(
                            self._repository_cache.items()
                        )
                    }
                    generation = self._repository_cache_generation
                    planning = self._cached_workflow_planning_snapshot()
                else:
                    documents = None
            if documents is None:
                documents, generation = self._repository_document_snapshot()
                planning = self._cached_workflow_planning_snapshot()
            before_errors = set(
                self._validation_errors_for_document_snapshot(
                    documents, generation
                )
            )
            projected = self._project_workflow_document_map(
                documents, plan.effects
            )
            projected_errors = frozenset(
                validate_document_map(projected)
                if before_errors
                else self._fast_task_projection_validation_errors(
                    planning, plan.effects
                )
            )
            validation_errors = tuple(
                sorted(projected_errors - before_errors)
            )
            if validation_errors:
                raise SchemaError(list(validation_errors))
            with self._repository_cache_lock:
                self._fast_workflow_validations[plan.integrity_tag] = (
                    generation,
                    projected_errors,
                )
                while len(self._fast_workflow_validations) > 256:
                    self._fast_workflow_validations.pop(
                        next(iter(self._fast_workflow_validations))
                    )
            return plan
        validation_source = self._capture_workflow_validation_source()
        with tempfile.TemporaryDirectory(
            prefix="mokvia-workflow-validation-source-"
        ) as temporary:
            validation_root = pathlib.Path(temporary)
            self._materialize_workflow_validation_source(
                validation_source, validation_root
            )
            validation_errors = self._virtual_workflow_validation_errors(
                plan.effects,
                source_root=validation_root,
            )
        if validation_errors:
            raise SchemaError(list(validation_errors))
        return plan

    @staticmethod
    def _project_workflow_document_map(
        documents: Mapping[str, bytes],
        effects: tuple[WorkflowEffect, ...],
    ) -> dict[str, bytes]:
        """Project exact workflow effects over one immutable byte snapshot."""
        projected = dict(documents)
        for effect in effects:
            source = effect.source_relative_path
            if source is None:
                continue
            source_bytes = projected.get(source)
            if source_bytes is None:
                raise MutationPlanConflict(
                    "planned workflow source is unavailable"
                )
            if source_bytes != effect.before_bytes:
                raise MutationPlanConflict(
                    "planned workflow source changed"
                )
            del projected[source]
        for effect in effects:
            destination = effect.destination_relative_path
            if destination in projected:
                raise DestinationConflict(
                    "planned workflow destination is unavailable"
                )
            projected[destination] = effect.after_bytes
        return projected

    def _fast_task_projection_validation_errors(
        self,
        planning: WorkflowPlanningSnapshot,
        effects: tuple[WorkflowEffect, ...],
    ) -> tuple[str, ...]:
        """Validate bounded Task effects over an authoritative clean snapshot.

        The full validator has already established every unchanged document and
        repository-wide invariant for this cache generation. Fast Task actions
        can change only Task documents and Project status/order effects, so the
        remaining checks operate on parsed cached entities plus the exact effect
        documents. Callers must use the full validator when the baseline is not
        clean or its generation is no longer authoritative.
        """
        errors: list[str] = []
        source_paths = {
            effect.source_relative_path
            for effect in effects
            if effect.source_relative_path is not None
        }
        if len(source_paths) != sum(
            effect.source_relative_path is not None for effect in effects
        ):
            return ("workflow effects contain duplicate source paths",)

        projected_by_path = {
            entity.relative_path: entity
            for entity in planning.entities
            if entity.relative_path not in source_paths
        }
        for effect in effects:
            errors.extend(
                self._preflight_document(
                    effect.destination_relative_path, effect.after_bytes
                )
            )
            if effect.destination_relative_path in projected_by_path:
                errors.append(
                    f"{effect.destination_relative_path}: duplicate workflow destination"
                )
                continue
            projected_by_path[effect.destination_relative_path] = (
                effect.planned_entity
            )

        projected = tuple(projected_by_path.values())
        entities_by_id: dict[str, list[Entity]] = {}
        for entity in projected:
            entities_by_id.setdefault(entity.entity_id, []).append(entity)
        for entity_id, matches in entities_by_id.items():
            if len(matches) > 1:
                errors.append(f"duplicate id: {entity_id}")

        doing = [
            entity
            for entity in projected
            if entity.entity_type == "task"
            and not entity.relative_path.startswith("archive/")
            and entity.frontmatter.get("status") == "doing"
        ]
        if len(doing) > 1:
            errors.append(
                f"{doing[1].relative_path}: multiple non-archived doing Tasks; "
                f"first seen in {doing[0].relative_path}"
            )

        for entity in projected:
            frontmatter = entity.frontmatter
            archived = entity.relative_path.startswith("archive/")
            for field, target_type, target_label in REFERENCE_CONTRACTS.get(
                entity.entity_type, ()
            ):
                target_id = frontmatter.get(field, "")
                if not target_id:
                    continue
                targets = entities_by_id.get(target_id, [])
                if not targets:
                    errors.append(
                        f"{entity.relative_path}: {field} references missing "
                        f"{target_label}: {target_id}"
                    )
                    continue
                if len(targets) != 1 or targets[0].entity_type != target_type:
                    errors.append(
                        f"{entity.relative_path}: {field} references non-"
                        f"{target_label} entity: {target_id}"
                    )
                    continue
                target = targets[0]
                if target.relative_path.startswith("archive/") and not archived:
                    errors.append(
                        f"{entity.relative_path}: {field} references archived "
                        f"{target_label}: {target_id}"
                    )
                if (
                    entity.entity_type == "task"
                    and not archived
                    and field == "project_id"
                    and frontmatter.get("status") != "done"
                    and target.frontmatter.get("status") in {"completed", "dropped"}
                ):
                    errors.append(
                        f"{entity.relative_path}: project_id is not executable: "
                        f"{target_id}"
                    )
                if (
                    entity.entity_type == "task"
                    and not archived
                    and field == "project_id"
                    and frontmatter.get("status") == "doing"
                    and target.frontmatter.get("status") != "doing"
                ):
                    errors.append(
                        f"{entity.relative_path}: doing Task Project is not doing: "
                        f"{target_id}"
                    )

            if entity.entity_type != "task":
                continue
            continuation_of = frontmatter.get("continuation_of", "")
            if continuation_of:
                targets = entities_by_id.get(continuation_of, [])
                if not targets:
                    errors.append(
                        f"{entity.relative_path}: continuation_of references "
                        f"missing Task: {continuation_of}"
                    )
                elif len(targets) != 1 or targets[0].entity_type != "task":
                    errors.append(
                        f"{entity.relative_path}: continuation_of reference is "
                        f"invalid: {continuation_of}"
                    )
                elif targets[0].frontmatter.get("status") != "done":
                    errors.append(
                        f"{entity.relative_path}: continuation_of references "
                        f"Task that is not done: {continuation_of}"
                    )
            for dependency_id in (
                parse_inline_list(frontmatter.get("depends_on", "")) or []
            ):
                targets = entities_by_id.get(dependency_id, [])
                if not targets:
                    errors.append(
                        f"{entity.relative_path}: depends_on references missing "
                        f"Task: {dependency_id}"
                    )
                    continue
                if len(targets) != 1 or targets[0].entity_type != "task":
                    errors.append(
                        f"{entity.relative_path}: depends_on reference is invalid: "
                        f"{dependency_id}"
                    )
                    continue
                target = targets[0]
                if (
                    target.frontmatter.get("title") == _BREAK_TITLE
                    or target.frontmatter.get("timer_kind") == _BREAK_TIMER_KIND
                ):
                    errors.append(
                        f"{entity.relative_path}: depends_on references break Task: "
                        f"{dependency_id}"
                    )
                elif (
                    target.relative_path.startswith("archive/")
                    and target.frontmatter.get("status") != "done"
                ):
                    errors.append(
                        f"{entity.relative_path}: depends_on references archived "
                        f"Task that is not done: {dependency_id}"
                    )

        waiting = {
            entity.entity_id: entity
            for entity in projected
            if entity.entity_type == "task"
            and not entity.relative_path.startswith("archive/")
            and entity.frontmatter.get("status") == "waiting"
            and len(entities_by_id.get(entity.entity_id, [])) == 1
        }
        waiting_graph = {
            entity_id: {
                dependency_id
                for dependency_id in (
                    parse_inline_list(
                        entity.frontmatter.get("depends_on", "")
                    )
                    or []
                )
                if dependency_id in waiting
            }
            for entity_id, entity in waiting.items()
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit_waiting(entity_id: str) -> bool:
            if entity_id in visiting:
                return True
            if entity_id in visited:
                return False
            visiting.add(entity_id)
            if any(
                visit_waiting(dependency_id)
                for dependency_id in sorted(waiting_graph[entity_id])
            ):
                return True
            visiting.remove(entity_id)
            visited.add(entity_id)
            return False

        for entity_id in sorted(waiting_graph):
            if entity_id not in visited and visit_waiting(entity_id):
                errors.append(
                    f"{waiting[entity_id].relative_path}: waiting Task dependency cycle"
                )
                break

        projects = [
            entity
            for entity in projected
            if entity.entity_type == "project"
            and not entity.relative_path.startswith("archive/")
        ]
        for lane in _PROJECT_KANBAN_LANES:
            entries = [
                entity
                for entity in projects
                if (
                    "not_started"
                    if entity.frontmatter.get("status") == "active"
                    else entity.frontmatter.get("status")
                )
                == lane
            ]
            positions = [
                entity.frontmatter.get("kanban_position", "")
                for entity in entries
            ]
            if not entries or any(not position for position in positions):
                continue
            if any(
                PROJECT_KANBAN_POSITION_PATTERN.fullmatch(position) is None
                for position in positions
            ):
                continue
            if sorted(int(position) for position in positions) != list(
                range(1, len(entries) + 1)
            ):
                errors.append(
                    f"{entries[0].relative_path}: kanban positions for {lane} "
                    "must be unique and contiguous from 1"
                )
        return tuple(sorted(errors))

    def _build_unvalidated_workflow_plan(
        self,
        *,
        action: str,
        target: Entity,
        active: Entity | None,
        resolution: str | None,
        expected_doing_id: str,
        timestamp: str,
        effects: tuple[WorkflowEffect, ...],
        work_event_timestamp: str | None = None,
    ) -> WorkflowPlan:
        """Build and sign exact effects; caller must validate the fixed snapshot."""
        operation_inputs = (
            ("action", action),
            ("target_entity_id", target.entity_id),
            ("target_base_hash", target.content_hash),
            ("active_entity_id", "" if active is None else active.entity_id),
            ("active_base_hash", "" if active is None else active.content_hash),
            ("resolution", resolution or ""),
            ("expected_doing_id", expected_doing_id),
            ("timestamp", timestamp),
            ("work_event_timestamp", work_event_timestamp or ""),
            (
                "generated_continuation_id",
                next(
                    (
                        effect.entity_id
                        for effect in effects
                        if effect.role == "continuation"
                    ),
                    "",
                ),
            ),
        )
        validation_errors: tuple[str, ...] = ()
        integrity_tag = self._workflow_plan_integrity_tag(
            action=action,
            target_entity_id=target.entity_id,
            target_base_hash=target.content_hash,
            active_entity_id=None if active is None else active.entity_id,
            active_base_hash=None if active is None else active.content_hash,
            resolution=resolution,
            operation_inputs=operation_inputs,
            effects=effects,
            validation_errors=validation_errors,
        )
        return WorkflowPlan(
            action=action,
            target_entity_id=target.entity_id,
            target_base_hash=target.content_hash,
            active_entity_id=None if active is None else active.entity_id,
            active_base_hash=None if active is None else active.content_hash,
            resolution=resolution,
            operation_inputs=operation_inputs,
            effects=effects,
            validation_errors=validation_errors,
            integrity_tag=integrity_tag,
        )

    def _virtual_workflow_validation_errors(
        self,
        effects: tuple[WorkflowEffect, ...],
        *,
        include_existing: bool = False,
        source_root: pathlib.Path | None = None,
    ) -> tuple[str, ...]:
        repository_root = self._root if source_root is None else source_root
        before_errors = (
            set() if include_existing else set(validate_repository(repository_root))
        )
        with tempfile.TemporaryDirectory(prefix="mokvia-workflow-preview-") as temporary:
            virtual_root = pathlib.Path(temporary)
            for directory in _SCAN_DIRECTORIES:
                (virtual_root / directory).mkdir(parents=True, exist_ok=True)
                scan_root = repository_root / directory
                if scan_root.is_symlink() or not scan_root.is_dir():
                    continue
                for path in self._walk_directory(scan_root):
                    relative = path.relative_to(repository_root)
                    destination = virtual_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(
                        self._read_regular_file(path)
                        if source_root is None
                        else path.read_bytes()
                    )

            for effect in effects:
                if effect.source_relative_path is not None:
                    source = virtual_root / effect.source_relative_path
                    if not source.exists():
                        raise MutationPlanConflict(
                            "planned workflow source is unavailable"
                        )
                    source.unlink()
            for effect in effects:
                destination = virtual_root / effect.destination_relative_path
                if destination.exists() or destination.is_symlink():
                    raise DestinationConflict(
                        "planned workflow destination is unavailable"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(effect.after_bytes)

            after_errors = set(validate_repository(virtual_root))
        return tuple(sorted(after_errors - before_errors))

    def _capture_workflow_validation_source(
        self,
    ) -> tuple[tuple[str, bytes], ...]:
        """Securely capture advisory validation input after plan lock release."""
        documents: list[tuple[str, bytes]] = []
        for directory in _SCAN_DIRECTORIES:
            scan_root = self._root / directory
            if scan_root.is_symlink() or not scan_root.is_dir():
                continue
            for path in self._walk_directory(scan_root):
                documents.append(
                    (
                        path.relative_to(self._root).as_posix(),
                        self._read_regular_file(path),
                    )
                )
        return tuple(documents)

    @staticmethod
    def _materialize_workflow_validation_source(
        documents: tuple[tuple[str, bytes], ...],
        destination_root: pathlib.Path,
    ) -> None:
        """Materialize an owned validation tree after releasing the lock."""
        for directory in _SCAN_DIRECTORIES:
            (destination_root / directory).mkdir(parents=True, exist_ok=True)
        for relative_path, content in documents:
            destination = destination_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

    def _workflow_plan_integrity_tag(
        self,
        *,
        action: str,
        target_entity_id: str,
        target_base_hash: str,
        active_entity_id: str | None,
        active_base_hash: str | None,
        resolution: str | None,
        operation_inputs: tuple[tuple[str, str], ...],
        effects: tuple[WorkflowEffect, ...],
        validation_errors: tuple[str, ...],
    ) -> str:
        digest = hmac.new(self._plan_integrity_key, digestmod=hashlib.sha256)
        values: list[bytes] = [
            action.encode(),
            target_entity_id.encode(),
            target_base_hash.encode(),
            (active_entity_id or "").encode(),
            (active_base_hash or "").encode(),
            (resolution or "").encode(),
        ]
        values.extend(
            f"{key}\0{value}".encode() for key, value in operation_inputs
        )
        for effect in effects:
            source_token = effect.source_token
            destination_token = effect.destination_token
            values.extend(
                (
                    effect.role.encode(),
                    effect.entity_id.encode(),
                    (effect.source_relative_path or "").encode(),
                    effect.destination_relative_path.encode(),
                    (effect.base_hash or "").encode(),
                    b"" if effect.before_bytes is None else effect.before_bytes,
                    effect.after_bytes,
                    (
                        b""
                        if source_token is None
                        else f"{source_token.st_dev}:{source_token.st_ino}".encode()
                    ),
                    (
                        b""
                        if destination_token is None
                        else f"{destination_token.st_dev}:{destination_token.st_ino}".encode()
                    ),
                )
            )
        values.extend(error.encode() for error in validation_errors)
        for value in values:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def _validate_workflow_plan(self, plan: WorkflowPlan) -> None:
        if type(plan) is not WorkflowPlan:
            raise InputError("invalid workflow plan type")
        if plan.action == "archive":
            self._validate_task_archive_workflow_plan(plan)
            return
        if plan.action == "resource_allocation_expand":
            self._validate_resource_allocation_expand_workflow_plan(plan)
            return
        if plan.action in {
            "resource_allocation_create",
            "resource_allocation_revise",
            "resource_allocation_withdraw",
        }:
            self._validate_resource_allocation_workflow_plan(plan)
            return
        if plan.action == "project_task_plan_create":
            self._validate_project_task_plan_workflow(plan)
            return
        if plan.action == "project_task_plan_update":
            self._validate_project_task_plan_update_workflow(plan)
            return
        if plan.action in {"project_task_move", "project_task_plan_migrate"}:
            self._validate_project_task_order_workflow(plan)
            return
        if plan.action in {
            "project_create", "project_move", "project_status_change", "project_archive"
        }:
            self._validate_project_order_workflow_plan(plan)
            return
        if plan.action in {
            "roadmap_move", "cycle_activate", "cycle_close",
            "roadmap_outcome_bundle_update",
        }:
            self._validate_roadmap_workflow_plan(plan)
            return
        if plan.action not in {
            "start", "interrupt", "complete", "create_and_start", "start_break",
            "update", "availability_release",
        }:
            raise InputError("invalid workflow plan action")
        if not isinstance(plan.effects, tuple) or not plan.effects:
            raise InputError("workflow plan effects are missing")
        if not isinstance(plan.operation_inputs, tuple):
            raise InputError("invalid workflow operation inputs")
        inputs: dict[str, str] = {}
        for item in plan.operation_inputs:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or item[0] in inputs
            ):
                raise InputError("invalid workflow operation inputs")
            inputs[item[0]] = item[1]
        required_inputs = {
            "action", "target_entity_id", "target_base_hash",
            "active_entity_id", "active_base_hash", "resolution",
            "expected_doing_id", "timestamp", "generated_continuation_id",
            "work_event_timestamp",
        }
        if set(inputs) != required_inputs:
            raise InputError("incomplete workflow operation inputs")
        if (
            inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
            or inputs["active_entity_id"] != (plan.active_entity_id or "")
            or inputs["active_base_hash"] != (plan.active_base_hash or "")
            or inputs["resolution"] != (plan.resolution or "")
        ):
            raise InputError("workflow plan operation is inconsistent")
        roles = tuple(effect.role for effect in plan.effects)

        def split_project_start(
            candidates: tuple[WorkflowEffect, ...],
        ) -> tuple[tuple[WorkflowEffect, ...], WorkflowEffect | None]:
            for index, effect in enumerate(candidates):
                if effect.role != "project_started":
                    continue
                if any(
                    trailing.role != "project_reordered"
                    for trailing in candidates[index + 1 :]
                ):
                    return candidates, None
                return candidates[:index], effect
            return candidates, None

        def ordered_dependency_roles(
            candidates: tuple[WorkflowEffect, ...], role: str
        ) -> bool:
            return (
                all(effect.role == role for effect in candidates)
                and [effect.entity_id for effect in candidates]
                == sorted(effect.entity_id for effect in candidates)
            )

        roles_valid = False
        if plan.action == "complete":
            roles_valid = (
                roles[0] == "completed"
                and ordered_dependency_roles(plan.effects[1:], "dependency_released")
            )
        elif plan.action == "interrupt":
            roles_valid = (
                len(roles) >= 2
                and roles[:2] == ("interrupted", "continuation")
                and ordered_dependency_roles(
                    plan.effects[2:], "dependency_retargeted"
                )
            )
        elif plan.action == "update":
            roles_valid = (
                roles[0]
                in {
                    "completed",
                    "dependency_released",
                    "dependency_retargeted",
                    "action_date_removed",
                    "availability_released",
                }
                and ordered_dependency_roles(
                    plan.effects[1:], "dependency_released"
                )
            )
        elif plan.action == "availability_release":
            roles_valid = ordered_dependency_roles(
                plan.effects, "availability_released"
            )
        elif plan.resolution == "complete":
            middle, _ = split_project_start(plan.effects[1:-1])
            roles_valid = (
                len(roles) >= 2
                and roles[0] == "completed"
                and roles[-1] == "started"
                and ordered_dependency_roles(
                    middle, "dependency_released"
                )
            )
        elif plan.resolution == "interrupt":
            middle, _ = split_project_start(plan.effects[2:-1])
            roles_valid = (
                len(roles) >= 3
                and roles[:2] == ("interrupted", "continuation")
                and roles[-1] == "started"
                and ordered_dependency_roles(
                    middle, "dependency_retargeted"
                )
            )
        else:
            middle, project_start = split_project_start(plan.effects[:-1])
            roles_valid = (
                roles[-1:] == ("started",)
                and not middle
                and (
                    project_start is not None
                    or len(plan.effects) == 1
                )
            )
        if not roles_valid or len({effect.entity_id for effect in plan.effects}) != len(plan.effects):
            raise InputError("workflow effect order is invalid")
        generated_ids = [
            effect.entity_id
            for effect in plan.effects
            if effect.role == "continuation"
        ]
        if inputs["generated_continuation_id"] != (
            generated_ids[0] if generated_ids else ""
        ):
            raise InputError("generated continuation ID is inconsistent")

        for effect in plan.effects:
            if type(effect) is not WorkflowEffect:
                raise InputError("invalid workflow effect type")
            destination = self._relative_plan_path(
                effect.destination_relative_path
            )
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if not self._entity_matches(parsed_after, effect.planned_entity):
                raise InputError("workflow planned entity does not match bytes")
            if parsed_after.entity_id != effect.entity_id:
                raise InputError("workflow effect identity is inconsistent")
            if effect.role == "project_started" and (
                effect.before_entity is None
                or effect.before_entity.entity_type != "project"
                or effect.before_entity.frontmatter.get("status")
                not in {"not_started", "active"}
                or effect.planned_entity.entity_type != "project"
                or effect.planned_entity.frontmatter.get("status") != "doing"
            ):
                raise InputError("Project start effect is invalid")
            if destination != self._canonical_destination(parsed_after.frontmatter):
                raise InputError("workflow destination is not canonical")
            if effect.source_relative_path is None:
                if any(
                    value is not None
                    for value in (
                        effect.base_hash,
                        effect.before_bytes,
                        effect.before_entity,
                        effect.source_token,
                        effect.destination_token,
                    )
                ):
                    raise InputError("workflow create effect has prior state")
            else:
                if (
                    effect.before_bytes is None
                    or effect.before_entity is None
                    or effect.source_token is None
                    or effect.base_hash is None
                ):
                    raise InputError("workflow effect prior state is missing")
                parsed_before = self._entity_from_bytes(
                    effect.source_relative_path, effect.before_bytes
                )
                if not self._entity_matches(parsed_before, effect.before_entity):
                    raise InputError("workflow before entity does not match bytes")
                if hashlib.sha256(effect.before_bytes).hexdigest() != effect.base_hash:
                    raise InputError("workflow base hash does not match bytes")
        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=plan.active_entity_id,
            active_base_hash=plan.active_base_hash,
            resolution=plan.resolution,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("workflow plan integrity check failed")

    def _validate_task_archive_workflow_plan(self, plan: WorkflowPlan) -> None:
        """Validate the exact archive and dependency-rewrite effect contract."""
        if (
            plan.active_entity_id is not None
            or plan.active_base_hash is not None
            or plan.resolution is not None
            or plan.validation_errors != ()
            or not isinstance(plan.effects, tuple)
            or len(plan.effects) < 2
        ):
            raise InputError("Task archive workflow is invalid")
        inputs = dict(plan.operation_inputs)
        if (
            len(inputs) != len(plan.operation_inputs)
            or set(inputs)
            != {
                "action",
                "target_entity_id",
                "target_base_hash",
                "dependents",
                "timestamp",
            }
            or inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
        ):
            raise InputError("Task archive workflow inputs are inconsistent")
        try:
            dependents = json.loads(inputs["dependents"])
            timestamp = datetime.datetime.fromisoformat(inputs["timestamp"])
        except (TypeError, ValueError) as error:
            raise InputError("Task archive workflow inputs are invalid") from error
        if (
            timestamp.utcoffset() is None
            or type(dependents) is not list
            or len(dependents) != len(plan.effects) - 1
            or any(
                type(item) is not dict
                or set(item) != {"id", "base_hash"}
                or not isinstance(item["id"], str)
                or not isinstance(item["base_hash"], str)
                or _CONTENT_HASH_PATTERN.fullmatch(item["base_hash"]) is None
                for item in dependents
            )
            or [item["id"] for item in dependents]
            != sorted(item["id"] for item in dependents)
            or len({item["id"] for item in dependents}) != len(dependents)
        ):
            raise InputError("Task archive workflow dependents are invalid")

        archive_effect = plan.effects[0]
        archived = archive_effect.before_entity
        parsed_archive_before = (
            None
            if archive_effect.before_bytes is None
            else self._entity_from_bytes(
                archive_effect.source_relative_path or "",
                archive_effect.before_bytes,
            )
        )
        parsed_archive_after = self._entity_from_bytes(
            archive_effect.destination_relative_path,
            archive_effect.after_bytes,
        )
        if (
            archive_effect.role != "task_archived"
            or archive_effect.entity_id != plan.target_entity_id
            or archived is None
            or archived.entity_type != "task"
            or archived.relative_path.startswith("archive/")
            or archive_effect.base_hash != plan.target_base_hash
            or archive_effect.source_relative_path != archived.relative_path
            or not archive_effect.destination_relative_path.startswith("archive/")
            or archive_effect.before_bytes is None
            or archive_effect.after_bytes != archive_effect.before_bytes
            or hashlib.sha256(archive_effect.before_bytes).hexdigest()
            != archive_effect.base_hash
            or parsed_archive_before is None
            or not self._entity_matches(parsed_archive_before, archived)
            or not self._entity_matches(
                parsed_archive_after, archive_effect.planned_entity
            )
            or archive_effect.source_token is None
            or archive_effect.destination_token is not None
            or archive_effect.planned_entity.relative_path
            != archive_effect.destination_relative_path
        ):
            raise InputError("Task archive workflow archive effect is invalid")
        source = self._relative_plan_path(archive_effect.source_relative_path)
        destination = self._relative_plan_path(
            archive_effect.destination_relative_path
        )
        archive_pattern = re.compile(
            rf"{re.escape(source.stem)}(?:-[2-9][0-9]*)?\.md"
        )
        if (
            destination.parent != self._root / "archive"
            or archive_pattern.fullmatch(destination.name) is None
        ):
            raise InputError("Task archive workflow destination is invalid")

        promoted_parent_id = self._task_archive_promoted_parent(archived)
        for item, effect in zip(dependents, plan.effects[1:], strict=True):
            before = effect.before_entity
            parsed_before = (
                None
                if effect.before_bytes is None
                else self._entity_from_bytes(
                    effect.source_relative_path or "", effect.before_bytes
                )
            )
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if (
                before is None
                or before.entity_id != item["id"]
                or before.entity_type != "task"
                or before.relative_path.startswith("archive/")
                or before.content_hash != item["base_hash"]
                or effect.entity_id != before.entity_id
                or effect.base_hash != before.content_hash
                or effect.source_relative_path != before.relative_path
                or effect.before_bytes is None
                or hashlib.sha256(effect.before_bytes).hexdigest()
                != effect.base_hash
                or parsed_before is None
                or not self._entity_matches(parsed_before, before)
                or not self._entity_matches(parsed_after, effect.planned_entity)
                or effect.destination_relative_path
                != self._canonical_destination(
                    dict(effect.planned_entity.frontmatter)
                ).relative_to(self._root).as_posix()
                or effect.source_token is None
                or effect.destination_token != effect.source_token
            ):
                raise InputError("Task archive workflow dependent effect is invalid")
            expected_role, expected_frontmatter = (
                self._task_archive_dependency_rewrite(
                    archived,
                    before,
                    promoted_parent_id,
                    inputs["timestamp"],
                )
            )
            expected_bytes = (
                serialize_frontmatter(expected_frontmatter, "task") + before.body
            ).encode("utf-8")
            if (
                effect.role != expected_role
                or effect.after_bytes != expected_bytes
                or effect.planned_entity.frontmatter != expected_frontmatter
                or effect.planned_entity.body != before.body
            ):
                raise InputError("Task archive dependency rewrite is inconsistent")

        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("Task archive workflow integrity check failed")

    def _validate_resource_allocation_workflow_plan(
        self, plan: WorkflowPlan
    ) -> None:
        expected_roles = {
            "resource_allocation_create": ("plan_created",),
            "resource_allocation_revise": ("plan_superseded", "plan_created"),
            "resource_allocation_withdraw": ("plan_withdrawn",),
        }[plan.action]
        if tuple(effect.role for effect in plan.effects) != expected_roles:
            raise InputError("allocation workflow effect order is invalid")
        if not plan.effects or len({effect.entity_id for effect in plan.effects}) != len(plan.effects):
            raise InputError("allocation workflow effects are invalid")
        inputs = dict(plan.operation_inputs)
        if (
            len(inputs) != len(plan.operation_inputs)
            or inputs.get("action") != plan.action
            or inputs.get("target_entity_id") != plan.target_entity_id
            or inputs.get("target_base_hash") != plan.target_base_hash
        ):
            raise InputError("allocation workflow inputs are inconsistent")
        for effect in plan.effects:
            if type(effect) is not WorkflowEffect:
                raise InputError("allocation workflow effect is invalid")
            parsed = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if (
                parsed.entity_type != "time_allocation_plan"
                or not self._entity_matches(parsed, effect.planned_entity)
                or self._canonical_destination(parsed.frontmatter)
                != self._relative_plan_path(effect.destination_relative_path)
                or parse_time_allocation_allocations(parsed.body) is None
            ):
                raise InputError("allocation workflow entity is invalid")
            if effect.source_relative_path is None:
                if any(value is not None for value in (
                    effect.base_hash, effect.before_bytes, effect.before_entity,
                    effect.source_token, effect.destination_token,
                )):
                    raise InputError("allocation create effect has prior state")
            else:
                if (
                    effect.base_hash is None
                    or effect.before_bytes is None
                    or effect.before_entity is None
                    or effect.source_token is None
                    or hashlib.sha256(effect.before_bytes).hexdigest()
                    != effect.base_hash
                ):
                    raise InputError("allocation update effect lacks prior state")
        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("allocation workflow integrity check failed")

    def _resource_allocation_expand_plan_inputs(
        self, plan: WorkflowPlan
    ) -> tuple[
        dict[str, str],
        tuple[str, ...],
        list[dict[str, object]],
        tuple[str, ...],
    ]:
        if not isinstance(plan.operation_inputs, tuple):
            raise InputError("allocation expansion inputs are invalid")
        inputs: dict[str, str] = {}
        for item in plan.operation_inputs:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or item[0] in inputs
            ):
                raise InputError("allocation expansion inputs are invalid")
            inputs[item[0]] = item[1]
        required_inputs = {
            "action",
            "target_entity_id",
            "target_base_hash",
            "source_id",
            "source_base_hash",
            "source_relative_path",
            "source_token",
            "axis",
            "source_period_kind",
            "source_period_start",
            "source_period_end",
            "source_input_mode",
            "source_total_minutes",
            "source_allocations",
            "target_period_kinds",
            "targets",
            "generated_ids",
        }
        if set(inputs) != required_inputs:
            raise InputError("allocation expansion inputs are incomplete")
        try:
            parsed_kinds = json.loads(inputs["target_period_kinds"])
            parsed_targets = json.loads(inputs["targets"])
            parsed_generated_ids = json.loads(inputs["generated_ids"])
        except (TypeError, ValueError) as error:
            raise InputError("allocation expansion inputs are invalid") from error
        if (
            not isinstance(parsed_kinds, list)
            or not all(isinstance(kind, str) for kind in parsed_kinds)
            or not isinstance(parsed_targets, list)
            or not all(isinstance(target, dict) for target in parsed_targets)
            or not isinstance(parsed_generated_ids, list)
            or not all(isinstance(entity_id, str) for entity_id in parsed_generated_ids)
        ):
            raise InputError("allocation expansion inputs are invalid")
        return (
            inputs,
            tuple(parsed_kinds),
            parsed_targets,
            tuple(parsed_generated_ids),
        )

    def _validate_resource_allocation_expand_workflow_plan(
        self, plan: WorkflowPlan
    ) -> None:
        if (
            plan.active_entity_id is not None
            or plan.active_base_hash is not None
            or plan.resolution is not None
            or plan.validation_errors != ()
            or not isinstance(plan.effects, tuple)
            or not plan.effects
            or len(plan.effects) > _RESOURCE_ALLOCATION_EXPAND_MAX_EFFECTS
        ):
            raise InputError("allocation expansion workflow is invalid")
        inputs, target_kinds, targets, generated_ids = (
            self._resource_allocation_expand_plan_inputs(plan)
        )
        if (
            inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
            or inputs["source_id"] != plan.target_entity_id
            or inputs["source_base_hash"] != plan.target_base_hash
            or inputs["axis"] not in TIME_ALLOCATION_AXES
            or inputs["source_input_mode"] not in TIME_ALLOCATION_INPUT_MODES
            or not 1 <= len(targets) <= _RESOURCE_ALLOCATION_EXPAND_MAX_TARGETS
            or len(generated_ids) != len(targets)
            or len(set(generated_ids)) != len(generated_ids)
            or re.fullmatch(r"[0-9]+:[0-9]+", inputs["source_token"]) is None
        ):
            raise InputError("allocation expansion inputs are inconsistent")
        normalized_kinds = self._normalize_resource_allocation_expand_kinds(
            inputs["source_period_kind"], target_kinds
        )
        if normalized_kinds != target_kinds:
            raise InputError("allocation expansion target kinds are not canonical")
        expected_periods = self._resource_allocation_expand_periods(
            inputs["source_period_kind"],
            inputs["source_period_start"],
            target_kinds,
        )
        source_start, source_end = self._resource_allocation_period(
            inputs["source_period_kind"], inputs["source_period_start"]
        )
        if inputs["source_period_end"] != source_end.date().isoformat():
            raise InputError("allocation expansion source period is inconsistent")
        try:
            source_total_minutes = int(inputs["source_total_minutes"])
            source_allocations = json.loads(inputs["source_allocations"])
        except (TypeError, ValueError) as error:
            raise InputError("allocation expansion source is invalid") from error
        if (
            source_total_minutes < 1
            or not isinstance(source_allocations, dict)
            or not source_allocations
            or any(
                type(target) is not str
                or type(value) is not int
                or value < 0
                for target, value in source_allocations.items()
            )
            or sum(source_allocations.values())
            != (
                10000
                if inputs["source_input_mode"] == "ratio"
                else source_total_minutes
            )
        ):
            raise InputError("allocation expansion source is invalid")

        if len(targets) != len(expected_periods):
            raise InputError("allocation expansion target count is inconsistent")
        expected_effect_count = sum(
            1 if target.get("current") is None else 2 for target in targets
        )
        if (
            expected_effect_count != len(plan.effects)
            or expected_effect_count > _RESOURCE_ALLOCATION_EXPAND_MAX_EFFECTS
        ):
            raise InputError("allocation expansion effect count is inconsistent")

        def parsed_effect(effect: WorkflowEffect) -> Entity:
            if type(effect) is not WorkflowEffect:
                raise InputError("allocation expansion effect is invalid")
            parsed = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if (
                parsed.entity_id != effect.entity_id
                or parsed.entity_type != "time_allocation_plan"
                or not self._entity_matches(parsed, effect.planned_entity)
                or self._canonical_destination(parsed.frontmatter)
                != self._relative_plan_path(effect.destination_relative_path)
                or parse_time_allocation_allocations(parsed.body) is None
            ):
                raise InputError("allocation expansion effect entity is invalid")
            return parsed

        effect_index = 0
        common_timestamp: str | None = None
        for target, period, generated_id in zip(
            targets, expected_periods, generated_ids, strict=True
        ):
            if set(target) != {
                "period_kind",
                "period_start",
                "period_end",
                "current",
                "generated_id",
                "total_minutes",
                "allocations",
            }:
                raise InputError("allocation expansion target facts are invalid")
            period_kind, period_start, period_end = period
            if (
                target["period_kind"] != period_kind
                or target["period_start"] != period_start
                or target["period_end"] != period_end
                or target["generated_id"] != generated_id
                or type(target["total_minutes"]) is not int
                or not isinstance(target["allocations"], dict)
            ):
                raise InputError("allocation expansion target facts are inconsistent")
            target_start_at, target_end = self._resource_allocation_period(
                period_kind, period_start
            )
            source_days = (source_end.date() - source_start.date()).days
            target_days = (target_end.date() - target_start_at.date()).days
            expected_total = (
                source_total_minutes * target_days + source_days // 2
            ) // source_days
            expected_allocations = (
                dict(sorted(source_allocations.items()))
                if inputs["source_input_mode"] == "ratio"
                else self._largest_remainder(source_allocations, expected_total)
            )
            if (
                expected_total < 1
                or target["total_minutes"] != expected_total
                or target["allocations"] != expected_allocations
            ):
                raise InputError("allocation expansion target values are inconsistent")

            current = target["current"]
            superseded: WorkflowEffect | None = None
            before: Entity | None = None
            if current is not None:
                if (
                    not isinstance(current, dict)
                    or set(current) != {
                        "entity_id",
                        "relative_path",
                        "content_hash",
                        "source_token",
                    }
                    or not all(isinstance(value, str) for value in current.values())
                    or re.fullmatch(r"[0-9]+:[0-9]+", current["source_token"])
                    is None
                ):
                    raise InputError(
                        "allocation expansion overwrite identity is invalid"
                    )
                superseded = plan.effects[effect_index]
                effect_index += 1
                before = superseded.before_entity
                parsed_after = parsed_effect(superseded)
                if (
                    superseded.role != "plan_superseded"
                    or superseded.entity_id != current["entity_id"]
                    or superseded.source_relative_path != current["relative_path"]
                    or superseded.destination_relative_path != current["relative_path"]
                    or superseded.base_hash != current["content_hash"]
                    or superseded.before_bytes is None
                    or before is None
                    or superseded.source_token is None
                    or superseded.destination_token != superseded.source_token
                    or f"{superseded.source_token.st_dev}:{superseded.source_token.st_ino}"
                    != current["source_token"]
                    or hashlib.sha256(superseded.before_bytes).hexdigest()
                    != current["content_hash"]
                    or before.entity_id != current["entity_id"]
                    or before.relative_path != current["relative_path"]
                    or before.content_hash != current["content_hash"]
                    or before.entity_type != "time_allocation_plan"
                    or before.frontmatter.get("status") != "active"
                    or (
                        before.frontmatter.get("axis"),
                        before.frontmatter.get("period_kind"),
                        before.frontmatter.get("period_start"),
                    )
                    != (inputs["axis"], period_kind, period_start)
                    or parsed_after.frontmatter.get("status") != "superseded"
                    or parsed_after.body != before.body
                ):
                    raise InputError(
                        "allocation expansion supersede effect is invalid"
                    )
                parsed_before = self._entity_from_bytes(
                    superseded.source_relative_path, superseded.before_bytes
                )
                if not self._entity_matches(parsed_before, before):
                    raise InputError(
                        "allocation expansion overwrite bytes are inconsistent"
                    )

            created = plan.effects[effect_index]
            effect_index += 1
            parsed_created = parsed_effect(created)
            if (
                created.role != "plan_created"
                or created.entity_id != generated_id
                or created.source_relative_path is not None
                or any(
                    value is not None
                    for value in (
                        created.base_hash,
                        created.before_bytes,
                        created.before_entity,
                        created.source_token,
                        created.destination_token,
                    )
                )
            ):
                raise InputError("allocation expansion create effect is invalid")
            timestamp = parsed_created.frontmatter.get("created_at", "")
            if common_timestamp is None:
                common_timestamp = timestamp
            if timestamp != common_timestamp:
                raise InputError("allocation expansion timestamps are inconsistent")
            try:
                expected_revision = (
                    1
                    if before is None
                    else int(before.frontmatter["revision"]) + 1
                )
            except (KeyError, ValueError) as error:
                raise InputError(
                    "allocation expansion revision is invalid"
                ) from error
            expected_frontmatter = self._resource_allocation_frontmatter(
                entity_id=generated_id,
                axis=inputs["axis"],
                period_kind=period_kind,
                period_start=period_start,
                revision=expected_revision,
                input_mode=inputs["source_input_mode"],
                total_minutes=expected_total,
                timestamp=timestamp,
                supersedes_id=None if before is None else before.entity_id,
            )
            if (
                dict(parsed_created.frontmatter) != expected_frontmatter
                or created.after_bytes
                != self._canonical_document(
                    expected_frontmatter,
                    "time_allocation_plan",
                    self._resource_allocation_body(expected_allocations),
                )
            ):
                raise InputError("allocation expansion created entity is invalid")
            if superseded is not None and before is not None:
                expected_superseded = dict(before.frontmatter)
                expected_superseded["status"] = "superseded"
                expected_superseded["updated_at"] = timestamp
                if (
                    dict(superseded.planned_entity.frontmatter)
                    != expected_superseded
                    or superseded.after_bytes
                    != self._canonical_document(
                        expected_superseded,
                        "time_allocation_plan",
                        before.body,
                    )
                ):
                    raise InputError(
                        "allocation expansion superseded entity is invalid"
                    )

        if effect_index != len(plan.effects):
            raise InputError("allocation expansion has unexpected effects")
        if (
            tuple(
                effect.entity_id
                for effect in plan.effects
                if effect.role == "plan_created"
            )
            != generated_ids
            or len({effect.entity_id for effect in plan.effects})
            != len(plan.effects)
            or len({effect.destination_relative_path for effect in plan.effects})
            != len(plan.effects)
        ):
            raise InputError("allocation expansion identities are invalid")

        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("allocation expansion integrity check failed")

    def _recheck_resource_allocation_expand_plan_locked(
        self, plan: WorkflowPlan
    ) -> None:
        inputs, target_kinds, targets, generated_ids = (
            self._resource_allocation_expand_plan_inputs(plan)
        )
        source_path = self._relative_plan_path(inputs["source_relative_path"])
        try:
            source_bytes, source_token = self._read_regular_file_with_token(
                source_path
            )
        except (FileNotFoundError, InputError) as error:
            raise MutationPlanConflict(
                "allocation expansion source is unavailable"
            ) from error
        if (
            hashlib.sha256(source_bytes).hexdigest() != inputs["source_base_hash"]
            or f"{source_token.st_dev}:{source_token.st_ino}"
            != inputs["source_token"]
        ):
            raise MutationPlanConflict("allocation expansion source changed")

        for target, generated_id in zip(targets, generated_ids, strict=True):
            current = target["current"]
            if current is not None:
                assert isinstance(current, dict)
                current_path = self._relative_plan_path(current["relative_path"])
                try:
                    current_bytes, current_token = (
                        self._read_regular_file_with_token(current_path)
                    )
                except (FileNotFoundError, InputError) as error:
                    raise MutationPlanConflict(
                        "allocation expansion overwrite target is unavailable"
                    ) from error
                if (
                    hashlib.sha256(current_bytes).hexdigest()
                    != current["content_hash"]
                    or f"{current_token.st_dev}:{current_token.st_ino}"
                    != current["source_token"]
                ):
                    raise MutationPlanConflict(
                        "allocation expansion overwrite target changed"
                    )
            created = next(
                effect
                for effect in plan.effects
                if effect.role == "plan_created"
                and effect.entity_id == generated_id
            )
            destination = self._relative_plan_path(
                created.destination_relative_path
            )
            if destination.exists() or destination.is_symlink():
                raise DestinationConflict(
                    "planned workflow destination is unavailable"
                )

        planning = self._workflow_planning_snapshot_locked()
        try:
            source = planning.find(inputs["source_id"])
        except (NotFoundError, InputError) as error:
            raise MutationPlanConflict(
                "allocation expansion source is unavailable"
            ) from error
        source_allocations = parse_time_allocation_allocations(source.body)
        if (
            source.entity_id != plan.target_entity_id
            or source.content_hash != plan.target_base_hash
            or source.relative_path != inputs["source_relative_path"]
            or source.entity_type != "time_allocation_plan"
            or source.relative_path.startswith("archive/")
            or source.frontmatter.get("status") != "active"
            or source.frontmatter.get("axis") != inputs["axis"]
            or source.frontmatter.get("period_kind")
            != inputs["source_period_kind"]
            or source.frontmatter.get("period_start")
            != inputs["source_period_start"]
            or source.frontmatter.get("period_end")
            != inputs["source_period_end"]
            or source.frontmatter.get("input_mode")
            != inputs["source_input_mode"]
            or source.frontmatter.get("total_minutes")
            != inputs["source_total_minutes"]
            or json.dumps(
                source_allocations, sort_keys=True, separators=(",", ":")
            )
            != inputs["source_allocations"]
        ):
            raise MutationPlanConflict("allocation expansion source changed")
        expected_periods = self._resource_allocation_expand_periods(
            inputs["source_period_kind"],
            inputs["source_period_start"],
            target_kinds,
        )
        actual_periods = tuple(
            (
                target["period_kind"],
                target["period_start"],
                target["period_end"],
            )
            for target in targets
        )
        if expected_periods != actual_periods:
            raise MutationPlanConflict(
                "allocation expansion target set changed"
            )

        active_by_key: dict[tuple[str, str, str], list[Entity]] = {}
        for entity in planning.entities:
            if (
                entity.entity_type != "time_allocation_plan"
                or entity.relative_path.startswith("archive/")
                or entity.frontmatter.get("status") != "active"
            ):
                continue
            key = (
                entity.frontmatter.get("axis", ""),
                entity.frontmatter.get("period_kind", ""),
                entity.frontmatter.get("period_start", ""),
            )
            active_by_key.setdefault(key, []).append(entity)
        for target in targets:
            key = (
                inputs["axis"],
                target["period_kind"],
                target["period_start"],
            )
            matches = active_by_key.get(key, [])
            current = target["current"]
            if current is None:
                if matches:
                    raise MutationPlanConflict(
                        "allocation expansion target presence changed"
                    )
                continue
            assert isinstance(current, dict)
            if (
                len(matches) != 1
                or matches[0].entity_id != current["entity_id"]
                or matches[0].relative_path != current["relative_path"]
                or matches[0].content_hash != current["content_hash"]
            ):
                raise MutationPlanConflict(
                    "allocation expansion overwrite identity changed"
                )
        for generated_id in generated_ids:
            if generated_id in planning.entities_by_id:
                raise DestinationConflict(
                    "planned workflow ID is unavailable"
                )

    def _validate_project_task_plan_workflow(self, plan: WorkflowPlan) -> None:
        if (
            plan.active_entity_id is not None
            or plan.active_base_hash is not None
            or plan.resolution is not None
            or not isinstance(plan.effects, tuple)
            or not plan.effects
        ):
            raise InputError("Project Task plan workflow is invalid")
        inputs = dict(plan.operation_inputs)
        expected_inputs = {
            "action", "target_entity_id", "target_base_hash", "tasks"
        }
        if "mode" in inputs:
            expected_inputs.add("mode")
        if (
            len(inputs) != len(plan.operation_inputs)
            or set(inputs) != expected_inputs
            or inputs.get("mode") not in {None, "tree"}
        ):
            raise InputError("Project Task plan operation inputs are invalid")
        if (
            inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
        ):
            raise InputError("Project Task plan operation is inconsistent")
        try:
            tasks = json.loads(inputs["tasks"])
        except (TypeError, ValueError) as error:
            raise InputError("Project Task plan operation is invalid") from error
        if type(tasks) is not list or len(tasks) != len(plan.effects):
            raise InputError("Project Task plan effects are incomplete")

        tree_mode = inputs.get("mode") == "tree"
        generated_by_key: dict[str, str] = {}
        sibling_positions: dict[str | None, int] = {}
        for item, effect in zip(tasks, plan.effects, strict=True):
            expected_item_keys = (
                {"key", "title", "parent_key", "generated_id"}
                if tree_mode
                else {"key", "title", "depends_on_keys", "generated_id"}
            )
            if (
                type(item) is not dict
                or not expected_item_keys <= set(item)
                or set(item) - expected_item_keys - ({"status"} if tree_mode else set())
            ):
                raise InputError("Project Task plan item is invalid")
            key = item["key"]
            title = item["title"]
            parent_key = item.get("parent_key")
            task_status = item.get("status")
            dependencies = (
                [] if parent_key is None else [parent_key]
            ) if tree_mode else item["depends_on_keys"]
            generated_id = item["generated_id"]
            if (
                not isinstance(key, str)
                or key in generated_by_key
                or not isinstance(title, str)
                or type(dependencies) is not list
                or any(dependency not in generated_by_key for dependency in dependencies)
                or not isinstance(generated_id, str)
                or (
                    tree_mode
                    and (
                        (parent_key is not None and "status" in item)
                        or ("status" in item and task_status not in {"planned", "next"})
                    )
                )
            ):
                raise InputError("Project Task plan item is invalid")
            dependency_ids = [generated_by_key[dependency] for dependency in dependencies]
            expected_position = None
            if tree_mode:
                if parent_key is not None and not isinstance(parent_key, str):
                    raise InputError("Project Task plan item is invalid")
                sibling_positions[parent_key] = sibling_positions.get(parent_key, 0) + 1
                expected_position = str(sibling_positions[parent_key])
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            expected_dependencies = (
                None if not dependency_ids else self._inline_list(dependency_ids)
            )
            if (
                effect.role != "task_created"
                or effect.entity_id != generated_id
                or effect.source_relative_path is not None
                or any(
                    value is not None
                    for value in (
                        effect.base_hash,
                        effect.before_bytes,
                        effect.before_entity,
                        effect.source_token,
                        effect.destination_token,
                    )
                )
                or not self._entity_matches(parsed_after, effect.planned_entity)
                or parsed_after.entity_id != generated_id
                or parsed_after.entity_type != "task"
                or parsed_after.frontmatter.get("title") != title
                or parsed_after.frontmatter.get("project_id")
                != plan.target_entity_id
                or parsed_after.frontmatter.get("status")
                != (
                    "planned"
                    if tree_mode and (dependency_ids or task_status == "planned")
                    else "waiting"
                    if dependency_ids
                    else "next"
                )
                or parsed_after.frontmatter.get("depends_on")
                != expected_dependencies
                or parsed_after.frontmatter.get("project_position")
                != expected_position
                or self._relative_plan_path(effect.destination_relative_path)
                != self._canonical_destination(parsed_after.frontmatter)
            ):
                raise InputError("Project Task plan effect is invalid")
            generated_by_key[key] = generated_id

        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("Project Task plan integrity check failed")

    def _validate_project_task_plan_update_workflow(
        self, plan: WorkflowPlan
    ) -> None:
        if (
            plan.active_entity_id is not None
            or plan.active_base_hash is not None
            or plan.resolution is not None
            or plan.validation_errors != ()
            or not isinstance(plan.effects, tuple)
            or not plan.effects
        ):
            raise InputError("Project Task plan update workflow is invalid")
        inputs = dict(plan.operation_inputs)
        if (
            len(inputs) != len(plan.operation_inputs)
            or set(inputs)
            != {
                "action", "target_entity_id", "target_base_hash", "nodes",
                "archives",
            }
            or inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
        ):
            raise InputError("Project Task plan update inputs are inconsistent")
        try:
            nodes = json.loads(inputs["nodes"])
            archives = json.loads(inputs["archives"])
        except (TypeError, ValueError) as error:
            raise InputError("Project Task plan update inputs are invalid") from error
        if (
            type(nodes) is not list
            or type(archives) is not list
            or any(type(item) is not dict for item in (*nodes, *archives))
        ):
            raise InputError("Project Task plan update inputs are invalid")

        by_key: dict[str, dict[str, object]] = {}
        expected_effects: list[tuple[str, str, dict[str, object]]] = []
        for item in nodes:
            if set(item) != {
                "key", "id", "existing", "base_hash", "title", "parent_key",
                "status", "project_position", "secondary_dependency_ids", "changed",
            }:
                raise InputError("Project Task plan update node is invalid")
            key = item["key"]
            entity_id = item["id"]
            if (
                not isinstance(key, str)
                or not isinstance(entity_id, str)
                or key in by_key
                or type(item["existing"]) is not bool
                or type(item["changed"]) is not bool
                or not isinstance(item["base_hash"], str)
                or not isinstance(item["title"], str)
                or (
                    item["parent_key"] is not None
                    and not isinstance(item["parent_key"], str)
                )
                or item["status"] not in {"planned", "next", "doing"}
                or not isinstance(item["project_position"], str)
                or type(item["secondary_dependency_ids"]) is not list
                or any(
                    not isinstance(value, str)
                    for value in item["secondary_dependency_ids"]
                )
            ):
                raise InputError("Project Task plan update node is invalid")
            by_key[key] = item
            if item["changed"]:
                expected_effects.append(
                    (
                        "task_updated" if item["existing"] else "task_created",
                        entity_id,
                        item,
                    )
                )
        for archive in archives:
            if (
                set(archive) != {"id", "base_hash"}
                or not isinstance(archive["id"], str)
                or not isinstance(archive["base_hash"], str)
            ):
                raise InputError("Project Task plan archive is invalid")
            expected_effects.append(("task_archived", archive["id"], archive))
        if len(expected_effects) != len(plan.effects):
            raise InputError("Project Task plan update effects are incomplete")

        resolved_id_by_key = {
            key: str(item["id"]) for key, item in by_key.items()
        }
        for (expected_role, expected_id, item), effect in zip(
            expected_effects, plan.effects, strict=True
        ):
            if (
                type(effect) is not WorkflowEffect
                or effect.role != expected_role
                or effect.entity_id != expected_id
            ):
                raise InputError("Project Task plan update effect order is invalid")
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if (
                not self._entity_matches(parsed_after, effect.planned_entity)
                or parsed_after.entity_id != effect.entity_id
                or parsed_after.entity_type != "task"
            ):
                raise InputError("Project Task plan update effect is invalid")
            if expected_role == "task_archived":
                if (
                    effect.source_relative_path is None
                    or effect.base_hash != item["base_hash"]
                    or effect.before_bytes is None
                    or effect.before_entity is None
                    or effect.source_token is None
                    or effect.after_bytes != effect.before_bytes
                    or self._relative_plan_path(effect.destination_relative_path).parent
                    != self._root / "archive"
                ):
                    raise InputError("Project Task archive effect is invalid")
                continue

            parent_key = item["parent_key"]
            parent_id = (
                None
                if parent_key is None
                else resolved_id_by_key.get(str(parent_key))
            )
            if parent_key is not None and parent_id is None:
                raise InputError("Project Task plan parent is invalid")
            dependencies = ([] if parent_id is None else [parent_id]) + [
                value
                for value in item["secondary_dependency_ids"]
                if value != parent_id
            ]
            expected_dependencies = (
                None if not dependencies else self._inline_list(dependencies)
            )
            if (
                parsed_after.frontmatter.get("project_id")
                != plan.target_entity_id
                or parsed_after.frontmatter.get("title") != item["title"]
                or parsed_after.frontmatter.get("status") != item["status"]
                or parsed_after.frontmatter.get("project_position")
                != item["project_position"]
                or parsed_after.frontmatter.get("depends_on")
                != expected_dependencies
                or self._relative_plan_path(effect.destination_relative_path)
                != self._canonical_destination(parsed_after.frontmatter)
            ):
                raise InputError("Project Task plan update effect is inconsistent")
            if item["existing"]:
                if (
                    effect.source_relative_path is None
                    or effect.base_hash != item["base_hash"]
                    or effect.before_bytes is None
                    or effect.before_entity is None
                    or effect.source_token is None
                ):
                    raise InputError("Project Task update prior state is missing")
            elif any(
                value is not None
                for value in (
                    effect.source_relative_path,
                    effect.base_hash,
                    effect.before_bytes,
                    effect.before_entity,
                    effect.source_token,
                    effect.destination_token,
                )
            ):
                raise InputError("Project Task create effect has prior state")

        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("Project Task plan update integrity check failed")

    def _validate_project_task_order_workflow(self, plan: WorkflowPlan) -> None:
        if (
            plan.active_entity_id is not None
            or plan.active_base_hash is not None
            or plan.resolution is not None
            or not isinstance(plan.effects, tuple)
            or not plan.effects
        ):
            raise InputError("Project Task workflow is invalid")
        inputs = dict(plan.operation_inputs)
        required = {
            "project_task_move": {
                "action", "target_entity_id", "target_base_hash", "project_id",
                "status", "primary_parent_id", "position",
            },
            "project_task_plan_migrate": {
                "action", "target_entity_id", "target_base_hash",
            },
        }[plan.action]
        if (
            len(inputs) != len(plan.operation_inputs)
            or set(inputs) != required
            or inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
        ):
            raise InputError("Project Task workflow operation is inconsistent")
        if plan.action == "project_task_move":
            if plan.effects[0].entity_id != plan.target_entity_id:
                raise InputError("Project Task move target effect must be first")
            roles = tuple(effect.role for effect in plan.effects)
            if roles[0] != "task_moved" or any(
                role != "task_reordered" for role in roles[1:]
            ):
                raise InputError("Project Task move effect order is invalid")
        elif any(effect.role != "planned_migrated" for effect in plan.effects):
            raise InputError("Project Task migration effects are invalid")

        for effect in plan.effects:
            if (
                type(effect) is not WorkflowEffect
                or effect.before_entity is None
                or effect.before_entity.entity_type != "task"
                or effect.planned_entity.entity_type != "task"
                or effect.source_relative_path is None
                or effect.before_bytes is None
                or effect.source_token is None
                or effect.base_hash is None
            ):
                raise InputError("Project Task workflow prior state is missing")
            parsed_before = self._entity_from_bytes(
                effect.source_relative_path, effect.before_bytes
            )
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if (
                not self._entity_matches(parsed_before, effect.before_entity)
                or not self._entity_matches(parsed_after, effect.planned_entity)
                or hashlib.sha256(effect.before_bytes).hexdigest() != effect.base_hash
                or self._relative_plan_path(effect.destination_relative_path)
                != self._canonical_destination(parsed_after.frontmatter)
            ):
                raise InputError("Project Task workflow entity bytes are inconsistent")
            if plan.action == "project_task_move":
                if (
                    parsed_before.frontmatter.get("project_id") != inputs["project_id"]
                    or parsed_before.frontmatter.get("status") != inputs["status"]
                    or (self._project_task_primary_parent(dict(parsed_before.frontmatter)) or "")
                    != inputs["primary_parent_id"]
                    or parsed_after.frontmatter.get("project_id") != inputs["project_id"]
                    or parsed_after.frontmatter.get("status") != inputs["status"]
                    or (self._project_task_primary_parent(dict(parsed_after.frontmatter)) or "")
                    != inputs["primary_parent_id"]
                    or PROJECT_TASK_POSITION_PATTERN.fullmatch(
                        parsed_after.frontmatter.get("project_position", "")
                    ) is None
                ):
                    raise InputError("Project Task move crossed its exact boundary")
            elif (
                parsed_before.frontmatter.get("project_id") != plan.target_entity_id
                or parsed_before.frontmatter.get("status") != "waiting"
                or not self._task_dependencies(dict(parsed_before.frontmatter))
                or parsed_before.frontmatter.get("waiting_for")
                or parsed_after.frontmatter.get("status") != "planned"
            ):
                raise InputError("Project Task migration widened its predicate")

        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("Project Task workflow integrity check failed")

    def _validate_project_order_workflow_plan(self, plan: WorkflowPlan) -> None:
        if not isinstance(plan.effects, tuple) or not plan.effects:
            raise InputError("Project workflow effects are missing")
        if (
            plan.active_entity_id is not None
            or plan.active_base_hash is not None
            or plan.resolution is not None
        ):
            raise InputError("Project workflow has Task-only state")
        inputs: dict[str, str] = {}
        for item in plan.operation_inputs:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or item[0] in inputs
            ):
                raise InputError("invalid Project workflow operation inputs")
            inputs[item[0]] = item[1]
        required = {
            "project_create": {"action", "target_entity_id", "target_base_hash"},
            "project_move": {
                "action", "target_entity_id", "target_base_hash", "status", "position"
            },
            "project_status_change": {
                "action", "target_entity_id", "target_base_hash", "status"
            },
            "project_archive": {"action", "target_entity_id", "target_base_hash"},
        }[plan.action]
        if set(inputs) != required or (
            inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
        ):
            raise InputError("Project workflow operation is inconsistent")
        if plan.effects[0].entity_id != plan.target_entity_id:
            raise InputError("Project workflow target effect must be first")
        if (
            plan.action == "project_create"
            and plan.target_base_hash
            != plan.effects[0].planned_entity.content_hash
        ):
            raise InputError("Project create workflow target hash is inconsistent")
        expected_target_role = {
            "project_create": "project_created",
            "project_move": "project_moved",
            "project_status_change": "project_status_changed",
            "project_archive": "project_archived",
        }[plan.action]
        roles = tuple(effect.role for effect in plan.effects)
        if roles[0] != expected_target_role or any(
            role != "project_reordered" for role in roles[1:]
        ):
            raise InputError("Project workflow effect order is invalid")
        for index, effect in enumerate(plan.effects):
            if type(effect) is not WorkflowEffect:
                raise InputError("invalid Project workflow effect")
            if plan.action == "project_create" and index == 0:
                if (
                    effect.before_entity is not None
                    or effect.planned_entity.entity_type != "project"
                    or effect.source_relative_path is not None
                    or effect.before_bytes is not None
                    or effect.source_token is not None
                    or effect.base_hash is not None
                ):
                    raise InputError("Project create workflow prior state is invalid")
                destination = self._relative_plan_path(
                    effect.destination_relative_path
                )
                if destination != self._canonical_destination(
                    effect.planned_entity.frontmatter
                ):
                    raise InputError("Project workflow destination is not canonical")
                parsed_after = self._entity_from_bytes(
                    effect.destination_relative_path, effect.after_bytes
                )
                if not self._entity_matches(parsed_after, effect.planned_entity):
                    raise InputError("Project workflow entity bytes are inconsistent")
                continue
            if (
                effect.before_entity is None
                or effect.before_entity.entity_type != "project"
                or effect.planned_entity.entity_type != "project"
                or effect.source_relative_path is None
                or effect.before_bytes is None
                or effect.source_token is None
                or effect.base_hash is None
            ):
                raise InputError("Project workflow effect prior state is missing")
            destination = self._relative_plan_path(effect.destination_relative_path)
            if plan.action == "project_archive" and index == 0:
                if destination.parent != self._root / "archive":
                    raise InputError("Project archive destination is not canonical")
                if effect.after_bytes != effect.before_bytes:
                    raise InputError("Project archive must preserve exact bytes")
            elif destination != self._canonical_destination(
                effect.planned_entity.frontmatter
            ):
                raise InputError("Project workflow destination is not canonical")
            parsed_before = self._entity_from_bytes(
                effect.source_relative_path, effect.before_bytes
            )
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if (
                not self._entity_matches(parsed_before, effect.before_entity)
                or not self._entity_matches(parsed_after, effect.planned_entity)
                or hashlib.sha256(effect.before_bytes).hexdigest() != effect.base_hash
            ):
                raise InputError("Project workflow entity bytes are inconsistent")
        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("Project workflow integrity check failed")

    def _validate_roadmap_workflow_plan(self, plan: WorkflowPlan) -> None:
        if not isinstance(plan.effects, tuple) or not plan.effects:
            raise InputError("workflow plan effects are missing")
        if plan.active_entity_id is not None or plan.active_base_hash is not None or plan.resolution is not None:
            raise InputError("Roadmap workflow has Task-only state")
        inputs: dict[str, str] = {}
        for item in plan.operation_inputs:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or item[0] in inputs
            ):
                raise InputError("invalid workflow operation inputs")
            inputs[item[0]] = item[1]
        required = {
            "roadmap_move": {"action", "target_entity_id", "target_base_hash", "lane", "position"},
            "cycle_activate": {"action", "target_entity_id", "target_base_hash"},
            "cycle_close": {
                "action", "target_entity_id", "target_base_hash", "status",
                "outcome_results", "retrospective", "cancellation_reason",
                "carryover_cycle_id",
            },
            "roadmap_outcome_bundle_update": {
                "action", "target_entity_id", "target_base_hash", "bundle",
            },
        }[plan.action]
        if set(inputs) != required or (
            inputs["action"] != plan.action
            or inputs["target_entity_id"] != plan.target_entity_id
            or inputs["target_base_hash"] != plan.target_base_hash
        ):
            raise InputError("Roadmap workflow operation is inconsistent")
        if plan.effects[0].entity_id != plan.target_entity_id:
            raise InputError("Roadmap workflow target effect must be first")
        if plan.action == "roadmap_outcome_bundle_update":
            self._validate_roadmap_bundle_workflow_plan(plan, inputs["bundle"])
        if plan.action == "cycle_activate":
            roles = tuple(effect.role for effect in plan.effects)
            if roles[0] != "cycle_activated" or any(
                role not in {"outcome_now", "outcome_reordered"}
                for role in roles[1:]
            ):
                raise InputError("Cycle activation effect order is invalid")
        if plan.action == "cycle_close":
            roles = tuple(effect.role for effect in plan.effects)
            if roles[0] != "cycle_closed" or any(
                not role.startswith("outcome_") and role != "carryover_cycle_updated"
                for role in roles[1:]
            ):
                raise InputError("Cycle close effect order is invalid")
            if "carryover_cycle_updated" in roles[:-1]:
                raise InputError("carryover Cycle effect must be last")
        for effect in plan.effects:
            if type(effect) is not WorkflowEffect:
                raise InputError("invalid Roadmap workflow effect")
            destination = self._relative_plan_path(effect.destination_relative_path)
            parsed_after = self._entity_from_bytes(
                effect.destination_relative_path, effect.after_bytes
            )
            if not self._entity_matches(parsed_after, effect.planned_entity):
                raise InputError("workflow planned entity does not match bytes")
            is_bundle_archive = (
                plan.action == "roadmap_outcome_bundle_update"
                and effect.role in {"task_archived", "project_archived"}
            )
            if is_bundle_archive:
                if destination.parent != self._root / "archive":
                    raise InputError("bundle archive destination is not canonical")
            elif destination != self._canonical_destination(parsed_after.frontmatter):
                raise InputError("workflow destination is not canonical")
            if effect.source_relative_path is None:
                if (
                    plan.action != "roadmap_outcome_bundle_update"
                    or effect.role != "project_created"
                    or effect.base_hash is not None
                    or effect.before_bytes is not None
                    or effect.before_entity is not None
                    or effect.source_token is not None
                ):
                    raise InputError("Roadmap workflow create effect is invalid")
                continue
            if (
                effect.before_bytes is None
                or effect.before_entity is None
                or effect.source_token is None
                or effect.base_hash is None
            ):
                raise InputError("workflow effect prior state is missing")
            parsed_before = self._entity_from_bytes(
                effect.source_relative_path, effect.before_bytes
            )
            if not self._entity_matches(parsed_before, effect.before_entity):
                raise InputError("workflow before entity does not match bytes")
            if hashlib.sha256(effect.before_bytes).hexdigest() != effect.base_hash:
                raise InputError("workflow base hash does not match bytes")
        expected_tag = self._workflow_plan_integrity_tag(
            action=plan.action,
            target_entity_id=plan.target_entity_id,
            target_base_hash=plan.target_base_hash,
            active_entity_id=None,
            active_base_hash=None,
            resolution=None,
            operation_inputs=plan.operation_inputs,
            effects=plan.effects,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_tag, plan.integrity_tag):
            raise InputError("workflow plan integrity check failed")

    def _validate_roadmap_bundle_workflow_plan(
        self, plan: WorkflowPlan, encoded_bundle: str
    ) -> None:
        try:
            bundle = json.loads(encoded_bundle)
        except (TypeError, ValueError) as error:
            raise InputError("Roadmap bundle operation is invalid") from error
        if type(bundle) is not dict or set(bundle) != {
            "fields", "body", "project_changes"
        }:
            raise InputError("Roadmap bundle operation is invalid")
        changes = bundle["project_changes"]
        if type(changes) is not list:
            raise InputError("Roadmap bundle operation is invalid")
        effects = plan.effects
        first = effects[0]
        if (
            first.role != "outcome_updated"
            or first.entity_id != plan.target_entity_id
            or first.before_entity is None
            or first.before_entity.entity_type != "roadmap_outcome"
            or first.planned_entity.entity_type != "roadmap_outcome"
        ):
            raise InputError("Roadmap bundle Outcome effect is invalid")

        effect_index = 1
        changed_project_ids: set[str] = set()
        for change in changes:
            if type(change) is not dict or type(change.get("action")) is not str:
                raise InputError("Roadmap bundle Project change is invalid")
            action = change["action"]
            if action == "create":
                if effect_index >= len(effects):
                    raise InputError("Roadmap bundle Project effect is missing")
                effect = effects[effect_index]
                effect_index += 1
                if (
                    effect.role != "project_created"
                    or effect.entity_id != change.get("generated_id")
                    or effect.before_entity is not None
                    or effect.planned_entity.entity_type != "project"
                    or effect.planned_entity.frontmatter.get("roadmap_outcome_id")
                    != plan.target_entity_id
                ):
                    raise InputError("Roadmap bundle Project create is invalid")
                continue

            project_id = change.get("id")
            if not isinstance(project_id, str) or project_id in changed_project_ids:
                raise InputError("Roadmap bundle Project identity is invalid")
            changed_project_ids.add(project_id)
            if action in {"update", "move"}:
                if effect_index >= len(effects):
                    raise InputError("Roadmap bundle Project effect is missing")
                effect = effects[effect_index]
                effect_index += 1
                expected_role = (
                    "project_updated" if action == "update" else "project_moved"
                )
                if (
                    effect.role != expected_role
                    or effect.entity_id != project_id
                    or effect.before_entity is None
                    or effect.before_entity.entity_type != "project"
                    or effect.planned_entity.entity_type != "project"
                ):
                    raise InputError("Roadmap bundle Project update is invalid")
                if action == "update" and (
                    effect.before_entity.frontmatter.get("roadmap_outcome_id")
                    != plan.target_entity_id
                ):
                    raise InputError("Roadmap bundle Project update is invalid")
                if action == "move" and (
                    effect.planned_entity.frontmatter.get("roadmap_outcome_id")
                    != plan.target_entity_id
                    or effect.before_entity.frontmatter.get("roadmap_outcome_id")
                    == plan.target_entity_id
                ):
                    raise InputError("Roadmap bundle Project move is invalid")
                continue
            if action != "archive" or type(change.get("task_cascade")) is not list:
                raise InputError("Roadmap bundle Project action is invalid")

            cascade_ids = [
                item.get("id") if type(item) is dict else None
                for item in change["task_cascade"]
            ]
            if any(not isinstance(item, str) for item in cascade_ids):
                raise InputError("Roadmap bundle Task cascade is invalid")
            expected_children = {
                entity.entity_id
                for entity in self.list_entities()
                if entity.entity_type == "task"
                and not entity.relative_path.startswith("archive/")
                and entity.frontmatter.get("project_id") == project_id
            }
            if set(cascade_ids) != expected_children or len(cascade_ids) != len(
                expected_children
            ):
                raise MutationPlanConflict("Roadmap bundle Task cascade changed")
            for child_id in cascade_ids:
                if effect_index >= len(effects):
                    raise InputError("Roadmap bundle Task archive effect is missing")
                effect = effects[effect_index]
                effect_index += 1
                if (
                    effect.role != "task_archived"
                    or effect.entity_id != child_id
                    or effect.before_entity is None
                    or effect.before_entity.entity_type != "task"
                    or effect.before_entity.frontmatter.get("project_id") != project_id
                ):
                    raise InputError("Roadmap bundle Task archive is invalid")
            if effect_index >= len(effects):
                raise InputError("Roadmap bundle Project archive effect is missing")
            effect = effects[effect_index]
            effect_index += 1
            if (
                effect.role != "project_archived"
                or effect.entity_id != project_id
                or effect.before_entity is None
                or effect.before_entity.entity_type != "project"
                or effect.before_entity.frontmatter.get("roadmap_outcome_id")
                != plan.target_entity_id
            ):
                raise InputError("Roadmap bundle Project archive is invalid")
        trailing = effects[effect_index:]
        if (
            any(effect.role != "project_reordered" for effect in trailing)
            or [effect.entity_id for effect in trailing]
            != sorted(effect.entity_id for effect in trailing)
        ):
            raise InputError("Roadmap bundle has unexpected effects")

    def _apply_task_workflow_locked(
        self, plan: WorkflowPlan
    ) -> tuple[Entity, ...]:
        self._validate_workflow_plan(plan)
        self._reject_workflow_symlinks()
        fast_task_workflow = plan.action in _FAST_TASK_WORKFLOW_ACTIONS
        workflow_generation: int | None = None
        if fast_task_workflow:
            workflow_documents, workflow_generation = (
                self._repository_document_snapshot()
            )
            workflow_planning = self._cached_workflow_planning_snapshot()
        else:
            workflow_documents = None
            workflow_planning = None
        projected_task_statuses = {
            effect.entity_id: effect.planned_entity.frontmatter.get("status", "")
            for effect in plan.effects
            if effect.planned_entity.entity_type == "task"
        }
        for effect in plan.effects:
            if effect.role not in {
                "availability_released", "dependency_released", "started"
            }:
                continue
            try:
                dependencies_satisfied = self._dependencies_satisfied(
                    dict(effect.planned_entity.frontmatter),
                    projected_task_statuses,
                    planning=workflow_planning,
                )
            except InputError as error:
                raise MutationPlanConflict(
                    "Task dependencies changed after preview"
                ) from error
            if not dependencies_satisfied:
                raise MutationPlanConflict(
                    "Task dependencies changed after preview"
                )
        projected_project_statuses = {
            effect.entity_id: effect.planned_entity.frontmatter.get("status", "")
            for effect in plan.effects
            if effect.planned_entity.entity_type == "project"
            and not effect.destination_relative_path.startswith("archive/")
        }
        for effect in plan.effects:
            if effect.destination_relative_path.startswith("archive/"):
                continue
            planned_frontmatter = dict(effect.planned_entity.frontmatter)
            if effect.planned_entity.entity_type == "task":
                self._validate_links(planned_frontmatter, "task")
                self._require_doing_task_project_status(
                    planned_frontmatter, projected_project_statuses
                )
            elif (
                effect.planned_entity.entity_type == "project"
                and effect.before_entity is not None
            ):
                self._validate_project_transition(
                    effect.before_entity, planned_frontmatter
                )
        inputs = dict(plan.operation_inputs)
        if plan.action == "archive":
            current = self._require_mutation_current(
                plan.target_entity_id, plan.target_base_hash
            )
            if current.entity_type != "task":
                raise InputError("Task archive workflow target is invalid")
            try:
                expected_dependents = json.loads(inputs["dependents"])
            except (KeyError, TypeError, ValueError) as error:
                raise InputError(
                    "Task archive workflow dependents are invalid"
                ) from error
            current_dependents = self._active_task_dependency_referrers(
                current.entity_id
            )
            actual = [
                {
                    "id": dependent.entity_id,
                    "base_hash": dependent.content_hash,
                }
                for dependent in current_dependents
            ]
            if actual != expected_dependents:
                raise MutationPlanConflict(
                    "Task dependency references changed after preview"
                )
        if plan.action == "project_archive":
            current_project = self._require_mutation_current(
                plan.target_entity_id, plan.target_base_hash
            )
            self._require_no_active_children(current_project)
        if plan.action in {
            "project_task_plan_create", "project_task_plan_update",
            "project_task_plan_migrate",
        }:
            project = self._require_mutation_current(
                plan.target_entity_id, plan.target_base_hash
            )
            if (
                project.entity_type != "project"
                or project.relative_path.startswith("archive/")
                or project.frontmatter.get("status")
                not in {"not_started", "doing"}
            ):
                raise ProjectOperationError("project_not_executable")
            if plan.action == "project_task_plan_update":
                try:
                    planned_nodes = json.loads(inputs["nodes"])
                    planned_archives = json.loads(inputs["archives"])
                except (KeyError, TypeError, ValueError) as error:
                    raise InputError(
                        "Project Task plan update inputs are invalid"
                    ) from error
                expected_hashes = {
                    item["id"]: item["base_hash"]
                    for item in planned_nodes
                    if item["existing"]
                }
                expected_hashes.update(
                    {item["id"]: item["base_hash"] for item in planned_archives}
                )
                current_member_list = tuple(
                    entity
                    for entity in self._project_task_tree_members(project.entity_id)
                    if entity.frontmatter.get("status")
                    in {"planned", "next", "doing"}
                )
                current_members = {
                    entity.entity_id: entity for entity in current_member_list
                }
                if (
                    len(current_members) != len(current_member_list)
                    or set(current_members) != set(expected_hashes)
                    or any(
                        current_members[entity_id].content_hash != expected_hash
                        for entity_id, expected_hash in expected_hashes.items()
                    )
                ):
                    raise MutationPlanConflict(
                        "Project Task tree membership changed"
                    )
            if plan.action == "project_task_plan_migrate":
                current_candidates = {
                    entity.entity_id
                    for entity in self.list_entities()
                    if entity.entity_type == "task"
                    and not entity.relative_path.startswith("archive/")
                    and entity.frontmatter.get("project_id") == project.entity_id
                    and entity.frontmatter.get("status") == "waiting"
                    and bool(self._task_dependencies(dict(entity.frontmatter)))
                    and not entity.frontmatter.get("waiting_for")
                }
                if current_candidates != {effect.entity_id for effect in plan.effects}:
                    raise MutationPlanConflict("Project Task migration target set changed")
        elif plan.action == "project_task_move":
            target = self._require_workflow_task(
                plan.target_entity_id, plan.target_base_hash
            )
            current_group = {
                entity.entity_id
                for entity in self._project_task_tree_members(inputs["project_id"])
                if entity.frontmatter.get("status") == inputs["status"]
                and (self._project_task_primary_parent(dict(entity.frontmatter)) or "")
                == inputs["primary_parent_id"]
            }
            if (
                target.frontmatter.get("project_id") != inputs["project_id"]
                or target.frontmatter.get("status") != inputs["status"]
                or target.entity_id not in current_group
                or current_group != {effect.entity_id for effect in plan.effects}
            ):
                raise MutationPlanConflict("Project Task sibling group changed")
        elif plan.action not in {
            "project_create", "project_move", "project_status_change", "project_archive",
            "project_task_move", "project_task_plan_update", "project_task_plan_migrate",
            "roadmap_move", "cycle_activate", "cycle_close",
            "roadmap_outcome_bundle_update",
            "resource_allocation_create", "resource_allocation_revise",
            "resource_allocation_withdraw", "resource_allocation_expand", "archive",
        }:
            doing = self._current_doing_task(workflow_planning)
            actual_doing_id = "" if doing is None else doing.entity_id
            if actual_doing_id != inputs["expected_doing_id"]:
                raise MutationPlanConflict("current doing Task changed")

        if plan.action == "resource_allocation_expand":
            self._recheck_resource_allocation_expand_plan_locked(plan)

        before_errors = (
            set(
                self._validation_errors_for_document_snapshot(
                    workflow_documents, workflow_generation
                )
            )
            if workflow_documents is not None
            and workflow_generation is not None
            else set(validate_repository(self._root))
        )
        destination_paths: set[pathlib.Path] = set()
        for effect in plan.effects:
            destination = self._relative_plan_path(
                effect.destination_relative_path
            )
            if destination in destination_paths:
                raise DestinationConflict("duplicate workflow destination")
            destination_paths.add(destination)
            if effect.source_relative_path is None:
                try:
                    self._find_mutation_entity(effect.entity_id)
                except NotFoundError:
                    pass
                else:
                    raise DestinationConflict("planned workflow ID is unavailable")
                if destination.exists() or destination.is_symlink():
                    raise DestinationConflict(
                        "planned workflow destination is unavailable"
                    )
                continue
            source = self._relative_plan_path(effect.source_relative_path)
            source_bytes, source_token = self._read_regular_file_with_token(source)
            if source_bytes != effect.before_bytes:
                try:
                    current = self._find_mutation_entity(effect.entity_id)
                except NotFoundError as error:
                    raise MutationPlanConflict(
                        "planned workflow source is unavailable"
                    ) from error
                raise ConflictError(current)
            if source_token != effect.source_token:
                raise MutationPlanConflict("planned workflow source inode changed")
            if destination != source and (
                destination.exists() or destination.is_symlink()
            ):
                raise DestinationConflict(
                    "planned workflow destination is unavailable"
                )

        if workflow_documents is not None:
            projected_documents = self._project_workflow_document_map(
                workflow_documents, plan.effects
            )
            with self._repository_cache_lock:
                cached_projection = self._fast_workflow_validations.get(
                    plan.integrity_tag
                )
            projected_errors = set(
                cached_projection[1]
                if cached_projection is not None
                and cached_projection[0] == workflow_generation
                else (
                    validate_document_map(projected_documents)
                    if before_errors
                    else self._fast_task_projection_validation_errors(
                        workflow_planning, plan.effects
                    )
                )
            )
            new_projected_errors = sorted(projected_errors - before_errors)
            if new_projected_errors:
                raise SchemaError(new_projected_errors)

        if plan.action == "roadmap_outcome_bundle_update":
            validation_errors = self._virtual_workflow_validation_errors(
                plan.effects, include_existing=True
            )
            if validation_errors:
                raise SchemaError(list(validation_errors))

        reservations: list[
            tuple[pathlib.Path, pathlib.Path, PublicationToken, bytes]
        ] = []
        publication_tokens: dict[pathlib.Path, PublicationToken] = {}
        results: list[Entity] = []
        try:
            for effect in plan.effects:
                if effect.source_relative_path is None:
                    continue
                source = self._relative_plan_path(effect.source_relative_path)
                assert effect.source_token is not None
                assert effect.before_bytes is not None
                quarantine = self._reserve_owned_source_for_replace(
                    source, effect.source_token
                )
                reservations.append(
                    (source, quarantine, effect.source_token, effect.before_bytes)
                )

            for effect in plan.effects:
                destination = self._relative_plan_path(
                    effect.destination_relative_path
                )
                if destination.exists() or destination.is_symlink():
                    raise DestinationConflict(
                        "planned workflow destination is unavailable"
                    )
                self._publish_no_replace(
                    destination, effect.after_bytes, publication_tokens
                )

            if workflow_documents is None:
                after_errors = set(validate_repository(self._root))
                new_errors = sorted(after_errors - before_errors)
                if new_errors:
                    raise SchemaError(new_errors)

            for effect in plan.effects:
                destination = self._relative_plan_path(
                    effect.destination_relative_path
                )
                result_bytes, result_token = self._read_regular_file_with_token(
                    destination
                )
                if (
                    result_bytes != effect.after_bytes
                    or result_token != publication_tokens.get(destination)
                ):
                    raise MutationPlanConflict(
                        "workflow publication ownership changed"
                    )
                result = self._entity_from_bytes(
                    effect.destination_relative_path, result_bytes
                )
                if not self._entity_matches(result, effect.planned_entity):
                    raise InputError("workflow result differs from planned bytes")
                results.append(result)
            if workflow_documents is not None:
                after_documents, after_generation = (
                    self._repository_document_snapshot()
                )
                if after_documents == projected_documents:
                    after_errors = projected_errors
                    self._cache_validation_errors_for_exact_snapshot(
                        after_documents,
                        after_generation,
                        after_errors,
                    )
                else:
                    after_errors = set(
                        self._validation_errors_for_document_snapshot(
                            after_documents, after_generation
                        )
                    )
                new_errors = sorted(after_errors - before_errors)
                if new_errors:
                    raise SchemaError(new_errors)
                for effect in plan.effects:
                    destination = self._relative_plan_path(
                        effect.destination_relative_path
                    )
                    result_bytes, result_token = (
                        self._read_regular_file_with_token(destination)
                    )
                    if (
                        result_bytes != effect.after_bytes
                        or result_token != publication_tokens.get(destination)
                    ):
                        raise MutationPlanConflict(
                            "workflow publication ownership changed"
                        )
        except BaseException as primary_error:
            try:
                self._rollback_task_workflow(
                    reservations,
                    publication_tokens,
                    before_errors,
                    use_document_map=workflow_documents is not None,
                    before_documents=workflow_documents,
                )
            except WorkflowRollbackError as rollback_error:
                raise rollback_error from primary_error
            except BaseException as rollback_error:
                raise WorkflowRollbackError(
                    (rollback_error,),
                    (),
                ) from primary_error
            raise

        # Commit boundary: all planned publications were validated and securely
        # reread. Cleanup below must never roll canonical effects back.
        cleanup_errors: list[BaseException] = []
        cleanup_residuals: set[pathlib.Path] = set()
        cleanup_protected: set[pathlib.Path] = set()
        cleanup_uncertain: set[pathlib.Path] = set()
        for _, quarantine, token, _ in reservations:
            try:
                self._discard_reserved_source(quarantine, token)
            except BaseException as error:
                cleanup_errors.append(error)
                if isinstance(error, QuarantineCleanupError):
                    cleanup_residuals.update(error.residual_paths)
                    cleanup_protected.update(error.protected_paths)
                    cleanup_uncertain.update(error.uncertain_paths)
        if cleanup_errors:
            cleanup_residuals.update(
                quarantine
                for _, quarantine, _, _ in reservations
                if quarantine.exists() or quarantine.is_symlink()
            )
            remaining_quarantines = tuple(
                sorted(
                    (
                        path
                        for path in cleanup_residuals
                        if path.exists() or path.is_symlink()
                    ),
                    key=lambda path: path.as_posix(),
                )
            )
            committed_entities = tuple(
                effect.planned_entity for effect in plan.effects
            )
            raise WorkflowPostCommitCleanupError(
                committed_entities,
                tuple(cleanup_errors),
                remaining_quarantines,
                tuple(sorted(cleanup_protected, key=lambda path: path.as_posix())),
                tuple(sorted(cleanup_uncertain, key=lambda path: path.as_posix())),
            ) from cleanup_errors[0]
        return tuple(results)

    def _rollback_task_workflow(
        self,
        reservations: list[
            tuple[pathlib.Path, pathlib.Path, PublicationToken, bytes]
        ],
        publication_tokens: dict[pathlib.Path, PublicationToken],
        before_errors: set[str],
        *,
        use_document_map: bool = False,
        before_documents: Mapping[str, bytes] | None = None,
    ) -> None:
        rollback_errors: list[BaseException] = []
        protected_paths: set[pathlib.Path] = set()
        blocked_paths: set[pathlib.Path] = set()
        for path, token in reversed(tuple(publication_tokens.items())):
            try:
                self._quarantine_delete_owned_path(
                    path, token, ownership_kind="publication"
                )
            except FileNotFoundError:
                pass
            except BaseException as error:
                rollback_errors.append(error)
                state, inspection_error = self._publication_path_state(path, token)
                if inspection_error is not None:
                    rollback_errors.append(inspection_error)
                if state == "owned":
                    try:
                        self._quarantine_delete_owned_path(
                            path, token, ownership_kind="publication"
                        )
                    except FileNotFoundError:
                        state = "absent"
                    except BaseException as retry_error:
                        rollback_errors.append(retry_error)
                        state, inspection_error = self._publication_path_state(
                            path, token
                        )
                        if inspection_error is not None:
                            rollback_errors.append(inspection_error)
                    else:
                        state = "absent"
                if state == "external":
                    protected_paths.add(path)
                elif state == "unknown":
                    blocked_paths.add(path)
        for source, quarantine, token, before_bytes in reversed(reservations):
            if source in protected_paths:
                rollback_errors.append(
                    MutationPlanConflict(
                        "workflow rollback source retained in quarantine because "
                        "its canonical path has an external winner"
                    )
                )
                continue
            if source in blocked_paths or source.exists() or source.is_symlink():
                rollback_errors.append(
                    MutationPlanConflict(
                        "workflow rollback source retained in quarantine because "
                        "its canonical path could not be proven absent"
                    )
                )
                continue
            try:
                _rename_no_replace(quarantine, source, self._root)
                restored_bytes, restored_token = (
                    self._read_regular_file_with_token(source)
                )
                if restored_bytes != before_bytes or restored_token != token:
                    raise RuntimeError(
                        "workflow rollback restored unexpected inode"
                    )
            except BaseException as error:
                protected_paths.add(source)
                rollback_errors.append(error)
        try:
            if use_document_map:
                restored_documents, _ = self._repository_document_snapshot()
                restored_errors = (
                    before_errors
                    if before_documents is not None
                    and restored_documents == before_documents
                    else set(validate_document_map(restored_documents))
                )
            else:
                restored_errors = set(validate_repository(self._root))
            if restored_errors != before_errors:
                rollback_errors.append(
                    RuntimeError("workflow rollback validation differs")
                )
        except BaseException as error:
            rollback_errors.append(error)
        if rollback_errors:
            raise WorkflowRollbackError(
                tuple(rollback_errors),
                tuple(sorted(protected_paths, key=lambda path: path.as_posix())),
            )

    def _publication_path_state(
        self, path: pathlib.Path, expected_token: PublicationToken
    ) -> tuple[str, BaseException | None]:
        """Classify a publication name after cleanup without trusting bytes."""
        try:
            current_stat = os.lstat(path)
        except FileNotFoundError:
            return "absent", None
        except OSError as error:
            return "unknown", error
        if stat.S_ISLNK(current_stat.st_mode):
            return "external", None
        if not stat.S_ISREG(current_stat.st_mode):
            return "external", None
        try:
            _, actual_token = self._read_regular_file_with_token(path)
        except BaseException as error:
            try:
                os.lstat(path)
            except FileNotFoundError:
                return "absent", None
            except OSError as inspection_error:
                return "unknown", inspection_error
            return "unknown", error
        if actual_token == expected_token:
            return "owned", None
        return "external", None

    def _plan_order_aware_create_locked(
        self,
        entity_type: str,
        fields: dict[str, str],
        body: str,
        review_kind: str | None,
        requested_id: str | None,
    ) -> MutationPlan | WorkflowPlan:
        """Normalize a legacy Project lane in the same atomic create workflow."""
        plan = self._plan_create_entity_locked(
            entity_type, fields, body, review_kind, requested_id
        )
        if entity_type != "project":
            return plan
        effect = self._workflow_effect_from_mutation_plan("project_created", plan)
        effects = self._normalize_project_order_effects(
            (effect,), plan.planned_entity.frontmatter["updated_at"]
        )
        if len(effects) == 1 and effects[0].after_bytes == plan.after_bytes:
            return plan
        return self._build_roadmap_workflow_plan(
            "project_create", effects[0].planned_entity, effects, {}
        )

    def _plan_create_entity_locked(
        self,
        entity_type: str,
        fields: dict[str, str],
        body: str,
        review_kind: str | None,
        requested_id: str | None,
        *,
        reserved_ids: set[str] | None = None,
    ) -> MutationPlan:
        self._validate_mutation_fields(entity_type, fields, create=True)
        if entity_type == "purpose":
            self._require_purpose_slot_available()
        if not isinstance(body, str):
            raise InputError("body must be a string")
        if entity_type == "review":
            if review_kind not in {"daily", "weekly"}:
                raise InputError("review_kind must be daily or weekly for review")
        elif review_kind is not None:
            raise InputError("review_kind is only supported for review")

        if requested_id is not None and (
            entity_type != "task"
            or type(requested_id) is not str
            or _CALENDAR_IMPORT_TASK_ID_PATTERN.fullmatch(requested_id) is None
        ):
            raise InputError("requested create ID is invalid")

        now = current_time()
        timestamp = now.isoformat(timespec="seconds")
        if requested_id is None:
            entity_id = self.next_entity_id(
                entity_type,
                review_kind=review_kind,
                on_date=now.date(),
            )
            while reserved_ids is not None and entity_id in reserved_ids:
                prefix, _, suffix = entity_id.rpartition("-")
                next_suffix = int(suffix) + 1
                if next_suffix > 999:
                    raise InputError(f"entity id suffix exhausted for {prefix}")
                entity_id = f"{prefix}-{next_suffix:03d}"
        else:
            entity_id = requested_id
            try:
                self._find_mutation_entity(entity_id)
            except NotFoundError:
                pass
            else:
                raise DestinationConflict("requested entity ID is unavailable")
        frontmatter = dict(fields)
        if entity_type == "review":
            period_start = frontmatter.get("period_start")
            if not period_start:
                raise InputError("missing required field: period_start")
            frontmatter.setdefault(
                "title", f"{period_start} {review_kind.title()} Review"
            )
            frontmatter = {
                "id": entity_id,
                "type": entity_type,
                **frontmatter,
                "review_kind": review_kind,
                "created_at": timestamp,
            }
        else:
            frontmatter = {
                "id": entity_id,
                "type": entity_type,
                **frontmatter,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            if entity_type in {
                "task", "vision", "project", "goal", "roadmap_outcome", "cycle"
            }:
                if entity_type == "task":
                    default_status = "inbox"
                elif entity_type == "cycle":
                    default_status = "planned"
                elif entity_type == "project":
                    default_status = "not_started"
                else:
                    default_status = "active"
                frontmatter["status"] = fields.get("status", default_status)
            if entity_type == "progress":
                frontmatter["visibility"] = "private"
        if entity_type == "task" and frontmatter.get("status") == "next":
            if not self._task_available_from_reached(frontmatter, now):
                frontmatter["status"] = "waiting"
        if entity_type == "project" and fields.get("status") == "active":
            raise InputError("legacy active Project status cannot be written")
        if entity_type == "project":
            lane = self._effective_project_lane(frontmatter.get("status"))
            if lane in _PROJECT_KANBAN_LANES:
                existing = self._project_lane_sequences()[lane]
                positions = [
                    entity.frontmatter.get("kanban_position") for entity in existing
                ]
                if not existing:
                    frontmatter["kanban_position"] = "1"
                elif all(positions) and sorted(int(value) for value in positions if value) == list(
                    range(1, len(existing) + 1)
                ):
                    frontmatter["kanban_position"] = str(len(existing) + 1)
        self._validate_complete_frontmatter(frontmatter, entity_type)
        self._validate_links(frontmatter, entity_type)
        if entity_type == "task":
            self._task_dependencies(frontmatter)
        self._validate_roadmap_crud_state(frontmatter)
        destination = self._canonical_destination(frontmatter)
        if destination.is_symlink() or destination.exists():
            raise DestinationConflict("planned destination is unavailable")
        data = self._canonical_document(frontmatter, entity_type, body)
        operation_inputs = (
            (
                ("review_kind", review_kind or ""),
                ("body", body),
                ("requested_id", requested_id or ""),
            )
            + tuple(
                (f"field:{key}", value)
                for key, value in sorted(fields.items())
            )
        )
        return self._build_plan(
            action="create",
            entity_id=entity_id,
            entity_type=entity_type,
            source=None,
            destination=destination,
            base_hash=None,
            before_bytes=None,
            before_entity=None,
            after_bytes=data,
            operation_inputs=operation_inputs,
        )

    def _plan_dependency_aware_update_locked(
        self,
        entity_id: str,
        base_hash: str,
        fields: dict[str, str],
        body: str | None,
    ) -> MutationPlan | WorkflowPlan:
        current = self._require_mutation_current(entity_id, base_hash)
        adjusted_fields = dict(fields)
        adjusted_body = body
        primary_role: str | None = None
        update_timestamp = (
            current_time().isoformat(timespec="seconds")
            if current.entity_type
            in {
                "task", "purpose", "vision", "area", "project", "goal",
                "roadmap_outcome", "cycle",
            }
            else None
        )
        dependency_changed = False
        if current.entity_type == "task" and "depends_on" in adjusted_fields:
            current_dependencies = parse_inline_list(
                current.frontmatter.get("depends_on", "[]")
            )
            proposed_dependencies = parse_inline_list(adjusted_fields["depends_on"])
            dependency_changed = (
                current_dependencies is not None
                and proposed_dependencies is not None
                and current_dependencies != proposed_dependencies
            )
            if (
                not dependency_changed
                and "depends_on" not in current.frontmatter
                and proposed_dependencies == []
            ):
                adjusted_fields.pop("depends_on")
        if current.entity_type == "task":
            projected = dict(current.frontmatter)
            for key, value in adjusted_fields.items():
                if key in OPTIONAL_KEYS["task"] and value == "":
                    projected.pop(key, None)
                else:
                    projected[key] = value
            assert update_timestamp is not None
            now = datetime.datetime.fromisoformat(update_timestamp)
            current_has_automatic_gate = self._task_has_automatic_gate(
                dict(current.frontmatter)
            )
            projected_has_automatic_gate = self._task_has_automatic_gate(projected)
            release_gates_satisfied = self._task_release_gates_satisfied(
                projected, now
            )
            projected_status = projected.get("status")
            if (
                projected_status != "done"
                and dependency_changed
                and not self._dependencies_satisfied(projected)
            ):
                adjusted_fields["status"] = (
                    "planned"
                    if projected_status == "planned"
                    or current.frontmatter.get("status") == "planned"
                    else "waiting"
                )
                primary_role = "dependency_retargeted"
            elif projected_status == "next" and not release_gates_satisfied:
                adjusted_fields["status"] = "waiting"
                if dependency_changed:
                    primary_role = "dependency_retargeted"
            elif (
                projected_status == "waiting"
                and (current_has_automatic_gate or projected_has_automatic_gate)
                and release_gates_satisfied
            ):
                adjusted_fields["status"] = "next"
                primary_role = (
                    "dependency_released"
                    if dependency_changed
                    else "availability_released"
                )
            elif (
                projected_status == "planned"
                and dependency_changed
                and projected_has_automatic_gate
                and release_gates_satisfied
            ):
                adjusted_fields["status"] = "next"
                primary_role = "dependency_released"
            elif dependency_changed:
                primary_role = "dependency_retargeted"

        plan = self._plan_update_entity_locked(
            entity_id,
            base_hash,
            adjusted_fields,
            adjusted_body,
            timestamp=update_timestamp,
        )
        action_date_removed = (
            current.entity_type == "task"
            and bool(current.frontmatter.get("action_date"))
            and current.frontmatter.get("status")
            != plan.planned_entity.frontmatter.get("status")
            and plan.planned_entity.frontmatter.get("status") in {"inbox", "planned"}
        )
        if primary_role is None and action_date_removed:
            primary_role = "action_date_removed"
        if (
            current.entity_type == "project"
            and plan.planned_entity.frontmatter.get("status")
            != current.frontmatter.get("status")
        ):
            status = self._effective_project_lane(
                plan.planned_entity.frontmatter.get("status")
            )
            if status not in _PROJECT_KANBAN_LANES:
                raise ProjectOperationError("project_not_movable")
            lanes = self._project_lane_sequences()
            destination = [
                entity for entity in lanes[status]
                if entity.entity_id != current.entity_id
            ]
            assert update_timestamp is not None
            effects = self._project_order_effects(
                current,
                status,
                len(destination) + 1,
                update_timestamp,
                target_role="project_status_changed",
                target_frontmatter=dict(plan.planned_entity.frontmatter),
                target_body=plan.planned_entity.body,
            )
            if len(effects) == 1:
                return self._build_plan(
                    action="update",
                    entity_id=current.entity_id,
                    entity_type="project",
                    source=self._root / current.relative_path,
                    destination=self._root / effects[0].destination_relative_path,
                    base_hash=current.content_hash,
                    before_bytes=effects[0].before_bytes,
                    before_entity=current,
                    after_bytes=effects[0].after_bytes,
                    operation_inputs=plan.operation_inputs,
                )
            return self._build_roadmap_workflow_plan(
                "project_status_change",
                current,
                effects,
                {"status": status},
            )
        becomes_done = (
            current.entity_type == "task"
            and current.frontmatter.get("status") != "done"
            and plan.planned_entity.frontmatter.get("status") == "done"
        )
        if primary_role is None and not becomes_done:
            return plan

        role = "completed" if becomes_done else primary_role
        assert role is not None
        primary = self._workflow_effect_from_entity(
            role,
            current,
            dict(plan.planned_entity.frontmatter),
            plan.planned_entity.body,
        )
        timestamp = plan.planned_entity.frontmatter.get("updated_at", "")
        effects: tuple[WorkflowEffect, ...] = (primary,)
        if becomes_done:
            effects += self._dependency_release_effects(effects, timestamp)
            if len(effects) == 1 and primary_role is None:
                return plan
        doing = self._current_doing_task()
        return self._build_unvalidated_workflow_plan(
            action="update",
            target=current,
            active=None,
            resolution=None,
            expected_doing_id="" if doing is None else doing.entity_id,
            timestamp=timestamp,
            effects=effects,
        )

    def _plan_update_entity_locked(
        self,
        entity_id: str,
        base_hash: str,
        fields: dict[str, str],
        body: str | None,
        *,
        timestamp: str | None = None,
    ) -> MutationPlan:
        if body is not None and not isinstance(body, str):
            raise InputError("body must be a string or None")
        current = self._require_mutation_current(entity_id, base_hash)
        if current.entity_type == "time_allocation_plan":
            raise InputError("Time Allocation Plans require dedicated workflows")
        if current.relative_path.startswith("archive/"):
            raise InputError("archived entities cannot be updated")
        if (
            current.entity_type == "task"
            and current.frontmatter.get("status") != "doing"
            and fields.get("status") == "doing"
        ):
            raise InputError("Task start requires the dedicated start workflow")
        if current.entity_type == "project":
            requested_status = fields.get("status")
            if current.frontmatter.get("status") == "active":
                if requested_status not in {"not_started", "doing"}:
                    raise InputError(
                        "legacy active Project must migrate to not_started or doing"
                    )
            elif requested_status == "active":
                raise InputError("legacy active Project status cannot be written")
        if current.entity_type == "cycle" and current.frontmatter.get("status") != "planned":
            raise RoadmapOperationError("cycle_membership_frozen", 409)
        self._validate_mutation_fields(current.entity_type, fields, create=False)
        frontmatter = dict(current.frontmatter)
        for key, value in fields.items():
            if key in OPTIONAL_KEYS[current.entity_type] and value == "":
                frontmatter.pop(key, None)
            else:
                frontmatter[key] = value
        if (
            current.entity_type == "task"
            and current.frontmatter.get("status") != frontmatter.get("status")
            and frontmatter.get("status") in {"inbox", "planned"}
        ):
            frontmatter.pop("action_date", None)
        if current.entity_type == "project":
            self._validate_project_transition(current, frontmatter)
        if current.entity_type in {
            "task", "purpose", "vision", "area", "project", "goal",
            "roadmap_outcome", "cycle", "progress",
        }:
            frontmatter["updated_at"] = (
                current_time().isoformat(timespec="seconds")
                if timestamp is None
                else timestamp
            )
        self._validate_complete_frontmatter(frontmatter, current.entity_type)
        self._validate_links(frontmatter, current.entity_type)
        if current.entity_type == "task":
            if (
                current.frontmatter.get("status") == "doing"
                and frontmatter.get("project_id")
                != current.frontmatter.get("project_id")
                and frontmatter.get("project_id")
            ):
                linked_project = self._find_mutation_entity(
                    frontmatter["project_id"]
                )
                if linked_project.frontmatter.get("status") != "doing":
                    raise ProjectOperationError("project_not_executable")
            self._task_dependencies(frontmatter)
        self._validate_roadmap_crud_state(
            frontmatter,
            exclude_entity_id=current.entity_id,
            previous_frontmatter=current.frontmatter,
        )
        source = self._root / current.relative_path
        destination = source
        if current.entity_type == "task":
            destination = self._canonical_destination(frontmatter)
        if destination != source and (
            destination.is_symlink() or destination.exists()
        ):
            raise DestinationConflict("planned destination is unavailable")
        source_bytes, _ = self._read_regular_file_with_token(source)
        if hashlib.sha256(source_bytes).hexdigest() != base_hash:
            raise ConflictError(self._find_mutation_entity(entity_id))
        if body is None:
            data = (
                serialize_frontmatter(frontmatter, current.entity_type)
                + current.body
            ).encode("utf-8")
        else:
            data = self._canonical_document(frontmatter, current.entity_type, body)
        operation_inputs = (
            (
                ("body_mode", "preserve" if body is None else "replace"),
                ("body", "" if body is None else body),
            )
            + tuple(
                (f"field:{key}", value)
                for key, value in sorted(fields.items())
            )
        )
        return self._build_plan(
            action="update",
            entity_id=current.entity_id,
            entity_type=current.entity_type,
            source=source,
            destination=destination,
            base_hash=base_hash,
            before_bytes=source_bytes,
            before_entity=current,
            after_bytes=data,
            operation_inputs=operation_inputs,
        )

    def _plan_order_aware_archive_locked(
        self,
        entity_id: str,
        base_hash: str,
    ) -> MutationPlan | WorkflowPlan:
        plan = self._plan_archive_entity_locked(entity_id, base_hash)
        current = plan.before_entity
        if current is not None and current.entity_type == "task":
            dependents = self._active_task_dependency_referrers(current.entity_id)
            if dependents:
                timestamp = current_time().isoformat(timespec="seconds")
                promoted_parent_id = self._task_archive_promoted_parent(current)
                effects = [
                    self._workflow_effect_from_mutation_plan("task_archived", plan)
                ]
                for dependent in dependents:
                    role, frontmatter = self._task_archive_dependency_rewrite(
                        current,
                        dependent,
                        promoted_parent_id,
                        timestamp,
                    )
                    effects.append(
                        self._workflow_effect_from_entity(
                            role, dependent, frontmatter, dependent.body
                        )
                    )
                encoded_dependents = [
                    {
                        "id": dependent.entity_id,
                        "base_hash": dependent.content_hash,
                    }
                    for dependent in dependents
                ]
                return self._build_roadmap_workflow_plan(
                    "archive",
                    current,
                    tuple(effects),
                    {
                        "dependents": json.dumps(
                            encoded_dependents,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "timestamp": timestamp,
                    },
                )
        if current is None or current.entity_type != "project":
            return plan
        source_lane = self._effective_project_lane(current.frontmatter.get("status"))
        if source_lane not in _PROJECT_KANBAN_LANES:
            return plan
        remaining = [
            entity
            for entity in self._project_lane_sequences()[source_lane]
            if entity.entity_id != current.entity_id
        ]
        timestamp = current_time().isoformat(timespec="seconds")
        neighbours: list[WorkflowEffect] = []
        for index, neighbour in enumerate(remaining, start=1):
            if neighbour.frontmatter.get("kanban_position") == str(index):
                continue
            frontmatter = dict(neighbour.frontmatter)
            frontmatter["kanban_position"] = str(index)
            frontmatter["updated_at"] = timestamp
            neighbours.append(
                self._workflow_effect_from_entity(
                    "project_reordered", neighbour, frontmatter, neighbour.body
                )
            )
        if not neighbours:
            return plan
        effects = (
            self._workflow_effect_from_mutation_plan("project_archived", plan),
            *sorted(neighbours, key=lambda effect: effect.entity_id),
        )
        return self._build_roadmap_workflow_plan(
            "project_archive", current, effects, {}
        )

    def _active_task_dependency_referrers(
        self, dependency_id: str
    ) -> tuple[Entity, ...]:
        """Return every active Task whose dependency list names one Task."""
        referrers: list[Entity] = []
        for directory in ("inbox", "tasks"):
            scan_root = self._root / directory
            if scan_root.is_symlink() or not scan_root.is_dir():
                continue
            for path in self._walk_directory(scan_root):
                try:
                    entity = self._read_entity(path)
                except InputError:
                    continue
                if entity is None or entity.entity_id == dependency_id:
                    continue
                dependencies = parse_inline_list(
                    entity.frontmatter.get("depends_on", "")
                )
                if dependencies and dependency_id in dependencies:
                    self._task_dependencies(dict(entity.frontmatter))
                    referrers.append(entity)
        return tuple(sorted(referrers, key=lambda entity: entity.entity_id))

    def _task_archive_promoted_parent(self, archived: Entity) -> str | None:
        """Resolve only a same-Project primary dependency as a tree parent."""
        project_id = archived.frontmatter.get("project_id")
        dependencies = self._task_dependencies(dict(archived.frontmatter))
        if not project_id or not dependencies:
            return None
        try:
            parent = self._find_mutation_entity(dependencies[0])
        except NotFoundError:
            return None
        if (
            parent.entity_type != "task"
            or parent.frontmatter.get("project_id") != project_id
        ):
            return None
        return parent.entity_id

    def _task_archive_dependency_rewrite(
        self,
        archived: Entity,
        dependent: Entity,
        promoted_parent_id: str | None,
        timestamp: str,
    ) -> tuple[str, dict[str, str]]:
        """Build one dependency rewrite without changing unrelated Task data."""
        dependencies = list(self._task_dependencies(dict(dependent.frontmatter)))
        structural_child = (
            bool(dependencies)
            and dependencies[0] == archived.entity_id
            and bool(archived.frontmatter.get("project_id"))
            and dependent.frontmatter.get("project_id")
            == archived.frontmatter.get("project_id")
        )
        replacements = (
            ([] if promoted_parent_id is None else [promoted_parent_id])
            if structural_child
            else []
        )
        retained = [
            dependency_id
            for dependency_id in dependencies
            if dependency_id != archived.entity_id
        ]
        rewritten: list[str] = []
        for dependency_id in (*replacements, *retained):
            if dependency_id not in rewritten:
                rewritten.append(dependency_id)

        frontmatter = dict(dependent.frontmatter)
        if rewritten:
            frontmatter["depends_on"] = self._inline_list(rewritten)
        else:
            frontmatter.pop("depends_on", None)
        role = "dependency_retargeted"
        now = datetime.datetime.fromisoformat(timestamp)
        if (
            dependent.frontmatter.get("status") in {"waiting", "planned"}
            and self._task_release_gates_satisfied(frontmatter, now)
        ):
            frontmatter["status"] = "next"
            role = "dependency_released"
        frontmatter["updated_at"] = timestamp
        return role, frontmatter

    def _plan_archive_entity_locked(
        self,
        entity_id: str,
        base_hash: str,
        *,
        ignored_child_ids: frozenset[str] = frozenset(),
    ) -> MutationPlan:
        current = self._require_mutation_current(entity_id, base_hash)
        if current.entity_type == "time_allocation_plan":
            raise InputError("Time Allocation Plans cannot be archived")
        if current.relative_path.startswith("archive/"):
            raise InputError("entity is already archived")
        if current.entity_type == "cycle" and current.frontmatter.get("status") == "active":
            raise RoadmapOperationError("cycle_membership_frozen", 409)
        if current.entity_type == "roadmap_outcome" and self._outcome_is_now(current.entity_id):
            raise RoadmapOperationError("cycle_membership_frozen", 409)
        self._require_no_active_children(current, ignored_child_ids=ignored_child_ids)
        source = self._root / current.relative_path
        source_bytes, _ = self._read_regular_file_with_token(source)
        if hashlib.sha256(source_bytes).hexdigest() != base_hash:
            raise ConflictError(self._find_mutation_entity(entity_id))
        archive_root = self._root / "archive"
        destination = archive_root / source.name
        suffix = 2
        while destination.is_symlink() or destination.exists():
            destination = archive_root / f"{source.stem}-{suffix}{source.suffix}"
            suffix += 1
        return self._build_plan(
            action="archive",
            entity_id=current.entity_id,
            entity_type=current.entity_type,
            source=source,
            destination=destination,
            base_hash=base_hash,
            before_bytes=source_bytes,
            before_entity=current,
            after_bytes=source_bytes,
            operation_inputs=(),
        )

    def _build_plan(
        self,
        *,
        action: str,
        entity_id: str,
        entity_type: str,
        source: pathlib.Path | None,
        destination: pathlib.Path,
        base_hash: str | None,
        before_bytes: bytes | None,
        before_entity: Entity | None,
        after_bytes: bytes,
        operation_inputs: tuple[tuple[str, str], ...],
    ) -> MutationPlan:
        destination_relative = destination.relative_to(self._root).as_posix()
        source_relative = (
            None if source is None else source.relative_to(self._root).as_posix()
        )
        planned_entity = self._entity_from_bytes(
            destination_relative, after_bytes
        )
        if planned_entity.entity_type == "purpose":
            _validate_purpose_body(planned_entity.body)
        self._validate_roadmap_body(planned_entity)
        validation_errors = self._preflight_document(
            destination_relative, after_bytes
        )
        if validation_errors:
            raise SchemaError(list(validation_errors))
        before_copy = self._copy_entity(before_entity)
        planned_copy = self._copy_entity(planned_entity)
        assert planned_copy is not None
        integrity_tag = self._mutation_plan_integrity_tag(
            action=action,
            entity_id=entity_id,
            entity_type=entity_type,
            source_relative_path=source_relative,
            destination_relative_path=destination_relative,
            base_hash=base_hash,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            operation_inputs=operation_inputs,
            validation_errors=validation_errors,
        )
        return MutationPlan(
            action=action,
            entity_id=entity_id,
            entity_type=entity_type,
            source_relative_path=source_relative,
            destination_relative_path=destination_relative,
            base_hash=base_hash,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            before_entity=before_copy,
            planned_entity=planned_copy,
            operation_inputs=operation_inputs,
            validation_errors=validation_errors,
            integrity_tag=integrity_tag,
        )

    def _validate_roadmap_body(self, entity: Entity) -> None:
        required = {
            "roadmap_outcome": ("## 成功条件", "## メモ"),
            "cycle": ("## 振り返り", "## メモ"),
        }.get(entity.entity_type)
        if required is None:
            return
        lines = entity.body.splitlines()
        missing = [heading for heading in required if heading not in lines]
        if missing:
            raise SchemaError(
                [
                    f"{entity.relative_path}: missing required body heading: {heading}"
                    for heading in missing
                ]
            )

    def _apply_mutation_plan_locked(self, plan: MutationPlan) -> Entity:
        self._require_mutations_available_locked()
        self._validate_mutation_plan(plan)
        if plan.entity_type == "purpose":
            _validate_purpose_body(plan.planned_entity.body)
        if plan.action != "archive":
            self._validate_links(
                dict(plan.planned_entity.frontmatter), plan.entity_type
            )
            if plan.entity_type == "task" and plan.before_entity is not None:
                self._require_doing_task_project_status(
                    dict(plan.planned_entity.frontmatter)
                )
            self._validate_roadmap_crud_state(
                dict(plan.planned_entity.frontmatter),
                exclude_entity_id=(
                    None if plan.action == "create" else plan.entity_id
                ),
                previous_frontmatter=(
                    plan.before_entity.frontmatter
                    if plan.action == "update" and plan.before_entity is not None
                    else None
                ),
            )
        if plan.action == "create" and plan.entity_type == "purpose":
            self._require_purpose_slot_available()
        destination = self._relative_plan_path(plan.destination_relative_path)
        source = (
            None
            if plan.source_relative_path is None
            else self._relative_plan_path(plan.source_relative_path)
        )

        if plan.action == "create":
            try:
                self._find_mutation_entity(plan.entity_id)
            except NotFoundError:
                pass
            else:
                raise DestinationConflict("planned entity ID is unavailable")
            if destination.is_symlink() or destination.exists():
                raise DestinationConflict("planned destination is unavailable")
            before_errors = set(validate_repository(self._root))
            self._ensure_create_parent_locked(destination)
            try:
                self._find_mutation_entity(plan.entity_id)
            except NotFoundError:
                pass
            else:
                raise DestinationConflict("planned entity ID is unavailable")
            if destination.is_symlink() or destination.exists():
                raise DestinationConflict("planned destination is unavailable")
            publication_tokens: dict[pathlib.Path, PublicationToken] = {}
            return self._commit_mutation(
                {destination: None},
                before_errors,
                destination,
                plan.entity_id,
                publication_tokens,
                lambda: self._publish_no_replace(
                    destination, plan.after_bytes, publication_tokens
                ),
            )

        assert source is not None
        if destination != source and (
            destination.is_symlink() or destination.exists()
        ):
            raise DestinationConflict("planned destination is unavailable")
        try:
            current = self._find_mutation_entity(plan.entity_id)
        except NotFoundError as error:
            raise MutationPlanConflict("planned source is no longer current") from error
        if (
            current.entity_type != plan.entity_type
            or current.relative_path != plan.source_relative_path
            or current.content_hash != plan.base_hash
        ):
            raise ConflictError(current)
        if plan.action == "update" and plan.entity_type == "project":
            self._validate_project_transition(
                current, dict(plan.planned_entity.frontmatter)
            )
        if plan.action == "archive":
            self._require_no_active_children(current)
        before_errors = set(validate_repository(self._root))
        if destination != source and (
            destination.is_symlink() or destination.exists()
        ):
            raise DestinationConflict("planned destination is unavailable")
        try:
            latest = self._find_mutation_entity(plan.entity_id)
        except NotFoundError as error:
            raise MutationPlanConflict("planned source is no longer current") from error
        if (
            latest.entity_type != plan.entity_type
            or latest.relative_path != plan.source_relative_path
            or latest.content_hash != plan.base_hash
        ):
            raise ConflictError(latest)
        if destination != source and (
            destination.is_symlink() or destination.exists()
        ):
            raise DestinationConflict("planned destination is unavailable")
        source_bytes, source_token = self._read_regular_file_with_token(source)
        if source_bytes != plan.before_bytes:
            try:
                changed = self._find_mutation_entity(plan.entity_id)
            except NotFoundError as error:
                raise MutationPlanConflict(
                    "planned source is no longer current"
                ) from error
            raise ConflictError(changed)

        if destination == source:
            return self._commit_same_path_plan_update(
                plan,
                source,
                source_bytes,
                source_token,
                before_errors,
            )

        snapshots: dict[pathlib.Path, bytes | None] = {source: source_bytes}
        snapshots[destination] = None
        publication_tokens: dict[pathlib.Path, PublicationToken] = {}

        def operation() -> None:
            self._publish_no_replace(
                destination, plan.after_bytes, publication_tokens
            )
            self._quarantine_delete_owned_path(
                source, source_token, ownership_kind="source"
            )

        return self._commit_mutation(
            snapshots,
            before_errors,
            destination,
            plan.entity_id,
            publication_tokens,
            operation,
        )

    def _commit_same_path_plan_update(
        self,
        plan: MutationPlan,
        path: pathlib.Path,
        source_bytes: bytes,
        source_token: PublicationToken,
        before_errors: set[str],
    ) -> Entity:
        """CAS one same-path update without ever replacing an unknown inode."""
        quarantine = self._reserve_owned_source_for_replace(path, source_token)
        publication_tokens: dict[pathlib.Path, PublicationToken] = {}
        try:
            try:
                self._publish_no_replace(
                    path, plan.after_bytes, publication_tokens
                )
            except DestinationCollision as error:
                self._discard_reserved_source(quarantine, source_token)
                raise DestinationConflict(
                    "planned source changed during apply"
                ) from error

            published_token = publication_tokens.get(path)
            if published_token is None:
                raise RuntimeError("planned update publication token is missing")
            after_errors = set(validate_repository(self._root))
            new_errors = sorted(after_errors - before_errors)
            if new_errors:
                raise SchemaError(new_errors)

            result_bytes, result_token = self._read_regular_file_with_token(path)
            if result_token != published_token or result_bytes != plan.after_bytes:
                self._discard_reserved_source(quarantine, source_token)
                raise MutationPlanConflict(
                    "planned source changed during apply"
                )
            result = self._entity_from_bytes(
                plan.destination_relative_path, result_bytes
            )
            if (
                result.entity_id != plan.entity_id
                or result.content_hash
                != hashlib.sha256(plan.after_bytes).hexdigest()
            ):
                raise InputError("mutation result does not match planned bytes")

            self._discard_reserved_source(quarantine, source_token)
            return result
        except (DestinationConflict, MutationPlanConflict):
            raise
        except BaseException as primary_error:
            published_token = publication_tokens.get(path)
            if published_token is not None:
                try:
                    _, current_token = self._read_regular_file_with_token(path)
                except BaseException as inspect_error:
                    raise RuntimeError(
                        "same-path rollback could not verify current ownership; "
                        "prior inode retained in quarantine"
                    ) from primary_error
                if current_token != published_token:
                    try:
                        self._discard_reserved_source(quarantine, source_token)
                    except BaseException as cleanup_error:
                        raise RuntimeError(
                            "same-path conflict cleanup failed; prior inode "
                            "retained in quarantine"
                        ) from cleanup_error
                    raise MutationPlanConflict(
                        "planned source changed during apply"
                    ) from primary_error
            try:
                self._rollback_same_path_plan_update(
                    path,
                    quarantine,
                    source_bytes,
                    source_token,
                    published_token,
                    before_errors,
                )
            except BaseException as rollback_error:
                if isinstance(rollback_error, MutationPlanConflict):
                    raise rollback_error from primary_error
                raise RuntimeError(
                    "mutation rollback failed; repository state retained for "
                    f"recovery: {rollback_error}"
                ) from primary_error
            raise

    def _reserve_owned_source_for_replace(
        self, path: pathlib.Path, expected_token: PublicationToken
    ) -> pathlib.Path:
        """Move the exact source inode aside atomically, restoring any winner."""
        for _ in range(32):
            quarantine = path.parent / (
                f".{path.name}.{secrets.token_hex(12)}.quarantine"
            )
            post_effect_error: RenamePostEffectError | None = None
            try:
                _rename_no_replace(path, quarantine, self._root)
            except RenamePostEffectError as error:
                post_effect_error = error
            except DestinationCollision:
                continue
            try:
                _, actual_token = self._read_regular_file_with_token(quarantine)
            except BaseException as inspect_error:
                self._restore_quarantine_or_raise(
                    path,
                    quarantine,
                    inspect_error,
                    ownership_kind="source",
                )
                raise MutationPlanConflict(
                    "planned source ownership could not be verified"
                ) from inspect_error
            if actual_token != expected_token:
                mismatch = RuntimeError("planned source ownership changed")
                self._restore_quarantine_or_raise(
                    path,
                    quarantine,
                    mismatch,
                    ownership_kind="source",
                )
                raise MutationPlanConflict(
                    "planned source changed during apply"
                ) from mismatch
            if post_effect_error is not None:
                self._restore_quarantine_or_raise(
                    path,
                    quarantine,
                    post_effect_error,
                    ownership_kind="source",
                )
                raise post_effect_error
            return quarantine
        raise RuntimeError("could not allocate same-path reservation")

    def _discard_reserved_source(
        self, quarantine: pathlib.Path, expected_token: PublicationToken
    ) -> None:
        self._quarantine_delete_owned_path(
            quarantine,
            expected_token,
            ownership_kind="publication",
        )

    def _rollback_same_path_plan_update(
        self,
        path: pathlib.Path,
        quarantine: pathlib.Path,
        source_bytes: bytes,
        source_token: PublicationToken,
        published_token: PublicationToken | None,
        before_errors: set[str],
    ) -> None:
        if published_token is not None:
            try:
                self._quarantine_delete_owned_path(
                    path,
                    published_token,
                    ownership_kind="source",
                )
            except FileNotFoundError:
                pass
            except SourceOwnershipConflict as conflict:
                try:
                    self._discard_reserved_source(quarantine, source_token)
                except BaseException as cleanup_error:
                    raise RuntimeError(
                        "same-path conflict cleanup failed; prior inode "
                        "retained in quarantine"
                    ) from cleanup_error
                raise MutationPlanConflict(
                    "planned source changed during apply"
                ) from conflict
        elif path.exists() or path.is_symlink():
            try:
                self._discard_reserved_source(quarantine, source_token)
            except BaseException as cleanup_error:
                raise RuntimeError(
                    "same-path conflict cleanup failed; prior inode retained "
                    "in quarantine"
                ) from cleanup_error
            raise MutationPlanConflict("planned source changed during apply")

        try:
            _rename_no_replace(quarantine, path, self._root)
        except DestinationCollision as conflict:
            try:
                self._discard_reserved_source(quarantine, source_token)
            except BaseException as cleanup_error:
                raise RuntimeError(
                    "same-path conflict cleanup failed; prior inode retained "
                    "in quarantine"
                ) from cleanup_error
            raise MutationPlanConflict(
                "planned source changed during apply"
            ) from conflict
        restored_bytes, restored_token = self._read_regular_file_with_token(path)
        if restored_token != source_token or restored_bytes != source_bytes:
            raise RuntimeError("same-path rollback restored unexpected inode")
        restored_errors = set(validate_repository(self._root))
        if restored_errors != before_errors:
            added = sorted(restored_errors - before_errors)
            removed = sorted(before_errors - restored_errors)
            raise RuntimeError(
                "restored repository validation differs; "
                f"added={added}; removed={removed}"
            )

    def _validate_mutation_plan(self, plan: MutationPlan) -> None:
        if type(plan) is not MutationPlan:
            raise InputError("invalid mutation plan type")
        if plan.action not in {"create", "update", "archive"}:
            raise InputError("invalid mutation plan action")
        if plan.entity_type not in REQUIRED_KEYS:
            raise InputError("invalid mutation plan entity type")
        if not isinstance(plan.entity_id, str):
            raise InputError("invalid mutation plan entity ID")
        if not isinstance(plan.after_bytes, bytes):
            raise InputError("invalid mutation plan after bytes")
        if plan.before_bytes is not None and not isinstance(plan.before_bytes, bytes):
            raise InputError("invalid mutation plan before bytes")
        if not isinstance(plan.operation_inputs, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
            for item in plan.operation_inputs
        ):
            raise InputError("invalid mutation plan operation inputs")
        if not isinstance(plan.validation_errors, tuple) or plan.validation_errors:
            raise InputError("mutation plan contains validation errors")
        if (
            not isinstance(plan.integrity_tag, str)
            or _CONTENT_HASH_PATTERN.fullmatch(plan.integrity_tag) is None
        ):
            raise InputError("invalid mutation plan integrity tag")

        destination = self._relative_plan_path(plan.destination_relative_path)
        source = (
            None
            if plan.source_relative_path is None
            else self._relative_plan_path(plan.source_relative_path)
        )
        parsed_after = self._entity_from_bytes(
            plan.destination_relative_path, plan.after_bytes
        )
        if not self._entity_matches(parsed_after, plan.planned_entity):
            raise InputError("planned entity does not match exact after bytes")
        if (
            parsed_after.entity_id != plan.entity_id
            or parsed_after.entity_type != plan.entity_type
        ):
            raise InputError("planned entity identity is inconsistent")
        expected_destination = self._canonical_destination(
            parsed_after.frontmatter
        )
        if plan.action != "archive" and destination != expected_destination:
            raise InputError("planned destination is not canonical")

        if plan.action == "create":
            if (
                source is not None
                or plan.base_hash is not None
                or plan.before_bytes is not None
                or plan.before_entity is not None
            ):
                raise InputError("create plan has unexpected prior state")
        else:
            if source is None or plan.before_bytes is None or plan.before_entity is None:
                raise InputError("mutation plan is missing prior state")
            if not isinstance(plan.base_hash, str) or _CONTENT_HASH_PATTERN.fullmatch(
                plan.base_hash
            ) is None:
                raise InputError("mutation plan has invalid base hash")
            if hashlib.sha256(plan.before_bytes).hexdigest() != plan.base_hash:
                raise InputError("mutation plan base hash does not match before bytes")
            parsed_before = self._entity_from_bytes(
                plan.source_relative_path, plan.before_bytes
            )
            if not self._entity_matches(parsed_before, plan.before_entity):
                raise InputError("before entity does not match exact before bytes")
            if (
                parsed_before.entity_id != plan.entity_id
                or parsed_before.entity_type != plan.entity_type
            ):
                raise InputError("before entity identity is inconsistent")
            if plan.action == "archive":
                if plan.after_bytes != plan.before_bytes or plan.operation_inputs:
                    raise InputError("archive plan must preserve exact bytes")
                if destination.parent != self._root / "archive":
                    raise InputError("archive destination is not canonical")
                archive_pattern = re.compile(
                    rf"{re.escape(source.stem)}(?:-[2-9][0-9]*)?\.md"
                )
                if archive_pattern.fullmatch(destination.name) is None:
                    raise InputError("archive destination is not canonical")

        self._validate_plan_operation_consistency(plan)
        expected_integrity_tag = self._mutation_plan_integrity_tag(
            action=plan.action,
            entity_id=plan.entity_id,
            entity_type=plan.entity_type,
            source_relative_path=plan.source_relative_path,
            destination_relative_path=plan.destination_relative_path,
            base_hash=plan.base_hash,
            before_bytes=plan.before_bytes,
            after_bytes=plan.after_bytes,
            operation_inputs=plan.operation_inputs,
            validation_errors=plan.validation_errors,
        )
        if not secrets.compare_digest(expected_integrity_tag, plan.integrity_tag):
            raise InputError("mutation plan integrity check failed")

    def _validate_plan_operation_consistency(self, plan: MutationPlan) -> None:
        inputs: dict[str, str] = {}
        for key, value in plan.operation_inputs:
            if key in inputs:
                raise InputError("duplicate mutation plan operation input")
            inputs[key] = value
        fields = {
            key.removeprefix("field:"): value
            for key, value in inputs.items()
            if key.startswith("field:")
        }
        if len(fields) != sum(key.startswith("field:") for key in inputs):
            raise InputError("duplicate mutation plan field input")

        if plan.action == "archive":
            if inputs:
                raise InputError("archive plan has unexpected operation inputs")
            return
        if plan.action == "create":
            if set(inputs) != {
                "review_kind",
                "body",
                "requested_id",
                *(f"field:{key}" for key in fields),
            }:
                raise InputError("create plan operation inputs are incomplete")
            self._validate_mutation_fields(plan.entity_type, fields, create=True)
            review_kind = inputs["review_kind"] or None
            requested_id = inputs["requested_id"] or None
            if requested_id is not None and (
                plan.entity_type != "task"
                or _CALENDAR_IMPORT_TASK_ID_PATTERN.fullmatch(requested_id) is None
                or requested_id != plan.entity_id
            ):
                raise InputError("create plan requested ID is invalid")
            frontmatter = dict(fields)
            created_at = plan.planned_entity.frontmatter.get("created_at", "")
            if plan.entity_type == "review":
                if review_kind not in {"daily", "weekly"}:
                    raise InputError("create plan review kind is invalid")
                period_start = frontmatter.get("period_start")
                if not period_start:
                    raise InputError("create plan period start is missing")
                frontmatter.setdefault(
                    "title", f"{period_start} {review_kind.title()} Review"
                )
                frontmatter = {
                    "id": plan.entity_id,
                    "type": plan.entity_type,
                    **frontmatter,
                    "review_kind": review_kind,
                    "created_at": created_at,
                }
            else:
                if review_kind is not None:
                    raise InputError("create plan review kind is unexpected")
                frontmatter = {
                    "id": plan.entity_id,
                    "type": plan.entity_type,
                    **frontmatter,
                    "created_at": created_at,
                    "updated_at": created_at,
                }
                if plan.entity_type in {
                    "task", "vision", "project", "goal", "roadmap_outcome", "cycle"
                }:
                    if plan.entity_type == "task":
                        default_status = "inbox"
                    elif plan.entity_type == "cycle":
                        default_status = "planned"
                    elif plan.entity_type == "project":
                        default_status = "not_started"
                    else:
                        default_status = "active"
                    frontmatter["status"] = fields.get("status", default_status)
                if plan.entity_type == "progress":
                    frontmatter["visibility"] = "private"
                if plan.entity_type == "task" and frontmatter.get("status") == "next":
                    created_now = datetime.datetime.fromisoformat(created_at)
                    if not self._task_available_from_reached(frontmatter, created_now):
                        frontmatter["status"] = "waiting"
                if (
                    plan.entity_type == "project"
                    and plan.planned_entity.frontmatter.get("kanban_position")
                ):
                    frontmatter["kanban_position"] = plan.planned_entity.frontmatter[
                        "kanban_position"
                    ]
            expected = self._canonical_document(
                frontmatter, plan.entity_type, inputs["body"]
            )
        else:
            expected_keys = {
                "body_mode",
                "body",
                *(f"field:{key}" for key in fields),
            }
            if set(inputs) != expected_keys or inputs["body_mode"] not in {
                "preserve",
                "replace",
            }:
                raise InputError("update plan operation inputs are incomplete")
            assert plan.before_entity is not None
            self._validate_mutation_fields(plan.entity_type, fields, create=False)
            frontmatter = dict(plan.before_entity.frontmatter)
            for key, value in fields.items():
                if key in OPTIONAL_KEYS[plan.entity_type] and value == "":
                    frontmatter.pop(key, None)
                else:
                    frontmatter[key] = value
            if plan.entity_type in {
                "task", "purpose", "vision", "area", "project", "goal",
                "roadmap_outcome", "cycle", "progress",
            }:
                frontmatter["updated_at"] = plan.planned_entity.frontmatter.get(
                    "updated_at", ""
                )
            if (
                plan.entity_type == "project"
                and plan.planned_entity.frontmatter.get("kanban_position")
            ):
                frontmatter["kanban_position"] = plan.planned_entity.frontmatter[
                    "kanban_position"
                ]
            if plan.entity_type == "progress":
                frontmatter["visibility"] = "private"
            if inputs["body_mode"] == "preserve":
                if inputs["body"]:
                    raise InputError("preserved body input must be empty")
                expected = (
                    serialize_frontmatter(frontmatter, plan.entity_type)
                    + plan.before_entity.body
                ).encode("utf-8")
            else:
                expected = self._canonical_document(
                    frontmatter, plan.entity_type, inputs["body"]
                )
        if expected != plan.after_bytes:
            raise InputError("mutation plan does not match operation inputs")

    def _relative_plan_path(self, relative_path: str) -> pathlib.Path:
        if not isinstance(relative_path, str) or "\\" in relative_path:
            raise InputError("invalid mutation plan path")
        pure = pathlib.PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or pure.suffix != ".md"
            or pure.parts[0]
            not in {
                "inbox", "tasks", "purposes", "visions", "areas",
                "projects", "goals", "roadmap-outcomes", "cycles",
                "progress", "time-allocation-plans", "reviews", "archive"
            }
        ):
            raise InputError("invalid mutation plan path")
        candidate = self._root.joinpath(*pure.parts)
        try:
            resolved_root = self._root.resolve(strict=True)
            resolved_parent = candidate.parent.resolve(strict=True)
            resolved_parent.relative_to(resolved_root)
        except FileNotFoundError as error:
            if (
                len(pure.parts) != 2
                or pure.parts[0] not in {"roadmap-outcomes", "cycles", "progress"}
                or candidate.parent.is_symlink()
            ):
                raise InputError("invalid mutation plan path") from error
            try:
                if candidate.parent.parent.resolve(strict=True) != self._root.resolve(strict=True):
                    raise InputError("invalid mutation plan path")
            except (OSError, RuntimeError) as parent_error:
                raise InputError("invalid mutation plan path") from parent_error
        except (OSError, RuntimeError, ValueError) as error:
            raise InputError("invalid mutation plan path") from error
        if candidate.parent.is_symlink() or candidate.is_symlink():
            raise InputError("invalid mutation plan path")
        return candidate

    def _ensure_create_parent_locked(self, destination: pathlib.Path) -> None:
        """Create one missing fixed first-level Roadmap entity root durably."""
        parent = destination.parent
        if parent.exists():
            if parent.is_symlink() or not parent.is_dir():
                raise InputError("canonical entity directory is unavailable")
            return
        if parent.parent != self._root or parent.name not in {
            "roadmap-outcomes", "cycles", "progress"
        }:
            raise InputError("canonical entity directory is unavailable")
        descriptor = self._open_root_lock_directory()
        try:
            try:
                os.mkdir(parent.name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            result = os.stat(parent.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(result.st_mode):
                raise InputError("canonical entity directory is unavailable")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _preflight_document(
        self, relative_path: str, data: bytes
    ) -> tuple[str, ...]:
        frontmatter, _ = split_document(data.decode("utf-8"))
        parts = pathlib.PurePosixPath(relative_path).parts
        if parts[0] == "archive":
            contract = EntityContract(frontmatter.get("type", ""))
        elif parts[0] == "inbox":
            contract = EntityContract("task", task_location="inbox")
        elif parts[0] == "tasks":
            contract = EntityContract("task", task_location="tasks")
        elif parts[0] in {"purposes", "visions", "areas", "projects", "goals"}:
            contract = EntityContract(parts[0][:-1])
        elif parts[0] == "roadmap-outcomes":
            contract = EntityContract("roadmap_outcome")
        elif parts[0] == "cycles":
            contract = EntityContract("cycle")
        elif parts[0] == "progress":
            contract = EntityContract("progress")
        elif len(parts) >= 3 and parts[:2] in {
            ("reviews", "daily"),
            ("reviews", "weekly"),
        }:
            contract = EntityContract("review", review_kind=parts[1])
        else:
            return (f"{relative_path}: unsupported entity directory",)
        return tuple(
            f"{relative_path}: {error}"
            for error in _validate_values(frontmatter, contract)
        )

    @staticmethod
    def _copy_entity(entity: Entity | None) -> Entity | None:
        if entity is None:
            return None
        return dataclasses.replace(
            entity,
            frontmatter=types.MappingProxyType(dict(entity.frontmatter)),
        )

    @staticmethod
    def _entity_matches(first: Entity, second: Entity) -> bool:
        return (
            isinstance(second, Entity)
            and first.entity_id == second.entity_id
            and first.entity_type == second.entity_type
            and first.relative_path == second.relative_path
            and first.frontmatter == second.frontmatter
            and first.body == second.body
            and first.content_hash == second.content_hash
        )

    def _mutation_plan_integrity_tag(
        self,
        *,
        action: str,
        entity_id: str,
        entity_type: str,
        source_relative_path: str | None,
        destination_relative_path: str,
        base_hash: str | None,
        before_bytes: bytes | None,
        after_bytes: bytes,
        operation_inputs: tuple[tuple[str, str], ...],
        validation_errors: tuple[str, ...],
    ) -> str:
        digest = hmac.new(self._plan_integrity_key, digestmod=hashlib.sha256)
        values: tuple[bytes, ...] = (
            action.encode(),
            entity_id.encode(),
            entity_type.encode(),
            (source_relative_path or "").encode(),
            destination_relative_path.encode(),
            (base_hash or "").encode(),
            b"" if before_bytes is None else before_bytes,
            after_bytes,
            *(f"{key}\0{value}".encode() for key, value in operation_inputs),
            *(error.encode() for error in validation_errors),
        )
        for value in values:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    @staticmethod
    def _entity_from_bytes(relative_path: str, data: bytes) -> Entity:
        if not isinstance(data, bytes):
            raise InputError("entity bytes must be bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InputError("entity bytes must be UTF-8") from error
        frontmatter, body = split_document(text)
        entity_id = frontmatter.get("id", "")
        entity_type = frontmatter.get("type", "")
        if not entity_id or not entity_type:
            raise InputError("planned entity identity is missing")
        return Entity(
            entity_id=entity_id,
            entity_type=entity_type,
            relative_path=relative_path,
            frontmatter=dict(frontmatter),
            body=body,
            content_hash=hashlib.sha256(data).hexdigest(),
        )

    def repository_errors(self) -> list[str]:
        """Return sorted errors from the authoritative repository validator."""
        documents, generation = self._repository_document_snapshot()
        return list(
            self._validation_errors_for_document_snapshot(
                documents, generation
            )
        )

    @staticmethod
    def _publish_no_replace(
        path: pathlib.Path,
        data: bytes,
        publication_tokens: dict[pathlib.Path, PublicationToken],
    ) -> None:
        """Publish bytes and retain the exact inode token even on later error."""
        try:
            token = atomic_create_no_replace(path, data)
        except BaseException as error:
            token = getattr(error, "publication_token", None)
            if isinstance(token, PublicationToken):
                publication_tokens[path] = token
            raise
        if not isinstance(token, PublicationToken):
            raise RuntimeError(f"atomic create returned no publication token: {path}")
        publication_tokens[path] = token

    def _commit_mutation(
        self,
        snapshots: dict[pathlib.Path, bytes | None],
        before_errors: set[str],
        result_path: pathlib.Path,
        entity_id: str,
        publication_tokens: dict[pathlib.Path, PublicationToken],
        operation: Callable[[], None],
    ) -> Entity:
        try:
            operation()
            after_errors = set(validate_repository(self._root))
            new_errors = sorted(after_errors - before_errors)
            if new_errors:
                raise SchemaError(new_errors)
            result = self._read_entity(result_path)
            if result is None or result.entity_id != entity_id:
                raise InputError(f"mutation result could not be reread: {result_path}")
            return result
        except DestinationCollision:
            # os.link() did not create the destination, so every repository path
            # still belongs to its pre-mutation owner. Never remove the winner.
            raise
        except BaseException as primary_error:
            protected_paths = (
                {primary_error.path}
                if isinstance(primary_error, SourceOwnershipConflict)
                else set()
            )
            try:
                self._rollback_mutation(
                    snapshots,
                    before_errors,
                    publication_tokens,
                    protected_paths,
                )
            except BaseException as rollback_error:
                raise RuntimeError(
                    f"mutation rollback failed: {rollback_error}"
                ) from primary_error
            raise

    def _rollback_mutation(
        self,
        snapshots: dict[pathlib.Path, bytes | None],
        before_errors: set[str],
        publication_tokens: dict[pathlib.Path, PublicationToken],
        protected_paths: set[pathlib.Path],
    ) -> None:
        """Restore prior files before considering deletion of any new path.

        Ordering is a data-retention invariant: a failed prior-file restore must
        leave every newly written destination untouched as the remaining copy.
        """
        self._restore_prior_existing_paths(snapshots, protected_paths)
        self._cleanup_prior_absent_paths(snapshots, publication_tokens)
        restored_errors = set(validate_repository(self._root))
        if restored_errors != before_errors:
            added = sorted(restored_errors - before_errors)
            removed = sorted(before_errors - restored_errors)
            raise RuntimeError(
                f"restored repository validation differs; added={added}; removed={removed}"
            )

    @staticmethod
    def _restore_prior_existing_paths(
        snapshots: dict[pathlib.Path, bytes | None],
        protected_paths: set[pathlib.Path],
    ) -> None:
        """Restore and fsync all prior files; fail before new-path cleanup."""
        restore_errors: list[BaseException] = []
        for path, previous in snapshots.items():
            if previous is None or path in protected_paths:
                continue
            try:
                atomic_write(path, previous)
            except BaseException as error:
                restore_errors.append(error)
        if restore_errors:
            raise RuntimeError(
                "prior-existing restore failed: "
                + "; ".join(str(error) for error in restore_errors)
            ) from restore_errors[0]

    def _cleanup_prior_absent_paths(
        self,
        snapshots: dict[pathlib.Path, bytes | None],
        publication_tokens: dict[pathlib.Path, PublicationToken],
    ) -> None:
        """Remove only the exact inode this mutation published."""
        cleanup_errors: list[BaseException] = []
        for path, previous in snapshots.items():
            if previous is not None:
                continue
            try:
                token = publication_tokens.get(path)
                if token is None:
                    if not path.exists() and not path.is_symlink():
                        continue
                    raise RuntimeError(
                        f"missing rollback publication token for new path: {path}"
                    )
                try:
                    self._quarantine_delete_owned_path(
                        path,
                        token,
                        ownership_kind="publication",
                    )
                except FileNotFoundError:
                    continue
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            raise RuntimeError(
                "new-path cleanup failed: "
                + "; ".join(str(error) for error in cleanup_errors)
            ) from cleanup_errors[0]

    def _quarantine_delete_owned_path(
        self,
        path: pathlib.Path,
        expected_token: PublicationToken,
        *,
        ownership_kind: str,
    ) -> None:
        """Quarantine a live name, verify its inode, then delete or restore it."""
        quarantine: pathlib.Path | None = None
        post_effect_errors: list[RenamePostEffectError] = []
        for _ in range(32):
            candidate = path.parent / (
                f".{path.name}.{secrets.token_hex(12)}.quarantine"
            )
            try:
                _rename_no_replace(path, candidate, self._root)
            except RenamePostEffectError as error:
                post_effect_errors.append(error)
            except DestinationCollision:
                continue
            quarantine = candidate
            break
        if quarantine is None:
            raise RuntimeError(f"could not allocate quarantine path for: {path}")

        try:
            quarantined_bytes, quarantined_token = (
                self._read_regular_file_with_token(quarantine)
            )
        except BaseException as inspect_error:
            restore_post_effect = self._restore_quarantine_or_raise(
                path,
                quarantine,
                inspect_error,
                ownership_kind=ownership_kind,
            )
            if restore_post_effect is not None:
                post_effect_errors.append(restore_post_effect)
            if ownership_kind == "source":
                conflict = SourceOwnershipConflict(
                    path,
                    f"source ownership could not be verified: {path}",
                )
                if post_effect_errors:
                    raise conflict from post_effect_errors[0]
                raise conflict from inspect_error
            if post_effect_errors:
                raise RuntimeError(
                    "quarantine rename post-effect failure recovered at "
                    f"canonical path {path}; inspection also failed: "
                    f"{inspect_error}"
                ) from post_effect_errors[0]
            raise

        if quarantined_token != expected_token:
            mismatch = (
                f"{ownership_kind} ownership changed; publication token mismatch: "
                f"{path}"
            )
            restore_post_effect = self._restore_quarantine_or_raise(
                path,
                quarantine,
                RuntimeError(mismatch),
                ownership_kind=ownership_kind,
            )
            if restore_post_effect is not None:
                post_effect_errors.append(restore_post_effect)
            if ownership_kind == "source":
                conflict = SourceOwnershipConflict(
                    path,
                    f"source ownership changed: {path}"
                    + (
                        "; rename post-effect failure recovered"
                        if post_effect_errors
                        else ""
                    ),
                )
                if post_effect_errors:
                    raise conflict from post_effect_errors[0]
                raise conflict
            if post_effect_errors:
                raise RuntimeError(
                    f"{mismatch}; rename post-effect failure recovered at "
                    f"canonical path {path}"
                ) from post_effect_errors[0]
            raise RuntimeError(mismatch)

        try:
            safe_unlink(quarantine, self._root)
        except BaseException as cleanup_error:
            if not quarantine.exists() and not quarantine.is_symlink():
                try:
                    preserved = self._preserve_quarantine_bytes(
                        path, quarantined_bytes
                    )
                except QuarantinePreservationError as preserve_error:
                    raise QuarantineCleanupError(
                        f"quarantine cleanup failed: {cleanup_error}; "
                        f"copy preservation failed: {preserve_error}",
                        (cleanup_error, preserve_error),
                        (),
                        preserve_error.protected_paths,
                        preserve_error.uncertain_paths,
                    ) from cleanup_error
                except BaseException as preserve_error:
                    raise QuarantineCleanupError(
                        f"quarantine cleanup failed: {cleanup_error}; "
                        f"copy preservation failed: {preserve_error}",
                        (cleanup_error, preserve_error),
                        (),
                    ) from cleanup_error
                raise QuarantineCleanupError(
                    f"quarantine cleanup failed; preserved copy at {preserved}: "
                    f"{cleanup_error}",
                    (cleanup_error,),
                    (preserved,),
                ) from cleanup_error
            raise QuarantineCleanupError(
                f"quarantine cleanup failed; retained quarantine path: "
                f"{quarantine}: {cleanup_error}",
                (cleanup_error,),
                (quarantine,),
            ) from cleanup_error
        if ownership_kind == "source" and (
            path.exists() or path.is_symlink()
        ):
            conflict = SourceOwnershipConflict(
                path,
                f"source ownership changed after quarantine: {path}",
            )
            if post_effect_errors:
                raise conflict from post_effect_errors[0]
            raise conflict
        if post_effect_errors:
            raise OSError(
                "quarantine rename post-effect failure recovered; owned "
                f"quarantine removed for {path}: {post_effect_errors[0]}"
            ) from post_effect_errors[0]

    def _restore_quarantine_or_raise(
        self,
        path: pathlib.Path,
        quarantine: pathlib.Path,
        primary_error: BaseException,
        *,
        ownership_kind: str,
    ) -> RenamePostEffectError | None:
        """Restore a quarantined external winner without overwriting live state."""
        try:
            _rename_no_replace(quarantine, path, self._root)
        except RenamePostEffectError as post_effect_error:
            return post_effect_error
        except BaseException as restore_error:
            message = (
                "could not restore quarantined entry without overwriting a "
                f"newer winner; preserved quarantine path: {quarantine}: "
                f"{restore_error}"
            )
            if ownership_kind == "source":
                raise SourceOwnershipConflict(path, message) from primary_error
            raise RuntimeError(message) from primary_error
        return None

    def _preserve_quarantine_bytes(
        self, original_path: pathlib.Path, data: bytes
    ) -> pathlib.Path:
        """Persist a reachable copy after an unlink-after-effect error."""
        for _ in range(32):
            candidate = original_path.parent / (
                f".{original_path.name}.{secrets.token_hex(12)}.quarantine"
            )
            try:
                atomic_create_no_replace(candidate, data)
            except DestinationCollision:
                continue
            except BaseException as error:
                publication_token = getattr(error, "publication_token", None)
                if isinstance(publication_token, PublicationToken):
                    state, inspection_error = self._publication_path_state(
                        candidate, publication_token
                    )
                else:
                    try:
                        os.lstat(candidate)
                    except FileNotFoundError:
                        state, inspection_error = "absent", None
                    except OSError as candidate_error:
                        state, inspection_error = "unknown", candidate_error
                    else:
                        state, inspection_error = "unknown", None
                if state == "owned":
                    return candidate
                details = f"preservation publication outcome {state}: {candidate}"
                if inspection_error is not None:
                    details += f": {inspection_error}"
                if state == "external":
                    raise QuarantinePreservationError(
                        details,
                        error,
                        protected_paths=(candidate,),
                    ) from error
                if state == "unknown":
                    raise QuarantinePreservationError(
                        details,
                        error,
                        uncertain_paths=(candidate,),
                    ) from error
                raise QuarantinePreservationError(details, error) from error
            return candidate
        raise RuntimeError(
            f"could not allocate preservation quarantine for: {original_path}"
        )

    def _validate_mutation_fields(
        self,
        entity_type: str,
        fields: dict[str, str],
        *,
        create: bool,
    ) -> None:
        if entity_type not in REQUIRED_KEYS:
            raise InputError(f"unknown entity type: {entity_type}")
        if not isinstance(fields, dict):
            raise InputError("fields must be a dictionary")
        allowed = _CREATE_FIELDS[entity_type] if create else _UPDATE_FIELDS[entity_type]
        for key, value in fields.items():
            if not isinstance(key, str):
                raise InputError("field names must be strings")
            if key in FORBIDDEN_KEYS:
                raise InputError(f"forbidden key: {key}")
            if key in _GENERATED_KEYS or key == "review_kind":
                raise InputError(f"immutable key: {key}")
            if key not in allowed:
                raise InputError(f"unknown key: {key}")
            if not isinstance(value, str):
                raise InputError(f"non-string value for key: {key}")
        if create and entity_type != "review" and "title" not in fields:
            raise InputError("missing required field: title")

    def _validate_complete_frontmatter(
        self, frontmatter: dict[str, str], entity_type: str
    ) -> None:
        title = frontmatter.get("title", "")
        if not title.strip():
            raise InputError("title must be nonempty")
        if entity_type == "review":
            review_kind = frontmatter.get("review_kind")
            if review_kind not in {"daily", "weekly"}:
                raise InputError(f"invalid review kind: {review_kind}")
        if entity_type == "task":
            status = frontmatter.get("status")
            project_position = frontmatter.get("project_position")
            if (
                project_position
                and PROJECT_TASK_POSITION_PATTERN.fullmatch(project_position) is None
            ):
                raise InputError(f"invalid project_position: {project_position}")
            if status == "planned":
                if not frontmatter.get("project_id"):
                    raise InputError("planned Task requires project_id")
                for key in (
                    "waiting_for",
                    "action_date",
                    "scheduled_start",
                    "scheduled_end",
                    *CALENDAR_IDENTITY_KEYS,
                    "calendar_sync_version",
                    "started_at",
                    "completed_at",
                ):
                    if frontmatter.get(key):
                        raise InputError(f"planned Task forbids {key}")
            if status == "inbox" and frontmatter.get("action_date"):
                raise InputError("inbox Task forbids action_date")
            action_date = frontmatter.get("action_date")
            if action_date and not self._is_date(action_date):
                raise InputError(f"invalid action_date: {action_date}")
            due = frontmatter.get("due")
            if due and not (self._is_date(due) or self._is_timestamp(due)):
                raise InputError(f"invalid due: {due}")
            contexts = frontmatter.get("contexts")
            if contexts is not None and _INLINE_LIST_PATTERN.fullmatch(contexts) is None:
                raise InputError(f"invalid contexts: {contexts}")
            depends_on = frontmatter.get("depends_on")
            if depends_on is not None and parse_inline_list(depends_on) is None:
                raise InputError("depends_on must be an inline list")
            estimated = frontmatter.get("estimated_minutes")
            if estimated and POSITIVE_INTEGER_PATTERN.fullmatch(estimated) is None:
                raise InputError(f"invalid estimated_minutes: {estimated}")
            start = self._parsed_timestamp(frontmatter.get("scheduled_start", ""))
            end = self._parsed_timestamp(frontmatter.get("scheduled_end", ""))
            for key, value, parsed in (
                ("scheduled_start", frontmatter.get("scheduled_start"), start),
                ("scheduled_end", frontmatter.get("scheduled_end"), end),
            ):
                if value and parsed is None:
                    raise InputError(f"invalid timestamp for {key}: {value}")
            if start is not None and end is not None and end < start:
                raise InputError("scheduled_end is earlier than scheduled_start")
        if entity_type == "project":
            if frontmatter.get("goal_id") and frontmatter.get("roadmap_outcome_id"):
                raise RoadmapOperationError("project_direction_link_conflict", 422)
            planned_start_date = frontmatter.get("planned_start_date")
            planned_end_date = frontmatter.get("planned_end_date")
            if bool(planned_start_date) != bool(planned_end_date):
                raise InputError("planned_start_date and planned_end_date must be paired")
            if planned_start_date and planned_end_date:
                if not self._is_date(planned_start_date):
                    raise InputError(
                        f"invalid date for planned_start_date: {planned_start_date}"
                    )
                if not self._is_date(planned_end_date):
                    raise InputError(
                        f"invalid date for planned_end_date: {planned_end_date}"
                    )
                if planned_end_date < planned_start_date:
                    raise InputError("planned_end_date is earlier than planned_start_date")
            position = frontmatter.get("kanban_position")
            if position and PROJECT_KANBAN_POSITION_PATTERN.fullmatch(position) is None:
                raise ProjectOperationError("project_position_invalid", 422)
        if entity_type in {"roadmap_outcome", "cycle", "progress", "time_allocation_plan"}:
            errors = _validate_values(frontmatter, EntityContract(entity_type))
            if errors:
                first = errors[0]
                if entity_type == "cycle" and (
                    "at most two" in first or "duplicate" in first
                ):
                    raise RoadmapOperationError("cycle_outcome_limit", 422)
                if entity_type == "roadmap_outcome" and (
                    "roadmap_" in first
                ):
                    raise RoadmapOperationError("roadmap_position_invalid", 422)
                raise InputError(first)
        serialize_frontmatter(frontmatter, entity_type)

    def _validate_project_transition(
        self, current: Entity, proposed: dict[str, str]
    ) -> None:
        """Enforce Project lifecycle invariants against current child Tasks."""
        proposed_status = proposed.get("status")
        if proposed_status == current.frontmatter.get("status"):
            return
        children = [
            entity
            for entity in self.list_entities()
            if entity.entity_type == "task"
            and not entity.relative_path.startswith("archive/")
            and entity.frontmatter.get("project_id") == current.entity_id
        ]
        if proposed_status in {"completed", "dropped"} and any(
            child.frontmatter.get("status") != "done" for child in children
        ):
            raise ProjectOperationError("unfinished_project_tasks")
        if proposed_status in {"not_started", "on_hold"} and any(
            child.frontmatter.get("status") == "doing" for child in children
        ):
            raise ProjectOperationError("project_has_doing_task")

    def _require_doing_task_project_status(
        self,
        task_frontmatter: dict[str, str],
        projected_project_statuses: dict[str, str] | None = None,
    ) -> None:
        """Require a doing Task's linked Project to be doing in this apply."""
        if task_frontmatter.get("status") != "doing":
            return
        project_id = task_frontmatter.get("project_id")
        if not project_id:
            return
        project_status = (projected_project_statuses or {}).get(project_id)
        if project_status is None:
            try:
                project = self._find_mutation_entity(project_id)
            except NotFoundError as error:
                raise ProjectOperationError("project_not_executable") from error
            project_status = project.frontmatter.get("status")
        if project_status != "doing":
            raise ProjectOperationError("project_not_executable")

    def _validate_links(
        self, frontmatter: dict[str, str], entity_type: str
    ) -> None:
        if (
            entity_type == "task"
            and frontmatter.get("project_id")
            and frontmatter.get("area_id")
        ):
            raise InputError("Tasks cannot have both project_id and area_id")
        if (
            entity_type == "project"
            and frontmatter.get("goal_id")
            and frontmatter.get("roadmap_outcome_id")
        ):
            raise RoadmapOperationError("project_direction_link_conflict", 422)
        for link_key, expected_type, _ in REFERENCE_CONTRACTS.get(
            entity_type, ()
        ):
            linked_id = frontmatter.get(link_key, "")
            if not linked_id:
                continue
            try:
                linked = self._find_mutation_entity(linked_id)
            except NotFoundError as error:
                raise InputError(f"unknown {link_key}: {linked_id}") from error
            if linked.entity_type != expected_type:
                raise InputError(
                    f"invalid {link_key} type: {linked.entity_type}; "
                    f"expected {expected_type}"
                )
            if (
                linked.relative_path.startswith("archive/")
                and entity_type != "progress"
            ):
                raise InputError(f"archived {link_key}: {linked_id}")
            if entity_type == "task" and link_key == "project_id":
                task_status = frontmatter.get("status")
                project_status = linked.frontmatter.get("status")
                if task_status != "done" and project_status in {
                    "completed", "dropped"
                }:
                    raise ProjectOperationError("project_not_executable")
        if entity_type == "cycle":
            for outcome_id in parse_inline_list(frontmatter.get("outcome_ids", "")) or []:
                try:
                    outcome = self._find_mutation_entity(outcome_id)
                except NotFoundError as error:
                    raise InputError(f"unknown outcome_id: {outcome_id}") from error
                if outcome.entity_type != "roadmap_outcome" or outcome.relative_path.startswith("archive/"):
                    raise InputError(f"invalid outcome_id: {outcome_id}")
                if outcome.frontmatter.get("status") != "active":
                    raise InputError(f"non-active outcome_id: {outcome_id}")

    def _validate_roadmap_crud_state(
        self,
        frontmatter: dict[str, str],
        *,
        exclude_entity_id: str | None = None,
        previous_frontmatter: dict[str, str] | None = None,
    ) -> None:
        entity_type = frontmatter.get("type")
        if entity_type == "roadmap_outcome":
            lane = frontmatter.get("roadmap_lane")
            position = frontmatter.get("roadmap_position")
            ordering_is_unchanged = previous_frontmatter is not None and all(
                frontmatter.get(key) == previous_frontmatter.get(key)
                for key in ("status", "roadmap_lane", "roadmap_position")
            )
            if lane in {"next", "later"} and position and not ordering_is_unchanged:
                occupied = [
                    entity for entity in self.list_entities()
                    if entity.entity_type == "roadmap_outcome"
                    and entity.entity_id != exclude_entity_id
                    and not entity.relative_path.startswith("archive/")
                    and entity.frontmatter.get("status") == "active"
                    and entity.frontmatter.get("roadmap_lane") == lane
                ]
                if position != str(len(occupied) + 1):
                    raise RoadmapOperationError("roadmap_position_invalid", 422)
            return
        if entity_type != "cycle":
            return
        if frontmatter.get("status") == "planned":
            for outcome_id in (
                parse_inline_list(frontmatter.get("outcome_ids", "")) or []
            ):
                outcome = self._find_mutation_entity(outcome_id)
                lane = outcome.frontmatter.get("roadmap_lane")
                position = outcome.frontmatter.get("roadmap_position")
                if (
                    lane not in {"next", "later"}
                    or not position
                    or not POSITIVE_INTEGER_PATTERN.fullmatch(position)
                ):
                    raise RoadmapOperationError(
                        "roadmap_operation_invalid", 400
                    )
        start = datetime.date.fromisoformat(frontmatter["start_date"])
        end = datetime.date.fromisoformat(frontmatter["end_date"])
        if frontmatter.get("status") in {"planned", "active"}:
            for entity in self.list_entities():
                if (
                    entity.entity_type != "cycle"
                    or entity.entity_id == exclude_entity_id
                    or entity.relative_path.startswith("archive/")
                    or entity.frontmatter.get("status") not in {"planned", "active"}
                ):
                    continue
                other_start = datetime.date.fromisoformat(entity.frontmatter["start_date"])
                other_end = datetime.date.fromisoformat(entity.frontmatter["end_date"])
                if start <= other_end and other_start <= end:
                    raise RoadmapOperationError("cycle_period_overlap", 409)

    def _outcome_is_now(self, outcome_id: str) -> bool:
        for entity in self.list_entities():
            if (
                not entity.relative_path.startswith("archive/")
                and entity.entity_type == "cycle"
                and entity.frontmatter.get("status") == "active"
                and outcome_id in (parse_inline_list(entity.frontmatter.get("outcome_ids", "")) or [])
            ):
                return True
        return False

    def _require_purpose_slot_available(self) -> None:
        purpose_root = self._root / "purposes"
        if purpose_root.is_symlink() or not purpose_root.is_dir():
            return
        for path in self._walk_directory(purpose_root):
            try:
                entity = self._read_entity(path)
            except InputError:
                continue
            if entity is not None and entity.entity_type == "purpose":
                raise InputError("a non-archived Purpose already exists")

    def _require_no_active_children(
        self,
        parent: Entity,
        *,
        ignored_child_ids: frozenset[str] = frozenset(),
    ) -> None:
        for directory in _SCAN_DIRECTORIES:
            if directory == "archive":
                continue
            scan_root = self._root / directory
            if scan_root.is_symlink() or not scan_root.is_dir():
                continue
            for path in self._walk_directory(scan_root):
                try:
                    child = self._read_entity(path)
                except InputError:
                    continue
                if child is None:
                    continue
                if child.entity_type == "progress":
                    continue
                if child.entity_id in ignored_child_ids:
                    continue
                for link_key, expected_type, _ in REFERENCE_CONTRACTS.get(
                    child.entity_type, ()
                ):
                    if (
                        expected_type == parent.entity_type
                        and child.frontmatter.get(link_key) == parent.entity_id
                    ):
                        raise InputError(
                            f"cannot archive entity referenced by {child.entity_id}"
                        )
                if child.entity_type == "cycle":
                    if (
                        parent.entity_type == "roadmap_outcome"
                        and parent.entity_id
                        in (parse_inline_list(child.frontmatter.get("outcome_ids", "")) or [])
                    ) or (
                        parent.entity_type == "cycle"
                        and child.frontmatter.get("carryover_cycle_id") == parent.entity_id
                    ):
                        raise InputError(
                            f"cannot archive entity referenced by {child.entity_id}"
                        )

    def _find_mutation_entity(self, entity_id: str) -> Entity:
        with self._repository_cache_lock:
            self._refresh_repository_cache_locked()
            matches = [
                cached.entity
                for cached in self._repository_cache.values()
                if cached.entity is not None
                and cached.entity.entity_id == entity_id
            ]
        if not matches:
            raise NotFoundError(f"unknown entity id: {entity_id}")
        if len(matches) > 1:
            raise InputError(f"duplicate entity id: {entity_id}")
        cached = matches[0]
        current = self._read_entity(self._root / cached.relative_path)
        if current is None or current.entity_id != entity_id:
            raise MutationPlanConflict("entity changed during current-state read")
        return current

    def _require_mutation_current(
        self, entity_id: str, base_hash: str
    ) -> Entity:
        if not isinstance(base_hash, str) or _CONTENT_HASH_PATTERN.fullmatch(base_hash) is None:
            raise InputError("base_hash must be 64 lowercase hexadecimal characters")
        current = self._find_mutation_entity(entity_id)
        if current.content_hash != base_hash:
            raise ConflictError(current)
        return current

    def _canonical_destination(
        self, frontmatter: dict[str, str]
    ) -> pathlib.Path:
        entity_type = frontmatter["type"]
        entity_id = frontmatter["id"]
        if entity_type == "task":
            directory = "inbox" if frontmatter["status"] == "inbox" else "tasks"
        elif entity_type in {"purpose", "vision", "area", "project", "goal"}:
            directory = f"{entity_type}s"
        elif entity_type == "roadmap_outcome":
            directory = "roadmap-outcomes"
        elif entity_type == "cycle":
            directory = "cycles"
        elif entity_type == "progress":
            directory = "progress"
        elif entity_type == "time_allocation_plan":
            directory = "time-allocation-plans"
        elif entity_type == "review":
            directory = f"reviews/{frontmatter['review_kind']}"
        else:
            raise InputError(f"unknown entity type: {entity_type}")
        return self._root / directory / f"{entity_id}.md"

    @staticmethod
    def _canonical_document(
        frontmatter: dict[str, str], entity_type: str, body: str
    ) -> bytes:
        stripped = body.strip("\r\n")
        normalized_body = "\n" if not stripped else f"\n{stripped}\n"
        return (serialize_frontmatter(frontmatter, entity_type) + normalized_body).encode(
            "utf-8"
        )

    @staticmethod
    def _is_date(value: str) -> bool:
        if DATE_PATTERN.fullmatch(value) is None:
            return False
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _parsed_timestamp(value: str) -> datetime.datetime | None:
        if not value or TIMESTAMP_PATTERN.fullmatch(value) is None:
            return None
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.utcoffset() is not None else None

    @classmethod
    def _parsed_work_session_timestamp(cls, value: str) -> datetime.datetime | None:
        if not isinstance(value, str) or WORK_TIMESTAMP_PATTERN.fullmatch(value) is None:
            return None
        return cls._parsed_timestamp(value)

    @classmethod
    def _is_timestamp(cls, value: str) -> bool:
        return cls._parsed_timestamp(value) is not None

    def _walk_directory(self, directory: pathlib.Path) -> Iterator[pathlib.Path]:
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise InputError(
                f"cannot scan repository directory: {directory}"
            ) from error
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                yield from self._walk_directory(child)
            elif (
                child.is_file()
                and child.suffix == ".md"
                and child.name != "README.md"
            ):
                yield child

    def _verify_repository_root_identity(self) -> None:
        try:
            root_stat = os.stat(
                self._root,
                follow_symlinks=self._root_descriptor >= 0,
            )
        except OSError as error:
            raise InputError(
                f"cannot verify repository root inode: {self._root}"
            ) from error
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or (root_stat.st_dev, root_stat.st_ino) != self._root_identity
        ):
            raise InputError(f"repository root inode changed: {self._root}")

    def _scan_supported_regular_files(
        self,
    ) -> dict[str, tuple[pathlib.Path, _FileSignature]]:
        """Stat supported paths without following repository symlinks."""
        self._verify_repository_root_identity()
        found: dict[str, tuple[pathlib.Path, _FileSignature]] = {}

        def scan(directory: pathlib.Path) -> None:
            try:
                directory_stat = os.stat(directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError as error:
                raise InputError(
                    f"cannot scan repository directory: {directory}"
                ) from error
            if not stat.S_ISDIR(directory_stat.st_mode):
                return
            try:
                children = sorted(directory.iterdir(), key=lambda path: path.name)
            except OSError as error:
                raise InputError(
                    f"cannot scan repository directory: {directory}"
                ) from error
            for child in children:
                try:
                    child_stat = os.stat(child, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise InputError(
                        f"cannot stat repository path: {child}"
                    ) from error
                if stat.S_ISDIR(child_stat.st_mode):
                    scan(child)
                elif (
                    stat.S_ISREG(child_stat.st_mode)
                    and child.suffix == ".md"
                    and child.name != "README.md"
                ):
                    relative_path = child.relative_to(self._root).as_posix()
                    found[relative_path] = (
                        child,
                        _FileSignature.from_stat(child_stat),
                    )

        for directory in _SCAN_DIRECTORIES:
            scan(self._root / directory)
        return found

    def _refresh_repository_cache_locked(self) -> None:
        scanned = self._scan_supported_regular_files()
        previous = self._repository_cache
        refreshed: dict[str, _CachedDocument] = {}
        for relative_path in sorted(scanned):
            path, signature = scanned[relative_path]
            cached = self._repository_cache.get(relative_path)
            if cached is not None and cached.signature == signature:
                refreshed[relative_path] = cached
                continue

            try:
                resolved_root = self._root.resolve(strict=True)
                resolved_path = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise InputError(
                    f"cannot resolve repository entity: {path}"
                ) from error
            if resolved_path != resolved_root / pathlib.PurePosixPath(relative_path):
                raise InputError(f"repository entity escaped root: {path}")
            raw_bytes, token = self._read_regular_file_with_token(path)
            try:
                verified_stat = os.stat(path, follow_symlinks=False)
            except OSError as error:
                raise InputError(
                    f"cannot verify cached repository entity: {path}"
                ) from error
            verified_signature = _FileSignature.from_stat(verified_stat)
            if (
                not stat.S_ISREG(verified_stat.st_mode)
                or verified_signature != signature
                or (token.st_dev, token.st_ino)
                != (signature.st_dev, signature.st_ino)
            ):
                raise InputError(
                    f"repository entity changed while refreshing cache: {path}"
                )
            try:
                entity = self._entity_from_bytes(relative_path, raw_bytes)
            except InputError:
                entity = None
            refreshed[relative_path] = _CachedDocument(
                entity=entity,
                raw_bytes=raw_bytes,
                signature=signature,
            )
        changed = set(refreshed) != set(previous) or any(
            refreshed[relative_path] is not previous[relative_path]
            for relative_path in refreshed.keys() & previous.keys()
        )
        self._repository_cache = refreshed
        if changed:
            self._repository_cache_generation += 1

    def _read_entity(self, path: pathlib.Path) -> Entity | None:
        try:
            if path.is_symlink():
                return None
            relative_path = path.relative_to(self._root)
            resolved_root = self._root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise InputError(f"cannot resolve repository entity: {path}") from error
        except ValueError:
            return None
        if (
            resolved_path != resolved_root / relative_path
            or not resolved_path.is_file()
        ):
            return None

        content_bytes = self._read_regular_file(path)
        try:
            text = content_bytes.decode("utf-8")
        except UnicodeError as error:
            raise InputError(f"cannot read UTF-8 repository entity: {path}") from error
        frontmatter, body = split_document(text)
        entity_id = frontmatter.get("id")
        entity_type = frontmatter.get("type")
        if not entity_id or not entity_type:
            raise InputError(f"entity is missing id or type: {path}")
        return Entity(
            entity_id=entity_id,
            entity_type=entity_type,
            relative_path=relative_path.as_posix(),
            frontmatter=frontmatter,
            body=body,
            content_hash=hashlib.sha256(content_bytes).hexdigest(),
        )

    def _read_regular_file(self, path: pathlib.Path) -> bytes:
        content, _ = self._read_regular_file_with_token(path)
        return content

    def _read_regular_file_with_token(
        self, path: pathlib.Path
    ) -> tuple[bytes, PublicationToken]:
        """Read a verified in-root regular file through one Linux descriptor.

        The product host is Linux. Fail closed if its O_NOFOLLOW or /proc fd
        identity checks are unavailable rather than falling back to a racy
        path-based read.
        """
        nofollow = getattr(os, "O_NOFOLLOW", None)
        proc_fd_root = pathlib.Path("/proc/self/fd")
        if nofollow is None or not proc_fd_root.is_dir():
            raise InputError(
                "secure repository reads require Linux /proc and O_NOFOLLOW"
            )

        flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise InputError(
                f"cannot securely open repository entity: {path}"
            ) from error

        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise InputError(f"repository entity is not a regular file: {path}")

            try:
                relative_path = path.relative_to(self._root)
                resolved_root = self._root.resolve(strict=True)
                opened_path = (proc_fd_root / str(descriptor)).resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as error:
                raise InputError(
                    f"opened repository entity escaped root: {path}"
                ) from error
            if opened_path != resolved_root / relative_path:
                raise InputError(f"opened repository entity changed path: {path}")

            try:
                candidate_stat = os.stat(path, follow_symlinks=False)
            except OSError as error:
                raise InputError(
                    f"cannot verify opened repository entity: {path}"
                ) from error
            if (
                not stat.S_ISREG(candidate_stat.st_mode)
                or candidate_stat.st_dev != opened_stat.st_dev
                or candidate_stat.st_ino != opened_stat.st_ino
            ):
                raise InputError(f"opened repository entity changed file: {path}")

            with os.fdopen(descriptor, "rb") as file:
                descriptor = -1
                content = file.read()
            return content, PublicationToken.from_stat(opened_stat)
        except OSError as error:
            raise InputError(f"cannot read repository entity: {path}") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _open_lock_file(self) -> int:
        """Open and verify the repository lock through one Linux descriptor."""
        nofollow = getattr(os, "O_NOFOLLOW", None)
        proc_fd_root = pathlib.Path("/proc/self/fd")
        if nofollow is None or not proc_fd_root.is_dir():
            raise InputError("mutation locks require Linux /proc and O_NOFOLLOW")

        lock_path = self._root / ".webapp.lock"
        close_on_exec = getattr(os, "O_CLOEXEC", None)
        if close_on_exec is None:
            raise InputError("mutation locks require Linux O_CLOEXEC")
        flags = os.O_RDWR | os.O_CREAT | nofollow | close_on_exec
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise InputError(
                f"cannot securely open repository lock: {lock_path}"
            ) from error

        try:
            self._verify_lock_file(descriptor, lock_path)
            return descriptor
        except (OSError, InputError) as error:
            os.close(descriptor)
            if isinstance(error, InputError):
                raise
            raise InputError(f"cannot verify repository lock: {lock_path}") from error

    def _open_root_lock_directory(self) -> int:
        """Open and verify the stable repository root lock inode."""
        directory_flag = getattr(os, "O_DIRECTORY", None)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        close_on_exec = getattr(os, "O_CLOEXEC", None)
        proc_fd_root = pathlib.Path("/proc/self/fd")
        if (
            directory_flag is None
            or nofollow is None
            or close_on_exec is None
            or not proc_fd_root.is_dir()
        ):
            raise InputError(
                "mutation locks require Linux /proc, O_DIRECTORY, "
                "O_NOFOLLOW, and O_CLOEXEC"
            )

        if self._root_descriptor >= 0:
            try:
                descriptor = os.dup(self._root_descriptor)
            except OSError as error:
                raise InputError(
                    f"cannot securely duplicate repository root: {self._root}"
                ) from error
            try:
                self._verify_root_lock_directory(descriptor)
                return descriptor
            except (OSError, InputError) as error:
                os.close(descriptor)
                if isinstance(error, InputError):
                    raise
                raise InputError(
                    f"cannot verify repository root: {self._root}"
                ) from error

        flags = os.O_RDONLY | directory_flag | nofollow | close_on_exec
        try:
            descriptor = os.open(self._root, flags)
        except OSError as error:
            raise InputError(
                f"cannot securely open repository root: {self._root}"
            ) from error
        try:
            self._verify_root_lock_directory(descriptor)
            return descriptor
        except (OSError, InputError) as error:
            os.close(descriptor)
            if isinstance(error, InputError):
                raise
            raise InputError(
                f"cannot verify repository root: {self._root}"
            ) from error

    def _verify_root_lock_directory(self, descriptor: int) -> None:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise InputError(f"repository root is not a directory: {self._root}")
        try:
            opened_path = pathlib.Path(f"/proc/self/fd/{descriptor}").resolve(
                strict=True
            )
        except (OSError, RuntimeError) as error:
            raise InputError(
                f"cannot verify opened repository root: {self._root}"
            ) from error
        try:
            expected_path = self._root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise InputError(
                f"cannot verify bound repository root: {self._root}"
            ) from error
        if opened_path != expected_path:
            raise InputError(f"opened repository root changed path: {self._root}")

        candidate_stat = os.stat(
            self._root, follow_symlinks=self._root_descriptor >= 0
        )
        if (
            not stat.S_ISDIR(candidate_stat.st_mode)
            or candidate_stat.st_dev != opened_stat.st_dev
            or candidate_stat.st_ino != opened_stat.st_ino
            or (opened_stat.st_dev, opened_stat.st_ino) != self._root_identity
        ):
            raise InputError(f"opened repository root changed inode: {self._root}")

    @staticmethod
    def _acquire_flock(
        descriptor: int,
        deadline: float,
        description: str,
    ) -> None:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except InterruptedError:
                pass
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise InputError(f"cannot acquire {description}") from error

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StoreLockTimeout(f"timed out acquiring {description}")
            time.sleep(min(LOCK_RETRY_INTERVAL_SECONDS, remaining))

    def _verify_lock_file(self, descriptor: int, lock_path: pathlib.Path) -> None:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise InputError(f"repository lock is not a regular file: {lock_path}")
        try:
            opened_path = pathlib.Path(f"/proc/self/fd/{descriptor}").resolve(
                strict=True
            )
        except (OSError, RuntimeError) as error:
            raise InputError(
                f"cannot verify opened repository lock: {lock_path}"
            ) from error
        try:
            relative_path = lock_path.relative_to(self._root)
            expected_path = self._root.resolve(strict=True) / relative_path
        except (OSError, RuntimeError, ValueError) as error:
            raise InputError(
                f"cannot verify opened repository lock: {lock_path}"
            ) from error
        if opened_path != expected_path:
            raise InputError(f"opened repository lock changed path: {lock_path}")

        candidate_stat = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate_stat.st_dev != opened_stat.st_dev
            or candidate_stat.st_ino != opened_stat.st_ino
        ):
            raise InputError(f"opened repository lock changed file: {lock_path}")


def split_document(text: str) -> tuple[dict[str, str], str]:
    """Split constrained frontmatter from a Markdown body."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise InputError("missing opening ---")

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing_index is None:
        raise InputError("missing closing ---")

    frontmatter: dict[str, str] = {}
    for raw_line in lines[1:closing_index]:
        line = raw_line.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() or ":" not in line:
            raise InputError(f"invalid frontmatter line: {line}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise InputError(f"invalid frontmatter line: {line}")
        if key in frontmatter:
            raise InputError(f"duplicate frontmatter key: {key}")
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        frontmatter[key] = value

    return frontmatter, "".join(lines[closing_index + 1 :])


def serialize_frontmatter(frontmatter: dict[str, str], entity_type: str) -> str:
    """Serialize one complete deterministic constrained frontmatter block."""
    if entity_type not in REQUIRED_KEYS:
        raise InputError(f"unknown entity type: {entity_type}")

    required_keys = REQUIRED_KEYS[entity_type]
    missing_keys = [key for key in required_keys if key not in frontmatter]
    if missing_keys:
        raise InputError(f"missing required key: {missing_keys[0]}")

    allowed_keys = set(required_keys) | OPTIONAL_KEYS[entity_type]
    for key in frontmatter:
        if key in FORBIDDEN_KEYS:
            raise InputError(f"forbidden key: {key}")
        if key not in allowed_keys:
            raise InputError(f"unknown key: {key}")

    lines = ["---"]
    for key in (*required_keys, *OPTIONAL_KEY_ORDER[entity_type]):
        if key not in frontmatter:
            continue
        value = frontmatter[key]
        if key not in required_keys and value == "":
            continue
        if value is None:
            raise InputError(f"non-string value for key: {key}")
        lines.append(f"{key}: {_serialize_value(key, value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _serialize_value(key: str, value: str) -> str:
    if not isinstance(value, str):
        raise InputError(f"non-string value for key: {key}")
    if '"' in value or "\r" in value or "\n" in value or value.startswith("#"):
        raise InputError(f"unsupported value for key: {key}")
    if _BARE_VALUE_PATTERN.fullmatch(value):
        return value
    if key in {
        "contexts", "depends_on", "outcome_ids", "achieved_outcome_ids",
        "carried_outcome_ids", "next_outcome_ids", "later_outcome_ids",
        "dropped_outcome_ids",
    } and _INLINE_LIST_PATTERN.fullmatch(value):
        return value
    return f'"{value}"'
