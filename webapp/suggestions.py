"""Pure history-based suggestions for Focus Quick Start."""

from __future__ import annotations

import datetime
import unicodedata
from collections import defaultdict
from collections.abc import Iterable

from webapp.store import Entity


_JST = datetime.timezone(datetime.timedelta(hours=9))


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _parse_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _time_band(value: datetime.datetime) -> str:
    hour = value.astimezone(_JST).hour
    if hour < 5:
        return "midnight"
    if hour < 10:
        return "morning"
    if hour < 14:
        return "daytime"
    if hour < 18:
        return "afternoon"
    return "night"


def build_quick_start_suggestions(
    entities: Iterable[Entity],
    now: datetime.datetime,
    query: str,
    limit: int = 5,
) -> list[dict[str, str | None]]:
    """Return deterministic, read-only title suggestions from Task start history."""
    if not isinstance(query, str) or not isinstance(limit, int) or limit < 1:
        return []
    if now.tzinfo is None or now.utcoffset() is None:
        return []

    all_entities = list(entities)
    active_area_ids = {
        item.entity_id
        for item in all_entities
        if item.entity_type == "area" and not item.relative_path.startswith("archive/")
    }
    grouped: dict[str, list[tuple[datetime.datetime, Entity]]] = defaultdict(list)
    for item in all_entities:
        if item.entity_type != "task" or item.frontmatter.get("timer_kind") == "break":
            continue
        title = item.frontmatter.get("title", "")
        normalized_title = _normalized_text(title)
        started_at = _parse_timestamp(item.frontmatter.get("work_started_at"))
        if not normalized_title or started_at is None:
            continue
        grouped[normalized_title].append((started_at, item))

    normalized_query = _normalized_text(query)
    local_now = now.astimezone(_JST)
    current_weekday = local_now.weekday()
    current_band = _time_band(local_now)
    ranked: list[tuple[tuple[object, ...], dict[str, str | None]]] = []
    for normalized_title, occurrences in grouped.items():
        if normalized_query and normalized_query not in normalized_title:
            continue
        if not normalized_query and len(occurrences) < 2:
            continue
        newest_at, newest_item = max(
            occurrences,
            key=lambda item: (item[0], item[1].relative_path, item[1].entity_id),
        )
        area_id = next(
            (
                candidate
                for _started, item in sorted(
                    occurrences,
                    key=lambda item: (item[0], item[1].relative_path, item[1].entity_id),
                    reverse=True,
                )
                if (candidate := item.frontmatter.get("area_id")) in active_area_ids
            ),
            None,
        )
        weekday_count = sum(
            started.astimezone(_JST).weekday() == current_weekday
            for started, _item in occurrences
        )
        time_band_count = sum(
            _time_band(started) == current_band for started, _item in occurrences
        )
        weekday_time_count = sum(
            started.astimezone(_JST).weekday() == current_weekday
            and _time_band(started) == current_band
            for started, _item in occurrences
        )
        if normalized_query:
            prefix_rank = 0 if normalized_title.startswith(normalized_query) else 1
            key = (
                prefix_rank,
                -weekday_time_count,
                -weekday_count,
                -time_band_count,
                -len(occurrences),
                -newest_at.timestamp(),
                normalized_title,
            )
            reason_code = "query_match"
        else:
            key = (
                -weekday_time_count,
                -weekday_count,
                -time_band_count,
                -len(occurrences),
                -newest_at.timestamp(),
                normalized_title,
            )
            reason_code = (
                "weekday_time" if weekday_time_count else
                "weekday" if weekday_count else
                "time_band" if time_band_count else
                "frequent"
            )
        ranked.append((key, {
            "title": newest_item.frontmatter["title"],
            "area_id": area_id,
            "reason_code": reason_code,
        }))
    return [suggestion for _key, suggestion in sorted(ranked, key=lambda item: item[0])[:limit]]
