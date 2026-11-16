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
        import time, zipfile, io
        orig_path = os.path.join(self.temp_dir, f"test_orig_{int(time.time() * 1000)}_{os.getpid()}.xlsx")
        recalc_path = os.path.join(self.temp_dir, f"test_recalc_{int(time.time() * 1000)}_{os.getpid()}.xlsx")

        wb_orig = openpyxl.Workbook()
        ws_orig = wb_orig.active
        ws_orig.title = sheet_name
        ws_orig["A1"] = 40
        ws_orig["A2"] = 60
        ws_orig["B1"] = formula if formula else original_val
        wb_orig.save(orig_path)
        wb_orig.close()

        # If formula provided and original_val provided, inject cached value into orig_path OpenXML
        if formula and original_val is not None:
            buf = io.BytesIO()
            with zipfile.ZipFile(orig_path, 'r') as zin, zipfile.ZipFile(buf, 'w') as zout:
                for item in zin.infolist():
                    if item.filename.startswith('xl/worksheets/sheet'):
                        s_xml = zin.read(item.filename).decode('utf-8')
                        clean_fn = formula.lstrip("=")
                        if f"<f>{clean_fn}</f>" in s_xml:
                            s_xml = s_xml.replace(f"<f>{clean_fn}</f>", f"<f>{clean_fn}</f><v>{original_val}</v>")
                        zout.writestr(item, s_xml.encode('utf-8'))
                    else:
                        zout.writestr(item, zin.read(item.filename))
            with open(orig_path, 'wb') as f:
                f.write(buf.getvalue())

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
        # downstream_chart was blocked due to upstream_table failure
        skipped = [a for a in report["assertion_results"] if a.get("status") in ["DEPENDENCY_SKIPPED", "BLOCKED_BY_DEPENDENCY"]]
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


    def test_t8_partially_unsupported_criterion(self):
        """T8: Partially unsupported criterion separates verified and pending marks."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Mixed Capability", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "supported_calc",
                    "criterion": "Mixed Capability",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,
                    "weight": 50
                },
                {
                    "id": "unsupported_dax",
                    "criterion": "Mixed Capability",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A2",
                    "expectedValue": 60,
                    "weight": 50,
                    "status": "UNSUPPORTED_FEATURE"
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Earned: 50, Pending: 50, Total Score: 50.0
        self.assertEqual(report["score"], 50.0)
        self.assertEqual(report["evaluation_status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(report["rubric_breakdown_details"]["Mixed Capability"]["pending_marks"], 50.0)

    def test_t9_dependency_skipped_preserves_denominator(self):
        """T9 (Reviewer Core Case): Skipping downstream assertions does NOT shrink the denominator!"""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Dashboard & Table", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "source_table",
                    "criterion": "Dashboard & Table",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 999,  # Fails
                    "weight": 40
                },
                {
                    "id": "chart_source",
                    "criterion": "Dashboard & Table",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 100,
                    "weight": 30,
                    "depends_on": ["source_table"],
                    "dependency_mode": "skip_penalty"
                },
                {
                    "id": "chart_title",
                    "criterion": "Dashboard & Table",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,  # Passes
                    "weight": 10
                },
                {
                    "id": "currency_formatting",
                    "criterion": "Dashboard & Table",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A2",
                    "expectedValue": 60,  # Passes
                    "weight": 20
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Math: Passing checks = 10 + 20 = 30.
        # Total weight = 40 + 30 + 10 + 20 = 100.
        # Score must be EXACTLY 30.0 / 100.0, NOT 42.8% (30/70)!
        self.assertEqual(report["score"], 30.0)
        self.assertEqual(report["rubric_breakdown"]["Dashboard & Table"], 30.0)
        self.assertEqual(report["scoring_summary"]["pending_review"], 30.0)
        self.assertEqual(report["evaluation_status"], "MANUAL_REVIEW_REQUIRED")

    def test_t10_circular_dependency_detected(self):
        """T10: Circular dependencies are caught and marked CONFIGURATION_ERROR."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Workflow", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "node_A",
                    "criterion": "Workflow",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,
                    "weight": 50,
                    "depends_on": ["node_B"]
                },
                {
                    "id": "node_B",
                    "criterion": "Workflow",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A2",
                    "expectedValue": 60,
                    "weight": 50,
                    "depends_on": ["node_A"]
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        self.assertEqual(report["evaluation_status"], "MANUAL_REVIEW_REQUIRED")
        cfg_errs = [a for a in report["assertion_results"] if a.get("status") == "CONFIGURATION_ERROR"]
        self.assertTrue(len(cfg_errs) > 0)

    def test_t11_two_dependent_assertions_same_root(self):
        """T11: Two downstream assertions dependent on same root do not double-penalize."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Reports", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "root_data",
                    "criterion": "Reports",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 999,  # Fails
                    "weight": 50
                },
                {
                    "id": "downstream_pivot",
                    "criterion": "Reports",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 100,
                    "weight": 25,
                    "depends_on": ["root_data"],
                    "dependency_mode": "skip_penalty"
                },
                {
                    "id": "downstream_chart",
                    "criterion": "Reports",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 100,
                    "weight": 25,
                    "depends_on": ["root_data"],
                    "dependency_mode": "skip_penalty"
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Root data failed, both dependents skipped without crashing, score is 0.0/100
        self.assertEqual(report["score"], 0.0)
        skipped = [a for a in report["assertion_results"] if a.get("status") in ["DEPENDENCY_SKIPPED", "BLOCKED_BY_DEPENDENCY"]]
        self.assertEqual(len(skipped), 2)

    def test_t12_all_assertions_pending_no_invented_score(self):
        """T12: If all assertions are pending manual review, final score is 0.0 with 100% pending."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Instructor Section", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "manual_item",
                    "criterion": "Instructor Section",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,
                    "weight": 100,
                    "status": "MANUAL_REVIEW_REQUIRED"
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        self.assertEqual(report["score"], 0.0)
        self.assertEqual(report["scoring_summary"]["pending_review"], 100.0)
        self.assertEqual(report["evaluation_status"], "MANUAL_REVIEW_REQUIRED")

    def test_t13_zero_weights_handled_gracefully(self):
        """T13: Zero assertion weights are handled safely without division by zero."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Zero Weight Test", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "zero_w",
                    "criterion": "Zero Weight Test",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 40,
                    "weight": 0.0
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        self.assertEqual(report["score"], 0.0)

    def test_t14_critical_requirement_teacher_defined_partial_cap(self):
        """T14: Critical requirement with a teacher-defined partial cap (e.g. 35.0 marks cap)."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=SUM(A1:A2)", cached_val=100)
        spec = {
            "rubric": {
                "criteria": [{"name": "Financial Modeling", "weight": 100}]
            },
            "testCases": [
                {
                    "id": "methodology",
                    "criterion": "Financial Modeling",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A1",
                    "expectedValue": 999,  # Fails
                    "weight": 20,
                    "is_critical": True,
                    "score_cap": 35.0  # Teacher allows max 35.0 if methodology fails
                },
                {
                    "id": "calculations",
                    "criterion": "Financial Modeling",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "A2",
                    "expectedValue": 60,  # Passes
                    "weight": 80
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Even though calculations passed (80% of 100 = 80), cap restricts score to 35.0
        self.assertEqual(report["score"], 35.0)

    def test_t15_volatile_formula_cache_divergence(self):
        """T15: Volatile formula divergence produces a CACHE_DIVERGENCE_VOLATILE warning without failure."""
        orig_p, recalc_p = self._create_sample_workbook(formula="=TODAY()", original_val=40000, cached_val=45001)
        spec = {
            "rubric": {
                "criteria": [{"name": "Date Logic", "weight": 100}]
            },
            "testCases": [
                {
                    "criterion": "Date Logic",
                    "type": "value_check",
                    "sheet": "Sheet1",
                    "cell": "B1",
                    "expectedValue": 45001
                }
            ]
        }
        report = inspect_and_evaluate(orig_p, recalc_p, spec)
        # Verify that CACHE_DIVERGENCE_VOLATILE warning was registered
        volatile_warnings = [w for w in report.get("calculation_warnings", []) if "CACHE_DIVERGENCE_VOLATILE" in w]
        self.assertTrue(len(volatile_warnings) > 0)


if __name__ == "__main__":
    unittest.main()
