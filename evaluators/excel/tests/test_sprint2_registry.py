import os
import sys
import tempfile
import unittest
import hashlib
import zipfile
import io
import openpyxl

# Add central-evaluator engine to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../engine")))
from workbook_registry import WorkbookObjectRegistry
from excel_runner import inspect_and_evaluate


class TestSprint2WorkbookObjectRegistry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_rich_workbook(self):
        """Creates a multi-sheet workbook with defined names, tables, and calculation properties."""
        wb_path = os.path.join(self.temp_dir, "rich_test.xlsx")
        wb = openpyxl.Workbook()

        # Sheet 1: Main Data
        ws1 = wb.active
        ws1.title = "SalesData"
        ws1["A1"] = "ProductID"
        ws1["B1"] = "Category"
        ws1["C1"] = "Revenue"
        ws1["A2"] = 101
        ws1["B2"] = "Electronics"
        ws1["C2"] = 1500
        ws1["A3"] = 102
        ws1["B3"] = "Furniture"
        ws1["C3"] = 800

        # Create an Excel Table
        from openpyxl.worksheet.table import Table, TableStyleInfo
        tab = Table(displayName="SalesTable", ref="A1:C3")
        style = TableStyleInfo(name="TableStyleMedium9", showFirstColumn=False,
                               showLastColumn=False, showRowStripes=True, showColumnStripes=True)
        tab.tableStyleInfo = style
        ws1.add_table(tab)

        # Sheet 2: Summary (Hidden)
        ws2 = wb.create_sheet(title="InternalSummary")
        ws2.sheet_state = "hidden"
        ws2["A1"] = "TotalRevenue"
        ws2["B1"] = "=SUM(SalesData!C2:C3)"

        # Defined Name
        from openpyxl.workbook.defined_name import DefinedName
        dn = DefinedName(name="TotalRevRange", attr_text="SalesData!C2:C3")
        wb.defined_names["TotalRevRange"] = dn

        wb.save(wb_path)
        wb.close()
        return wb_path

    def test_single_pass_metadata_and_worksheets(self):
        """Test that worksheets, visibility, and calculation properties are indexed in a single pass."""
        wb_p = self._create_rich_workbook()
        reg = WorkbookObjectRegistry(wb_p)

        self.assertIn("SalesData", reg.worksheets)
        self.assertIn("InternalSummary", reg.worksheets)
        self.assertEqual(reg.worksheets["SalesData"]["state"], "visible")
        self.assertEqual(reg.worksheets["InternalSummary"]["state"], "hidden")
        self.assertIn("TotalRevRange", reg.defined_names)
        reg.close()

    def test_excel_table_discovery_and_columns(self):
        """Test that Excel Tables, range references, and columns are cleanly indexed."""
        wb_p = self._create_rich_workbook()
        reg = WorkbookObjectRegistry(wb_p)

        self.assertEqual(len(reg.tables), 1)
        tbl = reg.tables[0]
        self.assertEqual(tbl["display_name"], "SalesTable")
        self.assertEqual(tbl["range_ref"], "A1:C3")
        self.assertIn("ProductID", tbl["columns"])
        self.assertIn("Revenue", tbl["columns"])

        # Test lookup by column name
        found_tbl = reg.get_table("Revenue")
        self.assertIsNotNone(found_tbl)
        self.assertEqual(found_tbl["display_name"], "SalesTable")
        reg.close()

    def test_lazy_loading_memory_safety(self):
        """Test that openpyxl workbooks are NOT loaded at initialization time (lazy loading)."""
        wb_p = self._create_rich_workbook()
        reg = WorkbookObjectRegistry(wb_p)

        # Confirm internal workbook references are None prior to access
        self.assertIsNone(reg._wb_orig)
        self.assertIsNone(reg._wb_cached)
        self.assertIsNone(reg._wb_recalc)

        # Access triple state -> lazily loads
        cell_info = reg.get_cell_triple_state("InternalSummary", "B1")
        self.assertIsNotNone(reg._wb_orig)
        self.assertTrue(cell_info["is_formula"])
        self.assertEqual(cell_info["formula_expr"], "=SUM(SalesData!C2:C3)")
        reg.close()

    def test_immutable_source_protection(self):
        """Test that original student file is strictly read-only and hash remains identical."""
        wb_p = self._create_rich_workbook()
        with open(wb_p, "rb") as f:
            before_hash = hashlib.sha256(f.read()).hexdigest()

        reg = WorkbookObjectRegistry(wb_p)
        _ = reg.worksheets
        _ = reg.tables
        _ = reg.get_cell_triple_state("SalesData", "C2")
        reg.close()

        with open(wb_p, "rb") as f:
            after_hash = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(before_hash, after_hash)

    def test_power_query_indexing_from_custom_xml(self):
        """Test that Power Query M-code in customXml/item*.xml is extracted into registry."""
        wb_p = self._create_rich_workbook()

        # Inject sample Power Query customXml
        m_code_sample = 'let Source = Excel.Workbook(File.Contents("data.xlsx")), #"Cleaned" = Table.TransformColumns(Source) in #"Cleaned"'
        custom_xml_content = f'''<m:mCode xmlns:m="http://schemas.microsoft.com/DataMashup">
            section Section1;
            shared #"CleanOrders" = {m_code_sample};
        </m:mCode>'''

        buf = io.BytesIO()
        with zipfile.ZipFile(wb_p, 'r') as zin, zipfile.ZipFile(buf, 'w') as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("customXml/item1.xml", custom_xml_content.encode("utf-8"))

        with open(wb_p, 'wb') as f:
            f.write(buf.getvalue())

        reg = WorkbookObjectRegistry(wb_p)
        self.assertEqual(len(reg.power_queries), 1)
        pq = reg.power_queries[0]
        self.assertEqual(pq["query_name"], "CleanOrders")
        self.assertTrue(pq["is_valid_syntax"])
        self.assertIn("File.Contents", pq["m_code"])
        reg.close()

    def test_chart_series_numref_indexing(self):
        """Test that charts and their underlying <c:numRef> data sources are indexed."""
        wb_p = self._create_rich_workbook()

        # Inject sample chart with numRef
        chart_xml_content = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
            <c:chart>
                <c:title><c:tx><c:rich><a:p><a:r><a:t>Quarterly Revenue</a:t></a:r></a:p></c:rich></c:tx></c:title>
                <c:plotArea>
                    <c:barChart>
                        <c:ser>
                            <c:val>
                                <c:numRef>
                                    <c:f>SalesData!$C$2:$C$3</c:f>
                                </c:numRef>
                            </c:val>
                        </c:ser>
                    </c:barChart>
                </c:plotArea>
            </c:chart>
        </c:chartSpace>'''

        buf = io.BytesIO()
        with zipfile.ZipFile(wb_p, 'r') as zin, zipfile.ZipFile(buf, 'w') as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("xl/charts/chart1.xml", chart_xml_content.encode("utf-8"))

        with open(wb_p, 'wb') as f:
            f.write(buf.getvalue())

        reg = WorkbookObjectRegistry(wb_p)
        self.assertEqual(len(reg.charts), 1)
        ch = reg.charts[0]
        self.assertEqual(ch["type"], "BarChart")
        self.assertEqual(ch["title"], "Quarterly Revenue")
        self.assertEqual(ch["series_count"], 1)
        self.assertIn("SalesData!$C$2:$C$3", ch["num_refs"])
        reg.close()

    def test_end_to_end_runner_registry_integration(self):
        """Test that inspect_and_evaluate() cleanly incorporates the registry into its report."""
        wb_p = self._create_rich_workbook()
        spec = {
            "rubric": {
                "criteria": [{"name": "Sales Check", "weight": 100}]
            },
            "testCases": [
                {
                    "criterion": "Sales Check",
                    "type": "value_check",
                    "sheet": "SalesData",
                    "cell": "C2",
                    "expectedValue": 1500
                }
            ]
        }

        report = inspect_and_evaluate(wb_p, wb_p, spec)
        self.assertEqual(report["score"], 100.0)
        self.assertIn("registry_summary", report)
        self.assertIsNotNone(report["registry_summary"])
        reg_sum = report["registry_summary"]
        self.assertEqual(reg_sum["worksheet_count"], 2)
        self.assertEqual(reg_sum["table_count"], 1)


if __name__ == "__main__":
    unittest.main()
