"""Pure validation helpers for the local Web app's HTTP boundary."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any


class ForbiddenRequestError(Exception):
    """Raised when required browser mutation metadata is forbidden."""


class SecurityInputError(Exception):
    """Raised when security-sensitive request input is malformed."""


ALLOWED_MUTATION_ORIGIN = "http://127.0.0.1:24873"
PRODUCT_HTTPS_ORIGIN = "http://localhost:24873"
DEDICATED_PRODUCT_HTTPS_ORIGIN = "http://localhost:24873"
PRODUCT_MUTATION_ORIGINS = ("http://localhost:24873", "http://127.0.0.1:24873")
_ENTITY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}", re.ASCII)
_JSON_CONTENT_TYPE_PATTERN = re.compile(
    r"application/json[ \t]*(?:;[ \t]*charset[ \t]*=[ \t]*utf-8[ \t]*)?",
    re.ASCII | re.IGNORECASE,
)

RESPONSE_SECURITY_HEADERS: Mapping[str, str] = MappingProxyType(
    {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
)


def _header_values(headers: object) -> dict[str, list[Any]]:
    """Collect header values without losing case-insensitive duplicates."""
    items = getattr(headers, "items", None)
    if not callable(items):
        raise SecurityInputError("request headers must be a mapping")

    normalized: dict[str, list[Any]] = {}
    try:
        pairs = items()
        for name, value in pairs:
            if not isinstance(name, str):
                raise SecurityInputError("request header names must be strings")
            normalized.setdefault(name.lower(), []).append(value)
    except SecurityInputError:
        raise
    except (TypeError, ValueError) as error:
        raise SecurityInputError("request headers must contain name/value pairs") from error
    return normalized


def _single_header(
    headers: dict[str, list[Any]],
    name: str,
    error_type: type[ForbiddenRequestError] | type[SecurityInputError],
) -> Any:
    values = headers.get(name.lower(), [])
    if len(values) != 1:
        raise error_type(f"exactly one {name} header is required")
    return values[0]


def normalize_mutation_origins(origins: Sequence[str]) -> tuple[str, ...]:
    """Return an exact, unambiguous mutation Origin allowlist."""
    if isinstance(origins, (str, bytes)) or not origins:
        raise ValueError("mutation origins must be a non-empty sequence")
    normalized = tuple(origins)
    if len(set(normalized)) != len(normalized):
        raise ValueError("mutation origins must be unique")
    for origin in normalized:
        if not isinstance(origin, str) or origin.strip() != origin or "," in origin:
            raise ValueError("mutation origins must be exact strings")
        try:
            parsed = urllib.parse.urlsplit(origin)
            _ = parsed.port
        except ValueError as error:
            raise ValueError("mutation origin is invalid") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise ValueError("mutation origin is invalid")
    return normalized


def validate_mutation_headers(
    headers: object,
    *,
    allowed_origin: str | None = ALLOWED_MUTATION_ORIGIN,
    allowed_origins: Sequence[str] | None = None,
) -> None:
    """Validate metadata required before a mutation body may be parsed."""
    normalized = _header_values(headers)

    if allowed_origins is not None:
        configured_origins = normalize_mutation_origins(allowed_origins)
    elif allowed_origin is not None:
        configured_origins = normalize_mutation_origins((allowed_origin,))
    else:
        raise ValueError("one mutation Origin policy is required")

    origin = _single_header(normalized, "Origin", ForbiddenRequestError)
    if origin not in configured_origins:
        raise ForbiddenRequestError("mutation Origin is forbidden")

    web_marker = _single_header(normalized, "X-Mokvia-Web", ForbiddenRequestError)
    if web_marker != "1":
        raise ForbiddenRequestError("mutation marker is forbidden")

    content_type = _single_header(normalized, "Content-Type", SecurityInputError)
    if not isinstance(content_type, str) or _JSON_CONTENT_TYPE_PATTERN.fullmatch(
        content_type
    ) is None:
        raise SecurityInputError("Content-Type must be UTF-8 application/json")


def validate_entity_id(entity_id: str) -> str:
    """Return a request entity ID only when it matches the ASCII allowlist."""
    if not isinstance(entity_id, str) or _ENTITY_ID_PATTERN.fullmatch(entity_id) is None:
        raise SecurityInputError("entity ID is invalid")
    return entity_id


def response_security_headers() -> dict[str, str]:
    """Return an independent copy of the mandatory response headers."""
    return dict(RESPONSE_SECURITY_HEADERS)
