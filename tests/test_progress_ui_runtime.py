import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProgressUiRuntimeTest(unittest.TestCase):
    def test_progress_ui_runtime_contract(self):
        result = subprocess.run(["node", "tests/progress_ui_runtime.js"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_daily_progress_dates_and_grouping(self):
        result = subprocess.run(["node", "tests/daily_progress_runtime.js"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_weekly_progress_edit_link_explicitly_selects_daily(self):
        source = (ROOT / "webapp" / "static" / "progress.js").read_text(encoding="utf-8")
        self.assertIn('"/reviews/weekly?tab=daily&progress="', source)

    def test_progress_keeps_required_fields_visible_and_collapses_optional_fields(self):
        source = (ROOT / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
        optional_start = source.index('<details id="progress-optional-fields"')
        optional_end = source.index("</details>", optional_start)
        optional = source[optional_start:optional_end]
        self.assertLess(source.index('id="progress-title"'), optional_start)
        self.assertLess(source.index('id="progress-occurred-on"'), optional_start)
        self.assertIn('id="progress-origin"', optional)
        self.assertIn('id="progress-benefit"', optional)
        self.assertIn('id="progress-evidence"', optional)
        self.assertNotIn(" open>", optional.split(">", 1)[0] + ">")

    def test_progress_optional_details_open_only_for_existing_values_and_preserves_inputs(self):
        source = (ROOT / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("pof.open", source)
        self.assertRegex(source, r"pof\.open\s*=\s*Boolean\([^)]*progressBenefit")
        self.assertRegex(source, r"pof\.open\s*=\s*Boolean\([^)]*progressEvidence")
        self.assertIn("pof.open = false", source)
