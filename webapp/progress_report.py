"""Read-only Progress period and monthly report projections."""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence

from webapp.store import Entity, InputError


_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}", re.ASCII)
_ORIGIN_FIELDS = (
    ("project_id", "project"),
    ("goal_id", "goal"),
    ("area_id", "area"),
)


def _entity_index(entities: Sequence[Entity]) -> dict[str, Entity]:
    indexed: dict[str, Entity] = {}
    for entity in entities:
        if entity.entity_id in indexed:
            raise InputError("duplicate entity id")
        indexed[entity.entity_id] = entity
    return indexed


def _section(body: str, heading: str) -> str:
    lines = body.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return ""
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip()


def _origin(entity: Entity, indexed: dict[str, Entity]) -> dict[str, str] | None:
    origins = [
        (field, kind, entity.frontmatter[field])
        for field, kind in _ORIGIN_FIELDS
        if entity.frontmatter.get(field)
    ]
    if len(origins) > 1:
        raise InputError("Progress origin must contain at most one reference")
    if not origins:
        return None
    _, kind, entity_id = origins[0]
    target = indexed.get(entity_id)
    if target is None or target.entity_type != kind:
        raise InputError("invalid Progress origin")
    return {"kind": kind, "id": target.entity_id, "title": target.frontmatter.get("title", "")}


def _progress_entities(
    entities: Sequence[Entity], start: datetime.date, end: datetime.date
) -> tuple[Entity, ...]:
    if start > end:
        raise InputError("invalid Progress period")
    selected: list[Entity] = []
    for entity in entities:
        if entity.entity_type != "progress" or entity.relative_path.startswith("archive/"):
            continue
        try:
            occurred_on = datetime.date.fromisoformat(entity.frontmatter.get("occurred_on", ""))
        except ValueError as error:
            raise InputError("invalid Progress date") from error
        if start <= occurred_on <= end:
            selected.append(entity)
    return tuple(sorted(selected, key=lambda item: (item.frontmatter.get("occurred_on", ""), item.entity_id)))


def _item(entity: Entity, indexed: dict[str, Entity]) -> dict[str, object]:
    evidence = _section(entity.body, "## 証拠")
    return {
        "id": entity.entity_id,
        "title": entity.frontmatter.get("title", ""),
        "occurred_on": entity.frontmatter.get("occurred_on", ""),
        "origin": _origin(entity, indexed),
        "benefit_learning": _section(entity.body, "## メリット・学び"),
        "evidence": evidence,
        "evidence_present": bool(evidence),
    }


def progress_period(
    entities: Sequence[Entity], start: datetime.date, end: datetime.date
) -> tuple[dict[str, object], ...]:
    """Return active Progress records in an inclusive date range."""
    indexed = _entity_index(entities)
    return tuple(_item(entity, indexed) for entity in _progress_entities(entities, start, end))


def _month_range(month: str) -> tuple[datetime.date, datetime.date]:
    if not isinstance(month, str) or _MONTH.fullmatch(month) is None:
        raise InputError("invalid month")
    try:
        start = datetime.date.fromisoformat(f"{month}-01")
    except ValueError as error:
        raise InputError("invalid month") from error
    end = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    return start, end


def _derived_project_context(
    origin_id: str, indexed: dict[str, Entity]
) -> dict[str, dict[str, str]]:
    project = indexed[origin_id]
    result: dict[str, dict[str, str]] = {}
    goal_id = project.frontmatter.get("goal_id")
    outcome_id = project.frontmatter.get("roadmap_outcome_id")
    if goal_id and outcome_id:
        raise InputError("project has ambiguous Goal reference")
    if outcome_id:
        outcome = indexed.get(outcome_id)
        if outcome is None or outcome.entity_type != "roadmap_outcome":
            raise InputError("invalid Roadmap Outcome reference")
        result["roadmap_outcome"] = {
            "id": outcome.entity_id,
            "title": outcome.frontmatter.get("title", ""),
        }
        goal_id = outcome.frontmatter.get("goal_id")
    if goal_id:
        goal = indexed.get(goal_id)
        if goal is None or goal.entity_type != "goal":
            raise InputError("invalid Goal reference")
        result["goal"] = {"id": goal.entity_id, "title": goal.frontmatter.get("title", "")}
    area_id = project.frontmatter.get("area_id")
    if area_id:
        area = indexed.get(area_id)
        if area is None or area.entity_type != "area":
            raise InputError("invalid Area reference")
        result["area"] = {"id": area.entity_id, "title": area.frontmatter.get("title", "")}
    return result


def _markdown(month: str, groups: list[dict[str, object]], unclassified: list[dict[str, object]]) -> str:
    lines = [f"# {month} 実績台帳", "", "## 実績"]
    for group in groups:
        lines.extend(("", f"### {group['kind']}: {group['title']}"))
        derived = group.get("derived", {})
        if isinstance(derived, dict) and derived:
            lines.extend(("", "#### 文脈"))
            for key, label in (
                ("roadmap_outcome", "Roadmap Outcome"),
                ("goal", "Goal"),
                ("area", "Area"),
            ):
                context = derived.get(key)
                if isinstance(context, dict):
                    lines.append(f"- {label}: {context['title']} ({context['id']})")
            lines.extend(("", "#### 実績項目"))
        lines.extend(
            f"- {item['occurred_on']} {item['title']} ({item['id']})"
            for item in group["items"]  # type: ignore[index]
        )
    if unclassified:
        lines.extend(("", "### 未分類"))
        lines.extend(f"- {item['occurred_on']} {item['title']} ({item['id']})" for item in unclassified)
    return "\n".join(lines) + "\n"


def monthly_progress_report(entities: Sequence[Entity], month: str) -> dict[str, object]:
    """Build a deterministic, non-persistent monthly Progress projection."""
    start, end = _month_range(month)
    indexed = _entity_index(entities)
    items = list(progress_period(entities, start, end))
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    unclassified: list[dict[str, object]] = []
    for item in items:
        origin = item["origin"]
        if origin is None:
            unclassified.append(item)
            continue
        grouped.setdefault((origin["kind"], origin["id"]), []).append(item)  # type: ignore[index]
    groups = [
        {
            "kind": kind,
            "id": entity_id,
            "title": indexed[entity_id].frontmatter.get("title", ""),
            "items": group_items,
            "derived": _derived_project_context(entity_id, indexed) if kind == "project" else {},
        }
        for (kind, entity_id), group_items in sorted(grouped.items())
    ]
    evidence_present = sum(item["evidence_present"] is True for item in items)
    return {
        "period": {"month": month, "start": start.isoformat(), "end": end.isoformat()},
        "count": len(items),
        "items": items,
        "groups": groups,
        "unclassified": unclassified,
        "evidence": {"present": evidence_present, "missing": len(items) - evidence_present},
        "markdown": _markdown(month, groups, unclassified),
    }
