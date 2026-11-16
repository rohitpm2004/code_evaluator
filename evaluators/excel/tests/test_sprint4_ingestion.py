"""
Sprint 4 Automated Test Suite: Teacher Solution Ingestion & Canonical Rubric Extraction

Tests reference workbook feature extraction, alternative function expansion,
canonical manifest generation, deterministic weights, SHA-256 immutability hashing,
master solution self-validation (100/100), and synthetic negative fixture detection.
"""

import unittest
import os
import sys
import tempfile
import zipfile
import shutil
import openpyxl
from openpyxl.worksheet.table import Table
from openpyxl.chart import BarChart, Reference

# Add engine to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../engine")))
from solution_ingestion import TeacherSolutionIngestionEngine, ingest_teacher_solution
from manifest_validator import ManifestValidator, validate_and_certify_manifest
from excel_runner import inspect_and_evaluate


class TestSprint4TeacherSolutionIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="sprint4_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_master_reference_workbook(self):
        """Creates a comprehensive reference solution workbook with formulas, table, and chart."""
        wb_path = os.path.join(self.tmp_dir, "master_solution.xlsx")
        wb = openpyxl.Workbook()

        # Sheet 1: Sales Data
        ws1 = wb.active
        ws1.title = "SalesData"
        ws1["A1"] = "Rep"
        ws1["B1"] = "Sales"
        ws1["C1"] = "Rate"
        ws1["D1"] = "Commission"

        data = [
            ("Alice", 10000, 0.10),
            ("Bob", 20000, 0.15),
            ("Charlie", 15000, 0.12)
        ]
        for idx, (rep, sales, rate) in enumerate(data, start=2):
            ws1[f"A{idx}"] = rep
            ws1[f"B{idx}"] = sales
            ws1[f"C{idx}"] = rate
            ws1[f"D{idx}"] = f"=B{idx}*C{idx}"  # Formula Commission

        tab = Table(displayName="SalesTable", ref="A1:D4")
        ws1.add_table(tab)

        # Add Chart for Sales
        chart = BarChart()
        chart.type = "col"
        chart.title = "Rep Performance"
        data_ref = Reference(ws1, min_col=2, min_row=1, max_row=4)
        cats_ref = Reference(ws1, min_col=1, min_row=2, max_row=4)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        ws1.add_chart(chart, "F2")

        wb.save(wb_path)
        wb.close()
        return wb_path

    # ── Test 1: Feature Extraction ──
    def test_feature_extraction_from_reference_workbook(self):
        """Extracts formulas, tables, charts, and supported capabilities from reference solution."""
        ref_path = self._create_master_reference_workbook()
        engine = TeacherSolutionIngestionEngine()
        features = engine.extract_reference_features(ref_path)

        # Discovered tables
        self.assertEqual(len(features["tables"]), 1)
        self.assertEqual(features["tables"][0]["display_name"], "SalesTable")

        # Discovered formula column
        formula_cols = features["formula_columns"]
        self.assertTrue(len(formula_cols) >= 1)
        comm_col = [fc for fc in formula_cols if "Commission" in fc["header"]][0]
        self.assertEqual(comm_col["range"], "D2:D4")
        self.assertIn("CELL_ARITHMETIC", comm_col["functions_used"])

        # Discovered chart
        self.assertEqual(len(features["charts"]), 1)
        ch = features["charts"][0]
        self.assertEqual(ch["title"], "Rep Performance")
        self.assertEqual(ch["type"], "ColumnChart")

    # ── Test 2: Canonical Manifest Generation ──
    def test_canonical_manifest_generation(self):
        """Generates structured canonical testCases matching teacher rubric criteria."""
        ref_path = self._create_master_reference_workbook()
        rubric = [
            {"name": "Calculate Commission", "weight": 50, "description": "Calculate total commission using formula"},
            {"name": "Rep Performance Chart", "weight": 50, "description": "Create a Column Chart for sales"}
        ]
        manifest = ingest_teacher_solution(ref_path, rubric, assignment_id="excel_hw_1")

        self.assertEqual(manifest["assignment_id"], "excel_hw_1")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertIn("manifest_hash", manifest)
        self.assertTrue(len(manifest["manifest_hash"]) == 64)

        # Test cases generated
        tcs = manifest["testCases"]
        self.assertTrue(len(tcs) >= 2)

        # Formula check generated for Commission
        formula_tc = [tc for tc in tcs if tc.get("type") == "formula_check"][0]
        self.assertEqual(formula_tc["criterion"], "Calculate Commission")
        self.assertEqual(formula_tc["columnAnchor"], "Commission")
        self.assertTrue(formula_tc["mustBeFormula"])

        # Chart check generated for Chart criterion
        chart_tc = [tc for tc in tcs if tc.get("type") == "chart_check"][0]
        self.assertEqual(chart_tc["criterion"], "Rep Performance Chart")
        self.assertEqual(chart_tc["chartType"], "ColumnChart")
        self.assertEqual(chart_tc["title"], "Rep Performance")

    # ── Test 3: Auto Alternative Function Expansion ──
    def test_auto_alternative_function_expansion(self):
        """Demonstrates that XLOOKUP automatically expands to accepted alternatives (INDEX, MATCH, VLOOKUP)."""
        wb_path = os.path.join(self.tmp_dir, "xlookup_test.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "ID"
        ws["B1"] = "Price"
        ws["A2"] = 101
        ws["B2"] = "=XLOOKUP(A2, D:D, E:E)"
        wb.save(wb_path)
        wb.close()

        engine = TeacherSolutionIngestionEngine()
        features = engine.extract_reference_features(wb_path)
        f_col = features["formula_columns"][0]
        self.assertIn("XLOOKUP", f_col["functions_used"])

        # Auto-expanded accepted alternatives
        accepted = f_col["accepted_functions"]
        self.assertIn("XLOOKUP", accepted)
        self.assertIn("INDEX", accepted)
        self.assertIn("MATCH", accepted)
        self.assertIn("VLOOKUP", accepted)

    # ── Test 4: Manifest Hash & Immutability ──
    def test_manifest_hash_and_immutability(self):
        """Identical criteria generate identical hashes; any criteria change updates the hash."""
        ref_path = self._create_master_reference_workbook()
        rubric_a = [{"name": "Commission", "weight": 100}]
        rubric_b = [{"name": "Commission", "weight": 100}]
        rubric_c = [{"name": "Commission", "weight": 80}, {"name": "Bonus", "weight": 20}]

        manifest_a = ingest_teacher_solution(ref_path, rubric_a)
        manifest_b = ingest_teacher_solution(ref_path, rubric_b)
        manifest_c = ingest_teacher_solution(ref_path, rubric_c)

        # Deterministic identical hash
        self.assertEqual(manifest_a["manifest_hash"], manifest_b["manifest_hash"])

        # Different criteria produce different hash
        self.assertNotEqual(manifest_a["manifest_hash"], manifest_c["manifest_hash"])

    # ── Test 5: Self-Validation Master Solution (100/100) ──
    def test_manifest_self_validation_master_score_100(self):
        """Reference solution must score 100.0 against its own generated manifest."""
        ref_path = self._create_master_reference_workbook()
        rubric = [
            {"name": "Calculate Commission", "weight": 50},
            {"name": "Rep Performance Chart", "weight": 50}
        ]
        manifest = ingest_teacher_solution(ref_path, rubric)

        validator = ManifestValidator()
        master_res = validator.validate_master_solution(ref_path, manifest)
        self.assertTrue(master_res["passed"])
        self.assertEqual(master_res["score"], 100.0)
        self.assertEqual(len(master_res["issues"]), 0)

    # ── Test 6: Negative Fixture Hardcoded Detection ──
    def test_negative_fixture_hardcoded_detection(self):
        """Synthetic negative workbook with hardcoded numbers triggers deduction."""
        ref_path = self._create_master_reference_workbook()
        rubric = [{"name": "Calculate Commission", "weight": 100}]
        manifest = ingest_teacher_solution(ref_path, rubric)

        validator = ManifestValidator()
        neg_res = validator.validate_negative_fixtures(ref_path, manifest)
        self.assertTrue(neg_res["passed_tests"] >= 1)
        anti_cheat_test = [r for r in neg_res["results"] if r["test_name"] == "anti_cheat_hardcoded_constants"][0]
        self.assertTrue(anti_cheat_test["defect_detected"])
        self.assertLess(anti_cheat_test["score"], 100.0)

    # ── Test 7: Full Certification Pipeline ──
    def test_full_manifest_certification_pipeline(self):
        """Certifies manifest, promoting status from PROPOSED to APPROVED."""
        ref_path = self._create_master_reference_workbook()
        rubric = [
            {"name": "Calculate Commission", "weight": 50},
            {"name": "Rep Performance Chart", "weight": 50}
        ]
        manifest = ingest_teacher_solution(ref_path, rubric)
        self.assertEqual(manifest["status"], "PROPOSED")

        cert_report = validate_and_certify_manifest(ref_path, manifest)
        self.assertTrue(cert_report["is_valid"])
        self.assertEqual(cert_report["status"], "APPROVED")
        self.assertEqual(manifest["status"], "APPROVED")
        self.assertEqual(cert_report["master_score"], 100.0)
        self.assertTrue(cert_report["negative_tests_passed"] >= 2)


if __name__ == "__main__":
    unittest.main()
