"""
Sprint 3 Automated Test Suite: Deep Workbook Verification
Tests chart source ranges, rendered data values, PivotTable Grand Totals,
PivotCache staleness detection, Power Query output contracts & freshness tagging,
and behavioral scenario mutations.
"""

import unittest
import os
import sys
import tempfile
import zipfile
import shutil
import xml.etree.ElementTree as ET
import openpyxl
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.chart import BarChart, Reference

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../engine")))
from excel_runner import inspect_and_evaluate
from workbook_registry import WorkbookObjectRegistry


class TestSprint3DeepWorkbookVerification(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="sprint3_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_chart_workbook(self, chart_title="Quarterly Sales", range_str="Sheet1!$B$2:$B$4", vals=[100, 200, 300]):
        """Helper to create a valid workbook with an OpenXML DrawingML chart referencing range_str."""
        wb_path = os.path.join(self.tmp_dir, "chart_test.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws["A1"] = "Category"
        ws["B1"] = "Revenue"
        ws["A2"] = "Q1"
        ws["B2"] = vals[0]
        ws["A3"] = "Q2"
        ws["B3"] = vals[1]
        ws["A4"] = "Q3"
        ws["B4"] = vals[2]

        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.title = chart_title
        data = Reference(ws, min_col=2, min_row=1, max_row=4)
        cats = Reference(ws, min_col=1, min_row=2, max_row=4)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws.add_chart(chart, "D2")

        wb.save(wb_path)
        wb.close()
        return wb_path

    def _create_pivot_workbook(self, grand_total=150000.0, cache_records=100, table_rows=100):
        """Helper to create a workbook with a native Pivot Table and rendered Grand Total row."""
        wb_path = os.path.join(self.tmp_dir, "pivot_test.xlsx")
        wb = openpyxl.Workbook()
        ws_data = wb.active
        ws_data.title = "SalesData"
        ws_data["A1"] = "Region"
        ws_data["B1"] = "Sales"
        for r in range(2, table_rows + 2):
            ws_data[f"A{r}"] = "North" if r % 2 == 0 else "South"
            ws_data[f"B{r}"] = 1000

        tab = Table(displayName="SalesTable", ref=f"A1:B{table_rows + 1}")
        ws_data.add_table(tab)

        ws_pivot = wb.create_sheet("PivotReport")
        ws_pivot["A3"] = "Row Labels"
        ws_pivot["B3"] = "Sum of Sales"
        ws_pivot["A4"] = "North"
        ws_pivot["B4"] = 75000
        ws_pivot["A5"] = "South"
        ws_pivot["B5"] = 75000
        ws_pivot["A6"] = "Grand Total"
        ws_pivot["B6"] = grand_total

        wb.save(wb_path)
        wb.close()

        # Inject native OpenXML PivotTable and PivotCache into package
        with zipfile.ZipFile(wb_path, 'r') as z_in:
            file_data = {item.filename: z_in.read(item.filename) for item in z_in.infolist()}

        pt_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<pivotTableDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'name="SalesPivot" cacheId="1" dataCaption="Data">'
            '<location ref="A3:B6" firstHeaderRow="1" firstDataRow="1" firstDataCol="1"/>'
            '<pivotFields count="2">'
            '<pivotField name="Region" axis="axisRow" showAll="0"/>'
            '<pivotField name="Sales" dataField="1" showAll="0"/>'
            '</pivotFields>'
            '<rowFields count="1"><field x="0"/></rowFields>'
            '<dataFields count="1"><dataField name="Sum of Sales" fld="1" baseField="0" baseItem="0"/></dataFields>'
            '</pivotTableDefinition>'
        )

        pc_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<pivotCacheDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" recordCount="{cache_records}">'
            '<cacheSource type="worksheet">'
            '<worksheetSource name="SalesTable" sheet="SalesData"/>'
            '</cacheSource>'
            '<cacheFields count="2">'
            '<cacheField name="Region"/>'
            '<cacheField name="Sales"/>'
            '</cacheFields>'
            '</pivotCacheDefinition>'
        )

        sheet2_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/pivotTable" Target="../pivotTables/pivotTable1.xml"/>'
            '</Relationships>'
        )

        file_data["xl/pivotTables/pivotTable1.xml"] = pt_xml.encode("utf-8")
        file_data["xl/pivotCache/pivotCacheDefinition1.xml"] = pc_xml.encode("utf-8")
        file_data["xl/worksheets/_rels/sheet2.xml.rels"] = sheet2_rels.encode("utf-8")

        with zipfile.ZipFile(wb_path, 'w', compression=zipfile.ZIP_DEFLATED) as z_out:
            for fname, content in file_data.items():
                z_out.writestr(fname, content)

        return wb_path

    # ── Test 1: Chart Source Range Verification (Pass) ──
    def test_chart_source_range_verification_pass(self):
        """Chart series source range matches expectedSourceRange (case/dollar insensitive)."""
        wb_p = self._create_chart_workbook(range_str="Sheet1!$B$2:$B$4")
        spec = {
            "rubric": [{"name": "Chart Verification", "weight": 100}],
            "testCases": [{
                "criterion": "Chart Verification",
                "type": "chart_check",
                "chartType": "column",
                "expectedSourceRange": "B2:B4",
                "expectedValues": [100, 200, 300]
            }]
        }
        report = inspect_and_evaluate(wb_p, wb_p, spec)
        self.assertEqual(report["score"], 100.0)
        self.assertEqual(report["evaluation_status"], "COMPLETED")
        a = report["assertion_results"][0]
        self.assertTrue(a["passed"])
        self.assertIn("Verified", a["reason"])

    # ── Test 2: Chart Source Range Mismatch (Fail) ──
    def test_chart_source_range_mismatch_fail(self):
        """Chart series points to Column B, but rubric requires Column C."""
        wb_p = self._create_chart_workbook(range_str="Sheet1!$B$2:$B$4")
        spec = {
            "rubric": [{"name": "Chart Verification", "weight": 100}],
            "testCases": [{
                "criterion": "Chart Verification",
                "type": "chart_check",
                "chartType": "column",
                "expectedSourceRange": "C2:C4"
            }]
        }
        report = inspect_and_evaluate(wb_p, wb_p, spec)
        self.assertEqual(report["score"], 0.0)
        a = report["assertion_results"][0]
        self.assertFalse(a["passed"])
        self.assertIn("range mismatch", a["reason"].lower())

    # ── Test 3: Chart Data Values Verification ──
    def test_chart_data_values_mismatch_fail(self):
        """Chart range is correct, but rendered numbers in sheet do not match expectedValues."""
        wb_p = self._create_chart_workbook(range_str="Sheet1!$B$2:$B$4", vals=[100, 200, 300])
        spec = {
            "rubric": [{"name": "Chart Verification", "weight": 100}],
            "testCases": [{
                "criterion": "Chart Verification",
                "type": "chart_check",
                "expectedValues": [500, 600, 700]
            }]
        }
        report = inspect_and_evaluate(wb_p, wb_p, spec)
        self.assertEqual(report["score"], 0.0)
        a = report["assertion_results"][0]
        self.assertFalse(a["passed"])
        self.assertIn("values mismatch", a["reason"].lower())

    # ── Test 4: PivotTable Rendered Grand Total Verification (Pass) ──
    def test_pivot_grand_total_rendered_verification_pass(self):
        """Pivot Table rendered output has Grand Total = 150000 matching expectation."""
        wb_p = self._create_pivot_workbook(grand_total=150000.0, cache_records=100, table_rows=100)
        spec = {
            "rubric": [{"name": "Pivot Verification", "weight": 100}],
            "testCases": [{
                "criterion": "Pivot Verification",
                "type": "pivot_check",
                "requiredField": "Sales",
                "expectedGrandTotal": 150000.0
            }]
        }
        report = inspect_and_evaluate(wb_p, wb_p, spec)
        self.assertEqual(report["score"], 100.0)
        a = report["assertion_results"][0]
        self.assertTrue(a["passed"])
        self.assertIn("Grand Total: 150000", a["reason"])

    # ── Test 5: PivotTable Grand Total Mismatch (Fail) ──
    def test_pivot_grand_total_mismatch_fail(self):
        """Pivot Table Grand Total is 150000, but rubric expects 200000."""
        wb_p = self._create_pivot_workbook(grand_total=150000.0, cache_records=100, table_rows=100)
        spec = {
            "rubric": [{"name": "Pivot Verification", "weight": 100}],
            "testCases": [{
                "criterion": "Pivot Verification",
                "type": "pivot_check",
                "requiredField": "Sales",
                "expectedGrandTotal": 200000.0
            }]
        }
        report = inspect_and_evaluate(wb_p, wb_p, spec)
        self.assertEqual(report["score"], 0.0)
        a = report["assertion_results"][0]
        self.assertFalse(a["passed"])
        self.assertIn("Grand Total mismatch", a["reason"])

    # ── Test 6: Stale PivotCache Detection ──
    def test_pivot_cache_stale_detection(self):
        """Source table has 100 rows, but PivotCache only cached 50 records (student forgot to refresh)."""
        wb_p = self._create_pivot_workbook(grand_total=150000.0, cache_records=50, table_rows=100)
        spec = {
            "rubric": [{"name": "Pivot Verification", "weight": 100}],
            "testCases": [{
                "criterion": "Pivot Verification",
                "type": "pivot_check",
                "requiredField": "Sales"
            }]
        }
        report = inspect_and_evaluate(wb_p, wb_p, spec)
        # Warning must be emitted in calculation_warnings
        warnings = report.get("calculation_warnings", [])
        stale_warns = [w for w in warnings if "PIVOT_CACHE_STALE" in w]
        self.assertTrue(len(stale_warns) > 0)
        self.assertIn("50 records", stale_warns[0])
        self.assertIn("100 rows", stale_warns[0])

    # ── Test 7: Power Query Output Headers & Freshness Contract Tagging ──
    def test_power_query_output_headers_and_freshness_tag(self):
        """Power Query destination table verified with required headers and freshness contract tagged."""
        wb_path = os.path.join(self.tmp_dir, "pq_test.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "LoadedQuery"
        ws["A1"] = "TransactionID"
        ws["B1"] = "CleanCustomer"
        ws["C1"] = "Revenue"
        ws["A2"] = 101
        ws["B2"] = "Acme Corp"
        ws["C2"] = 5000
        tab = Table(displayName="CleanOrders", ref="A1:C2")
        ws.add_table(tab)
        wb.save(wb_path)
        wb.close()

        # Inject M-code customXml into package
        with zipfile.ZipFile(wb_path, 'r') as z_in:
            file_data = {item.filename: z_in.read(item.filename) for item in z_in.infolist()}

        m_xml = (
            '<item xmlns="http://schemas.microsoft.com/DataMashup">'
            'section Section1; shared CleanOrders = let Source = Excel.Workbook(...) in Source;'
            '</item>'
        )
        file_data["customXml/item1.xml"] = m_xml.encode("utf-8")

        with zipfile.ZipFile(wb_path, 'w', compression=zipfile.ZIP_DEFLATED) as z_out:
            for fname, content in file_data.items():
                z_out.writestr(fname, content)

        spec = {
            "rubric": [{"name": "Power Query ETL", "weight": 100}],
            "testCases": [{
                "criterion": "Power Query ETL",
                "type": "power_query_check",
                "requiredColumns": ["TransactionID", "CleanCustomer", "Revenue"],
                "minRowCount": 1
            }]
        }
        report = inspect_and_evaluate(wb_path, wb_path, spec)
        self.assertEqual(report["score"], 100.0)
        a = report["assertion_results"][0]
        self.assertTrue(a["passed"])
        self.assertEqual(a.get("power_query_status"), "POWER_QUERY_M_CODE_VERIFIED")
        self.assertEqual(a.get("power_query_output"), "POWER_QUERY_OUTPUT_VERIFIED")
        self.assertEqual(a.get("freshness_status"), "POWER_QUERY_FRESHNESS_UNVERIFIED")

    # ── Test 8: Power Query Header Mismatch Fail ──
    def test_power_query_header_mismatch_fail(self):
        """Power Query destination table missing a required column."""
        wb_path = os.path.join(self.tmp_dir, "pq_fail.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "LoadedQuery"
        ws["A1"] = "TransactionID"
        ws["B1"] = "RawCustomer"  # Missing CleanCustomer
        ws["A2"] = 101
        ws["B2"] = "Acme"
        tab = Table(displayName="OrdersTable", ref="A1:B2")
        ws.add_table(tab)
        wb.save(wb_path)
        wb.close()

        spec = {
            "rubric": [{"name": "Power Query ETL", "weight": 100}],
            "testCases": [{
                "criterion": "Power Query ETL",
                "type": "power_query_check",
                "requireQuery": False,
                "requiredColumns": ["TransactionID", "CleanCustomer"]
            }]
        }
        report = inspect_and_evaluate(wb_path, wb_path, spec)
        self.assertEqual(report["score"], 0.0)
        a = report["assertion_results"][0]
        self.assertFalse(a["passed"])
        self.assertEqual(a.get("power_query_output"), "POWER_QUERY_OUTPUT_HEADER_MISMATCH")

    # ── Test 9: Controlled Formula-Behavior Check (Scenario Mutation) ──
    def test_formula_behavior_scenario_mutation(self):
        """Mutating input cell A1 on a workbook copy recomputes formula in C1 (=A1*B1)."""
        wb_path = os.path.join(self.tmp_dir, "mutation_test.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = 10
        ws["B1"] = 5
        ws["C1"] = "=A1*B1"  # 10 * 5 = 50 originally
        wb.save(wb_path)
        wb.close()

        spec = {
            "rubric": [{"name": "Dynamic Formulas", "weight": 100}],
            "testCases": [{
                "criterion": "Dynamic Formulas",
                "type": "formula_behavior_check",
                "scenarios": [{
                    "name": "Price Increase Scenario",
                    "inputs": [{"cell": "A1", "value": 40}],  # 40 * 5 = 200
                    "expectedOutputs": [{"cell": "C1", "expectedValue": 200}]
                }]
            }]
        }
        report = inspect_and_evaluate(wb_path, wb_path, spec)
        self.assertEqual(report["score"], 100.0)
        a = report["assertion_results"][0]
        self.assertTrue(a["passed"])
        self.assertIn("mutation(s) passed successfully", a["reason"])


if __name__ == "__main__":
    unittest.main()
