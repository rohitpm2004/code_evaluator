"""
Sprint 5 Automated Test Suite: Specialized Execution Runtimes

Tests LibreOfficeWorker ephemeral isolation & audit hashes,
DuckDBAnalyticalAdapter SQL aggregations, multi-table foreign-key relational integrity,
structural DAX measure limits, PowerQueryWorker host contract verification, and
Unified Execution Result schema compliance.
"""

import unittest
import os
import sys
import tempfile
import zipfile
import shutil
import openpyxl
from openpyxl.worksheet.table import Table

# Add engine to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../engine")))
from specialized_runtimes import (
    LibreOfficeWorker,
    DuckDBAnalyticalAdapter,
    PowerQueryWorker,
    build_unified_result
)


class TestSprint5SpecializedExecutionRuntimes(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="sprint5_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_relational_workbook(self):
        """Creates a multi-table workbook with Customers and Orders for relational testing."""
        wb_path = os.path.join(self.tmp_dir, "relational_store.xlsx")
        wb = openpyxl.Workbook()

        # Customers Sheet
        ws_cust = wb.active
        ws_cust.title = "Customers"
        ws_cust["A1"] = "CustomerID"
        ws_cust["B1"] = "CustomerName"
        ws_cust["A2"] = "C101"
        ws_cust["B2"] = "Acme Corp"
        ws_cust["A3"] = "C102"
        ws_cust["B3"] = "Beta LLC"

        # Orders Sheet
        ws_orders = wb.create_sheet("Orders")
        ws_orders["A1"] = "OrderID"
        ws_orders["B1"] = "CustomerID"
        ws_orders["C1"] = "Amount"
        ws_orders["A2"] = "O001"
        ws_orders["B2"] = "C101"
        ws_orders["C2"] = 1500.0
        ws_orders["A3"] = "O002"
        ws_orders["B3"] = "C102"
        ws_orders["C3"] = 2500.0
        ws_orders["A4"] = "O003"
        ws_orders["B3"] = "C101"
        ws_orders["C4"] = 1000.0

        wb.save(wb_path)
        wb.close()
        return wb_path

    # ── Test 1: Unified Execution Result Schema Compliance ──
    def test_unified_execution_result_schema_compliance(self):
        """Asserts build_unified_result contains all required contract keys and formatted types."""
        res = build_unified_result(
            assertion_id="test_001",
            criterion="Revenue Aggregation",
            execution_engine="duckdb_analytical_adapter",
            engine_version="1.0.0",
            status="VERIFIED_PASS",
            verification_mode="analytical_sql",
            evidence={"sum": 5000.0},
            limitations=["TEST_LIMITATION: Sample note."],
            execution_time_ms=12.345
        )

        required_keys = [
            "assertion_id", "criterion", "execution_engine", "engine_version",
            "status", "verification_mode", "evidence", "limitations", "execution_time_ms"
        ]
        for k in required_keys:
            self.assertIn(k, res)

        self.assertEqual(res["assertion_id"], "test_001")
        self.assertEqual(res["status"], "VERIFIED_PASS")
        self.assertEqual(res["execution_time_ms"], 12.35)
        self.assertEqual(len(res["limitations"]), 1)

    # ── Test 2: LibreOffice Worker Isolated Recalculation & Audit Hashes ──
    def test_libreoffice_worker_isolated_recalculation(self):
        """Asserts LibreOfficeWorker executes in isolated profile and emits audit hashes."""
        wb_path = os.path.join(self.tmp_dir, "lo_test.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = 10
        ws["A2"] = 20
        ws["A3"] = "=SUM(A1:A2)"
        wb.save(wb_path)
        wb.close()

        worker = LibreOfficeWorker(timeout_sec=10)
        res = worker.recalculate(wb_path)

        # Result conforms to contract
        self.assertIn("assertion_id", res)
        self.assertIn("execution_engine", res)
        self.assertIn("original_sha256", res["evidence"])
        self.assertIn("recalculated_sha256", res["evidence"])
        self.assertEqual(len(res["evidence"]["original_sha256"]), 64)
        self.assertTrue(res["execution_time_ms"] >= 0.0)

    # ── Test 3: DuckDB Analytical Adapter SQL Aggregation ──
    def test_duckdb_adapter_tabular_aggregation_sql(self):
        """Independently calculates column sum via SQL and verifies against expected total."""
        wb_path = self._create_relational_workbook()
        adapter = DuckDBAnalyticalAdapter(wb_path)

        # Orders Amount: 1500 + 2500 + 1000 = 5000.0
        res = adapter.verify_tabular_sum(
            sheet_or_table="Orders",
            column_name="Amount",
            expected_sum=5000.0,
            abs_tolerance=0.01
        )

        self.assertEqual(res["status"], "VERIFIED_PASS")
        self.assertEqual(res["evidence"]["actual_calculated_sum"], 5000.0)
        self.assertEqual(res["verification_mode"], "analytical_sql")
        adapter.close()

    # ── Test 4: DuckDB Relational Foreign Key Integrity ──
    def test_duckdb_adapter_relational_foreign_key_integrity(self):
        """Verifies multi-table foreign-key consistency and flags orphan foreign keys."""
        wb_path = self._create_relational_workbook()
        adapter = DuckDBAnalyticalAdapter(wb_path)

        # Orders CustomerID -> Customers CustomerID (valid)
        res_valid = adapter.verify_foreign_key_integrity(
            child_table="Orders",
            child_col="CustomerID",
            parent_table="Customers",
            parent_col="CustomerID"
        )
        self.assertEqual(res_valid["status"], "VERIFIED_PASS")
        self.assertEqual(res_valid["evidence"]["orphan_record_count"], 0)

        # Corrupt an order with orphan customer ID
        wb = openpyxl.load_workbook(wb_path)
        ws_ord = wb["Orders"]
        ws_ord["B2"] = "C999_ORPHAN"
        wb.save(wb_path)
        wb.close()
        adapter.close()

        # Re-verify with orphan
        adapter_corrupt = DuckDBAnalyticalAdapter(wb_path)
        res_corrupt = adapter_corrupt.verify_foreign_key_integrity(
            child_table="Orders",
            child_col="CustomerID",
            parent_table="Customers",
            parent_col="CustomerID"
        )
        self.assertEqual(res_corrupt["status"], "VERIFIED_FAIL")
        self.assertEqual(res_corrupt["evidence"]["orphan_record_count"], 1)
        adapter_corrupt.close()

    # ── Test 5: DuckDB DAX Measure Limitation Contract ──
    def test_duckdb_adapter_dax_measure_limitation_contract(self):
        """Simple DAX passes structural check; complex filter context tags MANUAL_REVIEW_REQUIRED with explicit limitation."""
        wb_path = self._create_relational_workbook()
        adapter = DuckDBAnalyticalAdapter(wb_path)

        # Simple aggregation measure
        res_simple = adapter.inspect_dax_measure_structural(
            measure_name="TotalSales",
            dax_expression="SUM(Orders[Amount])"
        )
        self.assertEqual(res_simple["status"], "VERIFIED_PASS")
        self.assertEqual(res_simple["verification_mode"], "structural_only")

        # Complex filter context measure (CALCULATE / ALL)
        res_complex = adapter.inspect_dax_measure_structural(
            measure_name="TotalAllSales",
            dax_expression="CALCULATE(SUM(Orders[Amount]), ALL(Customers))"
        )
        self.assertEqual(res_complex["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertTrue(len(res_complex["limitations"]) > 0)
        self.assertIn("DAX_FILTER_CONTEXT_UNVERIFIED", res_complex["limitations"][0])
        adapter.close()

    # ── Test 6: Host-Bound Power Query Contract Worker ──
    def test_power_query_worker_pipeline_contract(self):
        """Validates M-code steps, destination table columns, and tags freshness limitation."""
        wb_path = os.path.join(self.tmp_dir, "pq_pipeline.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "CleanSales"
        ws["A1"] = "OrderID"
        ws["B1"] = "CleanAmount"
        ws["A2"] = 101
        ws["B2"] = 500
        tab = Table(displayName="CleanSales", ref="A1:B2")
        ws.add_table(tab)
        wb.save(wb_path)
        wb.close()

        # Inject M-code into customXml
        with zipfile.ZipFile(wb_path, 'r') as z_in:
            file_data = {item.filename: z_in.read(item.filename) for item in z_in.infolist()}

        m_xml = (
            '<item xmlns="http://schemas.microsoft.com/DataMashup">'
            'section Section1; shared CleanSales = let Source = Excel.Workbook(...), #"Filtered Rows" = Table.SelectRows(Source) in #"Filtered Rows";'
            '</item>'
        )
        file_data["customXml/item1.xml"] = m_xml.encode("utf-8")

        with zipfile.ZipFile(wb_path, 'w', compression=zipfile.ZIP_DEFLATED) as z_out:
            for fname, content in file_data.items():
                z_out.writestr(fname, content)

        pq_worker = PowerQueryWorker()
        res = pq_worker.verify_pipeline(
            workbook_path=wb_path,
            query_name="CleanSales",
            required_columns=["OrderID", "CleanAmount"],
            min_row_count=1
        )

        self.assertEqual(res["status"], "VERIFIED_PASS")
        self.assertEqual(res["evidence"]["query_name"], "CleanSales")
        self.assertTrue(res["evidence"]["m_code_present"])
        self.assertTrue(len(res["evidence"]["m_steps_detected"]) >= 1)
        self.assertTrue(res["evidence"]["required_columns_satisfied"])

        # Explicit Freshness Limitation Contract
        self.assertTrue(len(res["limitations"]) > 0)
        self.assertIn("POWER_QUERY_FRESHNESS_UNVERIFIED", res["limitations"][0])


if __name__ == "__main__":
    unittest.main()

