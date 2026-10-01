import pathlib
import tempfile
import unittest

from scripts.validate_frontmatter import validate_repository


class RoadmapOutcomeTargetDateValidationTest(unittest.TestCase):
    def test_target_date_is_optional_and_requires_a_real_iso_date(self) -> None:
        base = """---
id: roadmap-outcome-20260826-001
type: roadmap_outcome
title: Outcome
status: active
goal_id: goal-20260826-001
roadmap_lane: next
roadmap_position: 1
created_at: 2026-08-26T09:00:00+09:00
updated_at: 2026-08-26T09:00:00+09:00
---

## 成功条件

Done.

## メモ
"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            outcome = root / "roadmap-outcomes/outcome.md"
            outcome.parent.mkdir()
            goals = root / "goals"
            goals.mkdir()
            (goals / "goal.md").write_text(
                """---
id: goal-20260826-001
type: goal
title: Goal
status: active
created_at: 2026-08-26T09:00:00+09:00
updated_at: 2026-08-26T09:00:00+09:00
---
""",
                encoding="utf-8",
            )

            for target_date, expected_error in (
                (None, None),
                ("2027-01-31", None),
                ("2027-02-30", "invalid date for target_date: 2027-02-30"),
            ):
                with self.subTest(target_date=target_date):
                    text = base
                    if target_date is not None:
                        text = text.replace("updated_at:", f"target_date: {target_date}\nupdated_at:")
                    outcome.write_text(text, encoding="utf-8")
                    errors = validate_repository(root)
                    if expected_error is None:
                        self.assertEqual(errors, [])
                    else:
                        self.assertTrue(any(expected_error in error for error in errors), errors)
