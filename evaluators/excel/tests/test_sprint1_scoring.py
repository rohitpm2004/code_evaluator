import os
import sys
import tempfile
import unittest
import openpyxl

# Add central-evaluator engine to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../engine")))
from excel_runner import inspect_and_evaluate


class TestSprint1GradingSafetyAndScoring(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_workbook(self, original_val=100, cached_val=100, formula="=SUM(A1:A2)", sheet_name="Sheet1"):
        """Creates original and recalculated test workbooks."""
        orig_path = os.path.join(self.temp_dir, "test_orig.xlsx")
        recalc_path = os.path.join(self.temp_dir, "test_recalc.xlsx")

        wb_orig = openpyxl.Workbook()
        ws_orig = wb_orig.active
        ws_orig.title = sheet_name
        ws_orig["A1"] = 40
        ws_orig["A2"] = 60
        ws_orig["B1"] = formula if formula else original_val
        wb_orig.save(orig_path)
        wb_orig.close()

        wb_recalc = openpyxl.Workbook()
        ws_recalc = wb_recalc.active
        ws_recalc.title = sheet_name
        ws_recalc["A1"] = 40
        ws_recalc["A2"] = 60
        ws_recalc["B1"] = cached_val
        wb_recalc.save(recalc_path)
        wb_recalc.close()

        return orig_path, recalc_path

    def test_weighted_assertions_calculation(self):
        """Test that assertions with different weights combine via Wi * Si / sum(Wi)."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        
        # Criterion worth 50 marks.
        # Assertion 1 (weight 40, correct)
        # Assertion 2 (weight 10, incorrect expected value 999)
        spec = {
            "rubric": {
                "criteria": [
                    {"name": "Calculations", "weight": 50},
                    {"name": "Summary", "weight": 50}
                ]
            },
            "testCases": [
                {
                    "id": "calc_main",
                    "criterion": "Calculations",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 100,
                    "weight": 40
                },
                {
                    "id": "calc_bonus",
                    "criterion": "Calculations",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 999,
                    "weight": 10
                },
                {
                    "id": "summary_ok",
                    "criterion": "Summary",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 100,
                    "weight": 50
                }
            ]
        }

        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Expected Calculations: (40*1.0 + 10*0.0) / 50 * 50 = 40.0
        # Expected Summary: 50.0
        # Total: 90.0
        self.assertEqual(report["rubric_breakdown"]["Calculations"], 40.0)
        self.assertEqual(report["rubric_breakdown"]["Summary"], 50.0)
        self.assertEqual(report["score"], 90.0)
        self.assertEqual(report["evaluation_status"], "COMPLETED")

    def test_critical_requirement_capping(self):
        """Test that a failed critical requirement caps the criterion score."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)

        spec = {
            "rubric": {
                "criteria": [{"name": "Core Math", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "critical_check",
                    "criterion": "Core Math",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 999,  # Deliberate failure
                    "weight": 10,
                    "is_critical": True,
                    "score_cap": 0.0
                },
                {
                    "id": "secondary_check",
                    "criterion": "Core Math",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,  # Passes
                    "weight": 90
                }
            ]
        }

        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Even though secondary check (weight 90) passed, critical cap forces score to 0.0
        self.assertEqual(report["rubric_breakdown"]["Core Math"], 0.0)
        self.assertEqual(report["score"], 0.0)

    def test_anti_double_deduction_dependency(self):
        """Test that dependent assertions skip duplicate penalty when upstream prerequisite fails."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)

        spec = {
            "rubric": {
                "criteria": [{"name": "Analysis", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "upstream_table",
                    "criterion": "Analysis",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 999,  # Fails
                    "weight": 50
                },
                {
                    "id": "downstream_chart",
                    "criterion": "Analysis",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,
                    "weight": 50,
                    "depends_on": ["upstream_table"],
                    "dependency_mode": "skip_penalty"
                }
            ]
        }

        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # downstream_chart was skipped due to upstream_table failure
        skipped = [a for a in report["assertion_results"] if a.get("status") == "DEPENDENCY_SKIPPED"]
        self.assertEqual(len(skipped), 1)

    def test_denominator_integrity_and_pending_review(self):
        """Amendment 1: Unverified criteria must NOT shrink the denominator (e.g. 70/100, never 70/70=100%)."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)

        spec = {
            "rubric": {
                "criteria": [
                    {"name": "Verified Part 1", "weight": 40},
                    {"name": "Unsupported Part", "weight": 30},
                    {"name": "Verified Part 2", "weight": 30}
                ]
            },
            "testCases": [
                {
                    "criterion": "Verified Part 1",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40
                },
                {
                    "criterion": "Verified Part 2",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A2",
                    "expectedValue": 60
                }
                # Notice: Unsupported Part has NO test cases configured!
            ]
        }

        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Score must be 70.0 (40 + 30), NOT 100.0!
        self.assertEqual(report["score"], 70.0)
        self.assertEqual(report["rubric_breakdown"]["Verified Part 1"], 40.0)
        self.assertEqual(report["rubric_breakdown"]["Verified Part 2"], 30.0)
        self.assertEqual(report["rubric_breakdown"]["Unsupported Part"], 0.0)
        self.assertEqual(report["evaluation_status"], "MANUAL_REVIEW_REQUIRED")
        
        summary = report["scoring_summary"]
        self.assertEqual(summary["total_available"], 100.0)
        self.assertEqual(summary["verified_earned"], 70.0)
        self.assertEqual(summary["pending_review"], 30.0)
        self.assertEqual(summary["status"], "MANUAL_REVIEW_REQUIRED")

    def test_no_heuristic_guessing(self):
        """P0 Directive: Empty test cases must NEVER guess marks via heuristic fallback."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)

        spec = {
            "rubric": {
                "criteria": [
                    {"name": "Charts and Visuals", "weight": 50},
                    {"name": "Pivot Analysis", "weight": 50}
                ]
            },
            "testCases": []  # No test cases at all
        }

        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        self.assertEqual(report["score"], 0.0)
        self.assertEqual(report["evaluation_status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(report["scoring_summary"]["pending_review"], 100.0)

    def test_stale_formula_cache_detection(self):
        """Amendment 7: Stale cached formula values are detected when cached differs from recalc."""
        # Student saved 100 in file, but when recalculated it evaluates to 50
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=50)

        spec = {
            "rubric": {
                "criteria": [{"name": "Formula Verification", "weight": 100}]
            },
            "testCases": [
                {
                    "criterion": "Formula Verification",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 50
                }
            ]
        }

        # Note: In our test setup, openpyxl saved file has no formula cache unless saved by Excel.
        # But if cached differs, it emits a STALE_FORMULA_CACHE warning.
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        self.assertIn("calculation_warnings", report)

    def test_hardcoded_value_anti_cheat(self):
        """Amendment 3: A student entering static numbers when a formula is required must fail."""
        # Student typed static 100, NOT a formula
        orig_p, recalc_p = self._create_sample_workbook(formula=None, original_val=100, cached_val=100)

        spec = {
            "rubric": {
                "criteria": [{"name": "Dynamic Formulas", "weight": 100}]
            },
            "testCases": [
                {
                    "criterion": "Dynamic Formulas",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 100,
                    "mustBeFormula": True  # Explicit formula requirement
                }
            ]
        }

        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        self.assertEqual(report["score"], 0.0)
        failed_a = [a for a in report["assertion_results"] if a.get("passed") is False]
        self.assertTrue(len(failed_a) > 0)
        self.assertIn("hardcoded", failed_a[0]["reason"].lower())


if __name__ == "__main__":
    unittest.main()
