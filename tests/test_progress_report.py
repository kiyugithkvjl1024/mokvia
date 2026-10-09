import datetime
import unittest

from webapp.progress_report import monthly_progress_report, progress_period
from webapp.store import Entity, InputError


def entity(entity_type, entity_id, *, body="", path=None, **frontmatter):
    return Entity(
        entity_id=entity_id,
        entity_type=entity_type,
        relative_path=path or f"{entity_type}s/{entity_id}.md",
        frontmatter={"id": entity_id, "title": entity_id, **frontmatter},
        body=body,
        content_hash="a" * 64,
    )


class ProgressReportTest(unittest.TestCase):
    def test_period_is_inclusive_sorted_and_extracts_sections(self):
        """Catches a date-boundary loss or report body extraction regression."""
        entities = (
            entity(
                "progress", "progress-20260802-002", occurred_on="2026-08-02",
                body="intro\n## メリット・学び\n\n学び\n\n## 証拠\n\nURL\n",
            ),
            entity(
                "progress", "progress-20260802-001", occurred_on="2026-08-02",
                body="## メリット・学び\n\n先\n\n### 小見出し\n残す\n",
            ),
            entity("progress", "progress-20260803-001", occurred_on="2026-08-03"),
        )

        actual = progress_period(
            entities, datetime.date(2026, 8, 2), datetime.date(2026, 8, 2)
        )

        self.assertEqual(
            [item["id"] for item in actual],
            ["progress-20260802-001", "progress-20260802-002"],
        )
        self.assertEqual(actual[0]["benefit_learning"], "先\n\n### 小見出し\n残す")
        self.assertEqual(actual[1]["evidence"], "URL")

    def test_period_uses_occurred_on_instead_of_registration_date(self):
        records = (
            entity("progress", "late-registration", occurred_on="2026-10-09", created_at="2026-10-10T03:00:00+09:00"),
            entity("progress", "old-registration", occurred_on="2026-10-10", created_at="2026-10-08T03:00:00+09:00"),
            entity("progress", "outside", occurred_on="2026-10-08", created_at="2026-10-10T03:00:00+09:00"),
        )
        self.assertEqual([r["id"] for r in progress_period(records, datetime.date(2026,10,9), datetime.date(2026,10,10))], ["late-registration", "old-registration"])

    def test_month_resolves_archived_project_through_outcome_to_goal(self):
        """Catches a monthly report that loses historical Project context."""
        entities = (
            entity("goal", "goal-1", title="Goal"),
            entity("area", "area-1", title="Area"),
            entity("roadmap_outcome", "outcome-1", title="Outcome", goal_id="goal-1"),
            entity(
                "project", "project-1", title="Project", path="archive/project-1.md",
                roadmap_outcome_id="outcome-1", area_id="area-1",
            ),
            entity(
                "progress", "progress-20260803-001", title="変化",
                occurred_on="2026-08-03", project_id="project-1",
            ),
        )

        report = monthly_progress_report(entities, "2026-08")

        self.assertEqual(report["count"], 1)
        self.assertEqual(report["groups"][0]["derived"], {  # type: ignore[index]
            "roadmap_outcome": {"id": "outcome-1", "title": "Outcome"},
            "goal": {"id": "goal-1", "title": "Goal"},
            "area": {"id": "area-1", "title": "Area"},
        })
        self.assertIn("# 2026-08 実績台帳", report["markdown"])
        for expected in (
            "Roadmap Outcome: Outcome (outcome-1)",
            "Goal: Goal (goal-1)",
            "Area: Area (area-1)",
        ):
            self.assertEqual(report["markdown"].count(expected), 1)

    def test_month_fails_closed_for_missing_origin(self):
        """Catches reports that silently invent an origin for broken history."""
        progress = entity(
            "progress", "progress-20260803-001", occurred_on="2026-08-03",
            project_id="missing",
        )

        with self.assertRaises(InputError):
            monthly_progress_report((progress,), "2026-08")
