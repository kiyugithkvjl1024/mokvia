"""Fixed-allowlist HTTP transport for the local mokvia Web application."""

from __future__ import annotations

import datetime
import errno
import http
import json
import os
import pathlib
import re
import signal
import stat
import sys
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from webapp.preflight import (
    PRODUCT_HOST,
    PRODUCT_ORIGIN,
    PRODUCT_PORT,
    REPOSITORY_ROOT,
    activate_validator,
    run_preflight,
)
from webapp.security import (
    ForbiddenRequestError,
    PRODUCT_MUTATION_ORIGINS,
    SecurityInputError,
    normalize_mutation_origins,
    response_security_headers,
    validate_mutation_headers,
)


_SHELL_ROUTES = frozenset(
    {
        "/",
        "/inbox",
        "/calendar",
        "/clarify",
        "/tasks",
        "/projects",
        "/roadmap",
        "/reviews/weekly",
        "/help/reviews",
        "/status",
        "/search",
        "/reports/progress",
    }
)
_STATIC_ROUTES = {
    "/assets/timetracker-nx.js": ("timetracker-nx.js", "text/javascript; charset=utf-8"),
    "/assets/timetracker-nx.css": ("timetracker-nx.css", "text/css; charset=utf-8"),
    "/assets/integration-settings.js": ("integration-settings.js", "text/javascript; charset=utf-8"),
    "/assets/outlook-import.js": ("outlook-import.js", "text/javascript; charset=utf-8"),
    "/assets/local-capture.js": ("local-capture.js", "text/javascript; charset=utf-8"),
    "/assets/local-capture.css": ("local-capture.css", "text/css; charset=utf-8"),
    "/assets/local-calendar.js": ("local-calendar.js", "text/javascript; charset=utf-8"),
    "/assets/local-calendar.css": ("local-calendar.css", "text/css; charset=utf-8"),
    "/assets/local-notifications.js": ("local-notifications.js", "text/javascript; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/task-card.js": ("task-card.js", "text/javascript; charset=utf-8"),
    "/assets/task-search.js": ("task-search.js", "text/javascript; charset=utf-8"),
    "/assets/archived-task-detail.js": ("archived-task-detail.js", "text/javascript; charset=utf-8"),
    "/assets/focus-completed.js": ("focus-completed.js", "text/javascript; charset=utf-8"),
    "/assets/allocation-ui.js": ("allocation-ui.js", "text/javascript; charset=utf-8"),
    "/assets/quick-start-suggestions.js": ("quick-start-suggestions.js", "text/javascript; charset=utf-8"),
    "/assets/focus-pip.js": ("focus-pip.js", "text/javascript; charset=utf-8"),
    "/assets/cycle-focus-context.js": ("cycle-focus-context.js", "text/javascript; charset=utf-8"),
    "/assets/roadmap-cycle-ui.js": ("roadmap-cycle-ui.js", "text/javascript; charset=utf-8"),
    "/assets/progress.js": ("progress.js", "text/javascript; charset=utf-8"),
    "/assets/review-help-data.js": ("review-help-data.js", "text/javascript; charset=utf-8"),
    "/assets/task-settings.js": ("task-settings.js", "text/javascript; charset=utf-8"),
    "/assets/task-panel-editor.js": ("task-panel-editor.js", "text/javascript; charset=utf-8"),
    "/assets/unsaved-navigation-guard.js": ("unsaved-navigation-guard.js", "text/javascript; charset=utf-8"),
    "/assets/markdown-preview.js": ("markdown-preview.js", "text/javascript; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/project-kanban-motion.js": ("project-kanban-motion.js", "text/javascript; charset=utf-8"),
    "/assets/project-task-board.js": ("project-task-board.js", "text/javascript; charset=utf-8"),
    "/assets/local-background.svg": ("local-background.svg", "image/svg+xml"),
}
_MUTATION_ROUTES = frozenset(
    {"/api/v1/mutations/preview", "/api/v1/mutations/apply", "/api/v1/local-notifications/heartbeat", "/api/v1/outlook-import/run", "/api/v1/outlook-import/local", "/api/v1/integrations/settings", "/api/v1/integrations/actual-write", "/api/v1/integrations/actual-preview", "/api/v1/integrations/actual-apply", "/api/v1/integrations/actual-reconcile", "/api/v1/integrations/task"}
)
_READ_API_ROUTES = frozenset(
    {
        "/api/v1/health",
        "/api/v1/calendar",
        "/api/v1/outlook-import/status",
        "/api/v1/integrations/status",
        "/api/v1/actuals",
        "/api/v1/capture-import/status",
        "/api/v1/local-notifications",
        "/api/v1/snapshot",
        "/api/v1/quick-start-suggestions",
        "/api/v1/tasks/search",
        "/api/v1/calendar-sync/status",
        "/api/v1/reports/resource-allocation",
        "/api/v1/reports/resource-allocation/tasks",
        "/api/v1/time-allocation-plan-draft",
        "/api/v1/progress",
        "/api/v1/reports/progress",
    }
)
_MAX_BODY_BYTES = 1_048_576
_MAX_QUERY_FIELDS = 100
_CONTENT_LENGTH = re.compile(r"[0-9]+", re.ASCII)
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}", re.ASCII)
_PREVIEW_BANNER_MARKER = b"<!-- preview-read-only-banner -->"
_PREVIEW_BANNER = (
    '<div class="banner notice" role="status">'
    "Preview — 読み取り専用の独立コピーです。"
    "本番とは件数・内容が異なる場合があります。"
    "</div>"
).encode("utf-8")
_KNOWN_METHODS = frozenset(
    {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # Request data and exception text are intentionally never logged.
        return

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            try:
                importer = getattr(self, "_capture_importer", None)
                if importer is not None:
                    importer.close()
            finally:
                store = getattr(self, "_mokvia_store", None)
                close = getattr(store, "close", None)
                if callable(close):
                    close()


class _InvalidQuery(ValueError):
    pass


class _UnsafeStaticFile(OSError):
    pass


class _SignalShutdownFailure(RuntimeError):
    pass


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _error_payload(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message, "details": []}}


def _valid_percent_encoding(value: str) -> bool:
    index = 0
    while True:
        index = value.find("%", index)
        if index < 0:
            return True
        if _PERCENT_ESCAPE.match(value, index) is None:
            return False
        index += 3


def _decode_query_component(value: str) -> str:
    if not _valid_percent_encoding(value):
        raise _InvalidQuery
    try:
        raw = urllib.parse.unquote_to_bytes(value.replace("+", " "))
        decoded = raw.decode("utf-8", "strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise _InvalidQuery from error
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in decoded):
        raise _InvalidQuery
    return decoded


def _parse_query(raw_query: str) -> Mapping[str, str | Sequence[str]]:
    if raw_query == "":
        return {}
    pairs = raw_query.split("&")
    if len(pairs) > _MAX_QUERY_FIELDS:
        raise _InvalidQuery
    result: dict[str, str | list[str]] = {}
    for pair in pairs:
        if "=" not in pair:
            raise _InvalidQuery
        raw_key, raw_value = pair.split("=", 1)
        key = _decode_query_component(raw_key)
        value = _decode_query_component(raw_value)
        existing = result.get(key)
        if existing is None:
            result[key] = value
        elif isinstance(existing, str):
            result[key] = [existing, value]
        else:
            existing.append(value)
    return result


def _request_target(target: str) -> tuple[str, str]:
    path, separator, query = target.partition("?")
    return path, query if separator else ""


def _read_static(static_root: pathlib.Path, filename: str) -> bytes:
    """Read one literal file from a verified directory and stable file descriptor."""
    directory_fd = -1
    file_fd = -1
    proc_root = pathlib.Path("/proc/self/fd")
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or nofollow is None or not proc_root.is_dir():
        raise _UnsafeStaticFile
    try:
        root_lstat = static_root.lstat()
        if stat.S_ISLNK(root_lstat.st_mode) or not stat.S_ISDIR(root_lstat.st_mode):
            raise _UnsafeStaticFile
        expected_root = static_root.resolve(strict=True)
        directory_fd = os.open(
            static_root,
            os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or (directory_stat.st_dev, directory_stat.st_ino)
            != (root_lstat.st_dev, root_lstat.st_ino)
        ):
            raise _UnsafeStaticFile
        if (proc_root / str(directory_fd)).resolve(strict=True) != expected_root:
            raise _UnsafeStaticFile

        file_fd = os.open(
            filename,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        initial_stat = os.fstat(file_fd)
        if not stat.S_ISREG(initial_stat.st_mode):
            raise _UnsafeStaticFile
        opened_file = (proc_root / str(file_fd)).resolve(strict=True)
        if opened_file != expected_root / filename:
            raise _UnsafeStaticFile

        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)

        final_stat = os.fstat(file_fd)
        current_path_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (initial_stat.st_dev, initial_stat.st_ino, initial_stat.st_size, initial_stat.st_mtime_ns)
            != (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns)
            or (final_stat.st_dev, final_stat.st_ino)
            != (current_path_stat.st_dev, current_path_stat.st_ino)
            or not stat.S_ISREG(current_path_stat.st_mode)
            or (proc_root / str(directory_fd)).resolve(strict=True) != expected_root
        ):
            raise _UnsafeStaticFile
        current_root_stat = static_root.lstat()
        if (
            stat.S_ISLNK(current_root_stat.st_mode)
            or (current_root_stat.st_dev, current_root_stat.st_ino)
            != (directory_stat.st_dev, directory_stat.st_ino)
        ):
            raise _UnsafeStaticFile
        data = b"".join(chunks)
        if len(data) != final_stat.st_size:
            raise _UnsafeStaticFile
        return data
    except (OSError, RuntimeError, ValueError) as error:
        raise _UnsafeStaticFile from error
    finally:
        if file_fd >= 0:
            try:
                os.close(file_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "MokviaWeb"
    sys_version = ""
    _store: object
    _api_state: object
    _api_handle: Callable[..., tuple[int, dict[str, object]]]
    _static_root: pathlib.Path

    def version_string(self) -> str:
        return "MokviaWeb"

    def setup(self) -> None:
        super().setup()
        self.close_connection = True
        self._response_logged = False

    def parse_request(self) -> bool:
        # BaseHTTPRequestHandler collapses a leading `//` before dispatch.
        # This server never redirects and must instead enforce the raw, exact
        # allowlist, including rejection of every extra-slash spelling.
        raw_target: str | None = None
        try:
            words = self.raw_requestline.decode("iso-8859-1").rstrip("\r\n").split()
            if len(words) in {2, 3}:
                raw_target = words[1]
        except (AttributeError, UnicodeError):
            pass
        parsed = super().parse_request()
        if parsed and raw_target is not None:
            self.path = raw_target
        return parsed

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except Exception:
            if not self._response_logged:
                self._write_error(
                    500, "internal_error", "request could not be completed"
                )

    def log_message(self, format: str, *args: object) -> None:
        return

    def log_error(self, format: str, *args: object) -> None:
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return

    def _safe_method(self) -> str:
        method = getattr(self, "command", "")
        return method if method in _KNOWN_METHODS else "<other>"

    def _safe_route(self) -> str:
        target = getattr(self, "path", "")
        if not isinstance(target, str):
            return "<unknown>"
        path, _ = _request_target(target)
        if path in _SHELL_ROUTES or path in _STATIC_ROUTES or path in _READ_API_ROUTES:
            return path
        if path in _MUTATION_ROUTES:
            return path
        prefix = "/api/v1/entities/"
        components = path[len(prefix) :].split("/") if path.startswith(prefix) else []
        if len(components) == 2 and all(components):
            return "/api/v1/entities/{kind}/{id}"
        return "<unknown>"

    def _safe_log(self, status: int) -> None:
        if self._response_logged:
            return
        self._response_logged = True
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        line = f"{timestamp} {self._safe_method()} {self._safe_route()} {status}\n"
        lock = getattr(self.server, "_mokvia_log_lock", None)
        if isinstance(lock, type(threading.Lock())):
            with lock:
                sys.stderr.write(line)
                sys.stderr.flush()
        else:
            sys.stderr.write(line)
            sys.stderr.flush()

    def _write_response(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        omit_body: bool = False,
    ) -> None:
        self.close_connection = True
        try:
            # Parser errors may occur before BaseHTTPRequestHandler assigns a
            # response-capable request version. Security headers are mandatory
            # even on those paths, so always emit an HTTP/1.0 response.
            self.request_version = "HTTP/1.0"
            self.send_response_only(status)
            self.send_header("Server", self.version_string())
            self.send_header("Content-Type", content_type)
            for name, value in response_security_headers().items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not omit_body:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self._safe_log(status)

    def _write_json(self, status: int, payload: object, *, omit_body: bool = False) -> None:
        self._write_response(
            status,
            _json_bytes(payload),
            "application/json; charset=utf-8",
            omit_body=omit_body,
        )

    def _write_error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        omit_body: bool = False,
    ) -> None:
        self._write_json(status, _error_payload(code, message), omit_body=omit_body)

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        if code == http.HTTPStatus.NOT_IMPLEMENTED and hasattr(self, "path"):
            self._handle_other_method()
            return
        status = int(code)
        if status == http.HTTPStatus.REQUEST_URI_TOO_LONG:
            error_code = "bad_request"
        elif status == http.HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE:
            error_code = "bad_request"
        else:
            status = 400 if status < 400 or status >= 500 else status
            error_code = "bad_request"
        self._write_error(status, error_code, "request could not be parsed")

    def _api_response(
        self,
        method: str,
        path: str,
        query: Mapping[str, str | Sequence[str]],
        *,
        body: bytes | None = None,
        headers: object | None = None,
        omit_body: bool = False,
    ) -> None:
        try:
            status, payload = self._api_handle(
                self._store,
                method,
                path,
                query,
                headers,
                body,
                state=self._api_state,
            )
        except Exception:
            status, payload = 500, _error_payload(
                "internal_error", "request could not be completed"
            )
        self._write_json(status, payload, omit_body=omit_body)

    def _query_or_error(self, raw_query: str) -> Mapping[str, str | Sequence[str]] | None:
        try:
            return _parse_query(raw_query)
        except _InvalidQuery:
            self._write_error(400, "invalid_query", "query parameters are invalid")
            return None

    def _local_host_allowed(self) -> bool:
        allowed = getattr(self, "_strict_hosts", None)
        hosts = self.headers.get_all("Host", [])
        if allowed is not None and (len(hosts) != 1 or hosts[0] not in allowed):
            self._write_error(403, "forbidden", "Host is forbidden")
            return False
        return True

    def do_GET(self) -> None:
        if not self._local_host_allowed(): return
        path, raw_query = _request_target(self.path)
        if path in _SHELL_ROUTES:
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        asset = _STATIC_ROUTES.get(path)
        if asset is not None:
            self._serve_static(*asset)
            return
        if path.startswith("/api/v1/"):
            query = self._query_or_error(raw_query)
            if query is not None:
                self._api_response("GET", path, query)
            return
        self._write_error(404, "not_found", "route was not found")

    def _serve_static(
        self, filename: str, content_type: str, *, omit_body: bool = False
    ) -> None:
        try:
            body = _read_static(self._static_root, filename)
        except _UnsafeStaticFile:
            self._write_error(500, "static_unavailable", "static content is unavailable")
            return
        if filename == "index.html" and getattr(self, "_preview_read_only", False):
            if body.count(_PREVIEW_BANNER_MARKER) != 1:
                self._write_error(500, "static_unavailable", "static content is unavailable")
                return
            body = body.replace(_PREVIEW_BANNER_MARKER, _PREVIEW_BANNER)
        self._write_response(200, body, content_type, omit_body=omit_body)

    def _validate_mutation_metadata(self) -> bool:
        try:
            mutation_origins = getattr(self, "_mutation_origins", None)
            validate_mutation_headers(
                self.headers,
                allowed_origin=(
                    None
                    if mutation_origins is not None
                    else getattr(self, "_mutation_origin", PRODUCT_ORIGIN)
                ),
                allowed_origins=mutation_origins,
            )
        except ForbiddenRequestError:
            self._write_error(403, "forbidden", "mutation request is forbidden")
            return False
        except SecurityInputError:
            self._write_error(400, "invalid_request", "mutation request is invalid")
            return False
        if self.headers.defects:
            self._write_error(400, "invalid_request", "request framing is invalid")
            return False
        return True

    def _read_mutation_body(self) -> bytes | None:
        if self.headers.get_all("Transfer-Encoding"):
            self._write_error(400, "invalid_request", "request framing is invalid")
            return None
        if self.headers.get_all("Expect"):
            self._write_error(400, "invalid_request", "request framing is invalid")
            return None
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            self._write_error(400, "invalid_request", "request framing is invalid")
            return None
        value = lengths[0]
        if not isinstance(value, str) or _CONTENT_LENGTH.fullmatch(value) is None:
            self._write_error(400, "invalid_request", "request framing is invalid")
            return None
        try:
            length = int(value)
        except (ValueError, OverflowError):
            self._write_error(400, "invalid_request", "request framing is invalid")
            return None
        if length == 0:
            self._write_error(400, "invalid_request", "request body is required")
            return None
        if length > _MAX_BODY_BYTES:
            self._write_error(413, "payload_too_large", "request body is too large")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._write_error(400, "invalid_request", "request body is incomplete")
            return None
        return body

    def do_POST(self) -> None:
        if not self._local_host_allowed(): return
        path, raw_query = _request_target(self.path)
        if path in _MUTATION_ROUTES:
            if getattr(self, "_read_only", False):
                self._write_error(405, "read_only", "preview is read-only")
                return
            if not self._validate_mutation_metadata():
                return
            query = self._query_or_error(raw_query)
            if query is None:
                return
            body = self._read_mutation_body()
            if body is not None:
                self._api_response(
                    "POST", path, query, body=body, headers=self.headers
                )
            return
        if self._is_known_route(path):
            self._write_error(405, "method_not_allowed", "method is not allowed")
        else:
            self._write_error(404, "not_found", "route was not found")

    @staticmethod
    def _is_known_route(path: str) -> bool:
        if path in _SHELL_ROUTES or path in _STATIC_ROUTES:
            return True
        if path in _READ_API_ROUTES or path in _MUTATION_ROUTES:
            return True
        prefix = "/api/v1/entities/"
        if path.startswith(prefix):
            components = path[len(prefix) :].split("/")
            return len(components) == 2 and all(components)
        return False

    def _handle_other_method(self) -> None:
        path, _ = _request_target(getattr(self, "path", ""))
        if getattr(self, "command", "") == "HEAD":
            asset = _STATIC_ROUTES.get(path)
            if asset is not None:
                self._serve_static(*asset, omit_body=True)
                return
        self._write_error(
            405 if self._is_known_route(path) else 404,
            "method_not_allowed" if self._is_known_route(path) else "not_found",
            "method is not allowed" if self._is_known_route(path) else "route was not found",
            omit_body=getattr(self, "command", "") == "HEAD",
        )

    do_HEAD = _handle_other_method
    do_PUT = _handle_other_method
    do_PATCH = _handle_other_method
    do_DELETE = _handle_other_method
    do_OPTIONS = _handle_other_method
    do_TRACE = _handle_other_method
    do_CONNECT = _handle_other_method


def create_server(
    host: str,
    port: int,
    root: pathlib.Path,
    *,
    static_root: pathlib.Path | None = None,
    bind_origin: str = PRODUCT_ORIGIN,
    mutation_origins: Sequence[str] | None = None,
    health_details: Mapping[str, object] | None = None,
    expected_root_identity: tuple[int, int] | None = None,
    read_only: bool = False,
    capture_folder: pathlib.Path | None = None,
    outlook_folder: pathlib.Path | None = None,
    outlook_collector: object | None = None,
    integration_specs=None,
    integration_options=None,
    integration_availability=None,
    integration_services=None,
    extra_handler: Callable | None = None,
    strict_hosts: tuple[str, ...] | None = None,
    timetracker_config: pathlib.Path | None = None,
) -> ThreadingHTTPServer:
    """Create a closed-by-default threaded server over one long-lived Store."""
    from webapp.api import ApiState, handle
    from webapp.store import Store, InputError
    from webapp.local_calendar import calendar_projection, JST
    from webapp.local_capture import CaptureImporter
    from webapp.outlook_import import OutlookImport, ImportError as OutlookImportError

    if read_only and (capture_folder is not None or outlook_folder is not None or outlook_collector is not None):
        raise ValueError("capture cannot run in a read-only preview")

    configured_mutation_origins = (
        None
        if mutation_origins is None
        else normalize_mutation_origins(mutation_origins)
    )
    store = Store(
        pathlib.Path(root), expected_root_identity=expected_root_identity
    )
    try:
        # Prime from the Store's own inode-bound byte snapshot. Preflight may
        # have validated an earlier filesystem state and cannot seed this cache.
        store.repository_errors()
        importer = CaptureImporter(root, capture_folder, bind_origin=bind_origin) if capture_folder is not None else None
        outlook = OutlookImport(root, outlook_folder, collector=outlook_collector)
    except BaseException:
        store.close()
        raise
    try:
        from webapp.integrations.registry import Registry
        from webapp.integrations.reflections import ActualCatalog, ReflectionService
        from webapp.integrations.state import IntegrationError
        from webapp.timetracker_nx import NXError, ResultUnknown
        from webapp.integration_plugins.outlook import saved_actuals, saved_events
        services={"outlook_events":outlook, **(integration_services or {})}
        if timetracker_config is not None:
            from webapp.timetracker_nx_transport import load_integration_configuration
            if "timetracker_nx_transport" in services or (integration_options or {}).get("timetracker_nx"):
                raise NXError("configuration_invalid")
            nx_options,nx_services=load_integration_configuration(timetracker_config)
            integration_options={**(integration_options or {}),**nx_options}
            services.update(nx_services)
        if "google_status" not in services:
            def google_status():
                from calendar_sync.status import read_calendar_sync_status
                return read_calendar_sync_status(runtime_root=pathlib.Path(root)/"private"/"calendar-sync")
            services["google_status"]=google_status
        registry=Registry(root,services=services,options=integration_options,availability=integration_availability,specs=integration_specs)
        actual_catalog=ActualCatalog(store,[lambda:saved_actuals(outlook)])
        reflections=ReflectionService(registry,actual_catalog)
    except BaseException:
        store.close()
        raise
    api_state = ApiState(store)
    configured_static_root = (
        pathlib.Path(__file__).with_name("static")
        if static_root is None
        else pathlib.Path(static_root)
    ).absolute()

    class RequestHandler(_RequestHandler):
        pass

    def api_handle(*args: object, **kwargs: object) -> tuple[int, dict[str, object]]:
        if extra_handler is not None:
            extra = extra_handler(*args[:6])
            if extra is not None:
                return extra
        _, method, path, query = args[:4]
        if path == "/api/v1/capture-import/status":
            if method != "GET" or query:
                return 400, _error_payload("invalid_request", "GET without query is required")
            return 200, importer.status() if importer is not None else {"enabled": False}
        if path in {"/api/v1/integrations/status", "/api/v1/actuals", "/api/v1/integrations/settings", "/api/v1/integrations/actual-write", "/api/v1/integrations/actual-preview", "/api/v1/integrations/actual-apply", "/api/v1/integrations/actual-reconcile", "/api/v1/integrations/task"}:
            try:
                if method=="GET" and path=="/api/v1/integrations/status" and not query:return 200,registry.statuses()
                if method=="GET" and path=="/api/v1/actuals" and not query:
                    records=list(actual_catalog.read().values())
                    writers=[name for name,spec in registry.specs.items() if "actual.write" in spec.capabilities]
                    return 200,{"actuals":[record.to_dict() for record in records],"reflection":{name:[reflections.status(name,record) for record in records] for name in writers}}
                if method!="POST" or query:raise IntegrationError("invalid_request")
                if store.read_snapshot().recovery_required:raise IntegrationError("recovery_required")
                operation=json.loads(args[5])
                if path=="/api/v1/integrations/settings" and isinstance(operation,dict) and set(operation)=={"id","enabled","revision"}:
                    return 200,registry.state.set_enabled(operation["id"],operation["enabled"],operation["revision"])
                if path=="/api/v1/integrations/actual-preview" and isinstance(operation,dict) and set(operation)=={"id","references"}:
                    return 200,reflections.preview(operation["id"],operation["references"])
                if path=="/api/v1/integrations/actual-apply" and isinstance(operation,dict) and set(operation)=={"id","references","token"}:
                    return 200,reflections.apply(operation["id"],operation["references"],operation["token"])
                if path=="/api/v1/integrations/actual-reconcile" and isinstance(operation,dict) and set(operation)=={"id","reference_id"}:
                    return 200,reflections.reconcile(operation["id"],operation["reference_id"])
                if path=="/api/v1/integrations/task" and isinstance(operation,dict) and set(operation)=={"id","reference_id","task_url","work_item_id","categories"}:
                    return 200,reflections.register_task(operation["id"],operation["reference_id"],operation["task_url"],operation["work_item_id"],operation["categories"])
                if path=="/api/v1/integrations/actual-write":
                    raise IntegrationError("preview_required")
                raise IntegrationError("invalid_request")
            except (IntegrationError, NXError, ValueError,TypeError,KeyError,ResultUnknown) as error:
                code=str(error) if isinstance(error,IntegrationError) else error.code if isinstance(error,NXError) else "unknown" if isinstance(error,ResultUnknown) else "invalid_request"
                return (409 if code in {"busy","conflict","stale_preview","reflection_requires_readback"} else 400),_error_payload(code,"連携状態を確認してください。")
        if path.startswith("/api/v1/outlook-import/"):
            try:
                if query: raise OutlookImportError("invalid_request")
                if path == "/api/v1/outlook-import/status" and method == "GET":
                    return 200, {**outlook.status(), "enabled":outlook.status()["enabled"] and registry.state.enabled("outlook"), "plugin_enabled":registry.state.enabled("outlook")}
                if method != "POST": raise OutlookImportError("invalid_request")
                if store.read_snapshot().recovery_required: raise OutlookImportError("recovery_required")
                operation = json.loads(args[5])
                if not isinstance(operation, dict): raise OutlookImportError("invalid_request")
                if path == "/api/v1/outlook-import/run" and set(operation) == {"source", "from", "to"}:
                    return 200, registry.require("outlook","calendar.read").fetch_calendar(operation["source"], operation["from"], operation["to"])
                if path == "/api/v1/outlook-import/local" and {"key", "revision", "done"} <= set(operation) and not set(operation)-{"key", "revision", "done", "actual", "undo"}:
                    return 200, outlook.annotate(operation["key"], operation["revision"], operation["done"], actual=operation.get("actual"), undo=operation.get("undo"))
                raise OutlookImportError("invalid_request")
            except (OutlookImportError, IntegrationError, ValueError, TypeError, KeyError) as error:
                code = str(error) if isinstance(error, (OutlookImportError,IntegrationError)) else "invalid_request"
                return (409 if code in {"busy", "conflict"} else 400), _error_payload(code, "取り込みを保存できませんでした。状態を再読込してください。")
        if path == "/api/v1/calendar":
            if method != "GET" or set(query) - {"view", "date"} or any(not isinstance(v, str) for v in query.values()):
                return 400, _error_payload("invalid_calendar_query", "日付または表示範囲を確認してください")
            try:
                return 200, calendar_projection(store, query.get("view", "month"), query.get("date", datetime.datetime.now(JST).date().isoformat()), external=saved_events(outlook))
            except InputError:
                return 400, _error_payload("invalid_calendar_query", "日付または表示範囲を確認してください")
        kwargs["bind_origin"] = bind_origin
        kwargs["mutation_origins"] = configured_mutation_origins
        kwargs["health_details"] = health_details
        return handle(*args, **kwargs)  # type: ignore[arg-type]

    RequestHandler._strict_hosts = strict_hosts
    RequestHandler._store = store
    RequestHandler._api_state = api_state
    RequestHandler._api_handle = staticmethod(api_handle)
    RequestHandler._mutation_origin = bind_origin
    RequestHandler._mutation_origins = configured_mutation_origins
    RequestHandler._read_only = read_only
    RequestHandler._preview_read_only = (
        read_only is True
        and isinstance(health_details, Mapping)
        and health_details.get("mode") == "preview"
        and health_details.get("read_only") is True
    )
    RequestHandler._static_root = configured_static_root
    try:
        server = _Server((host, port), RequestHandler)
    except BaseException:
        store.close()
        raise
    server._mokvia_store = store  # type: ignore[attr-defined]
    server._mokvia_api_state = api_state  # type: ignore[attr-defined]
    server._mokvia_log_lock = threading.Lock()  # type: ignore[attr-defined]
    server._capture_importer = importer  # type: ignore[attr-defined]
    if importer is not None:
        try:
            importer.start(store)
        except BaseException:
            server.server_close()
            raise
    return server


def _bind_failure_message(error: OSError) -> str:
    if error.errno == errno.EADDRINUSE:
        return (
            "port 24873 is in use; refusing to start. "
            "Do not stop or reconfigure the other process."
        )
    if error.errno == errno.EADDRNOTAVAIL:
        return "Local bind address is unavailable; refusing to start."
    return "server bind failed; refusing to start."


def main(argv=None):
    from local_runtime import main as run
    return run()

if __name__ == "__main__":
    raise SystemExit(main())
