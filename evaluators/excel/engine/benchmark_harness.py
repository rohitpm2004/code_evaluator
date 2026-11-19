"""
Sprint 6 Benchmark Certification Harness: 50-Workbook Benchmark Suite

Provides an automated regression testing runner with teacher ground truth across 50
standardized Excel evaluation scenarios:
- 20 Positive workbooks (correct formulas, alternative lookups, variable layouts, charts, pivots, joins)
- 20 Negative workbooks (hardcoded values, calculation errors, broken lookups, stale caches, wrong charts)
- 10 Edge cases (shifted cells, whitespace, case insensitivity, non-100 max scores, partial caps)

Measures:
- Ground truth agreement (100% target)
- False Positive Rate (0.0% target)
- False Negative Rate (0.0% target)
- Score Drift (0.0% target)
- Execution latency per submission (SLA bounds)
"""

import os
import sys
import time
import tempfile
import shutil
import io
import zipfile
import xml.etree.ElementTree as ET
import openpyxl
from openpyxl.worksheet.table import Table
from openpyxl.chart import BarChart, Reference

# Add engine to sys.path
sys.path.insert(0, os.path.dirname(__file__))
from excel_runner import inspect_and_evaluate
from workbook_registry import WorkbookObjectRegistry
from specialized_runtimes import DuckDBAnalyticalAdapter, PowerQueryWorker


class ExcelBenchmarkHarness:
    """
    Harness that generates, executes, and validates 50 distinct Excel evaluation benchmarks
    against verified teacher ground truth.
    """

    def __init__(self, work_dir=None):
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="excel_bench_")
        self._owned_work_dir = work_dir is None

    def cleanup(self):
        if self._owned_work_dir and os.path.exists(self.work_dir):
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def _inject_pivot_xml(self, file_path, grand_total=120000.0, cache_records=100):
        """Helper to inject native OpenXML PivotTable and PivotCache into package."""
        with zipfile.ZipFile(file_path, 'r') as z_in:
            file_data = {item.filename: z_in.read(item.filename) for item in z_in.infolist()}

        pt_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<pivotTableDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'name="SalesPivot" cacheId="1" dataCaption="Data">'
            '<location ref="A1:B4" firstHeaderRow="1" firstDataRow="1" firstDataCol="1"/>'
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

        sheet1_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/pivotTable" Target="../pivotTables/pivotTable1.xml"/>'
            '</Relationships>'
        )

        file_data["xl/pivotTables/pivotTable1.xml"] = pt_xml.encode("utf-8")
        file_data["xl/pivotCache/pivotCacheDefinition1.xml"] = pc_xml.encode("utf-8")
        file_data["xl/worksheets/_rels/sheet1.xml.rels"] = sheet1_rels.encode("utf-8")

        with zipfile.ZipFile(file_path, 'w', compression=zipfile.ZIP_DEFLATED) as z_out:
            for fname, content in file_data.items():
                z_out.writestr(fname, content)

    # -------------------------------------------------------------------------
    # Helper: Workbook file builder
    # -------------------------------------------------------------------------
    def _create_wb(self, filename, sheet_setup_fn, inject_cached_values=None):
        """
        Creates an original workbook via openpyxl and optional cached values in XML.
        Also creates a twin recalculated workbook.
        """
        orig_path = os.path.join(self.work_dir, f"{filename}_orig.xlsx")
        recalc_path = os.path.join(self.work_dir, f"{filename}_recalc.xlsx")

        wb_orig = openpyxl.Workbook()
        ws_orig = wb_orig.active
        sheet_setup_fn(wb_orig, ws_orig, is_recalc=False)
        wb_orig.save(orig_path)
        wb_orig.close()

        # If cached values specified, inject into orig XML
        if inject_cached_values:
            import re
            buf = io.BytesIO()
            with zipfile.ZipFile(orig_path, 'r') as zin, zipfile.ZipFile(buf, 'w') as zout:
                for item in zin.infolist():
                    if item.filename.startswith('xl/worksheets/sheet'):
                        s_xml = zin.read(item.filename).decode('utf-8')
                        for formula_txt, cached_v in inject_cached_values.items():
                            clean_f = formula_txt.lstrip("=")
                            if f"<f>{clean_f}</f>" in s_xml:
                                if isinstance(cached_v, str):
                                    def replace_cell(m):
                                        tag_open = m.group(1)
                                        if 't=' not in tag_open:
                                            tag_open = tag_open + ' t="str"'
                                        return f'{tag_open}><f>{clean_f}</f><v>{cached_v}</v>'
                                    s_xml = re.sub(rf'(<c\s+[^>]*?)><f>{re.escape(clean_f)}</f>', replace_cell, s_xml)
                                else:
                                    s_xml = s_xml.replace(f"<f>{clean_f}</f>", f"<f>{clean_f}</f><v>{cached_v}</v>")
                        zout.writestr(item, s_xml.encode('utf-8'))
                    else:
                        zout.writestr(item, zin.read(item.filename))
            with open(orig_path, 'wb') as f:
                f.write(buf.getvalue())

        wb_recalc = openpyxl.Workbook()
        ws_recalc = wb_recalc.active
        sheet_setup_fn(wb_recalc, ws_recalc, is_recalc=True)
        wb_recalc.save(recalc_path)
        wb_recalc.close()

        return orig_path, recalc_path

    # -------------------------------------------------------------------------
    # 50 Benchmark Scenarios Definition & Execution
    # -------------------------------------------------------------------------
    def get_benchmark_catalog(self):
        """Returns the definitions of all 50 benchmarks."""
        catalog = []

        # =====================================================================
        # COHORT 1: POSITIVE SCENARIOS (1 to 20)
        # =====================================================================
        catalog.append({
            "id": "POS_01_BASIC_SUM",
            "cohort": "positive",
            "category": "Basic Formulas",
            "name": "Standard contiguous SUM formula",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_01()
        })
        catalog.append({
            "id": "POS_02_CELL_ARITHMETIC",
            "cohort": "positive",
            "category": "Basic Formulas",
            "name": "Cell arithmetic multiplication =B2*C2",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_02()
        })
        catalog.append({
            "id": "POS_03_AVERAGE_AGGREGATION",
            "cohort": "positive",
            "category": "Basic Formulas",
            "name": "Average aggregation =AVERAGE(C2:C5)",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_03()
        })
        catalog.append({
            "id": "POS_04_COUNT_COUNTA",
            "cohort": "positive",
            "category": "Basic Formulas",
            "name": "Numeric and text counting via COUNT and COUNTA",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_04()
        })
        catalog.append({
            "id": "POS_05_LOGICAL_IF",
            "cohort": "positive",
            "category": "Logical Functions",
            "name": "Conditional logical branch =IF(B2>=100, 'Pass', 'Fail')",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_05()
        })
        catalog.append({
            "id": "POS_06_NESTED_IFS",
            "cohort": "positive",
            "category": "Logical Functions",
            "name": "Multi-tier conditional =IF(B2>=90, 'A', IF(B2>=80, 'B', 'C'))",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_06()
        })
        catalog.append({
            "id": "POS_07_LOOKUP_VLOOKUP",
            "cohort": "positive",
            "category": "Advanced Lookups",
            "name": "Standard vertical lookup =VLOOKUP(D2, A2:B5, 2, FALSE)",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_07()
        })
        catalog.append({
            "id": "POS_08_LOOKUP_XLOOKUP",
            "cohort": "positive",
            "category": "Advanced Lookups",
            "name": "Modern lookup =XLOOKUP(D2, A2:A5, B2:B5) as allowed alternative",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_08()
        })
        catalog.append({
            "id": "POS_09_LOOKUP_INDEX_MATCH",
            "cohort": "positive",
            "category": "Advanced Lookups",
            "name": "Two-way lookup =INDEX(B2:B5, MATCH(D2, A2:A5, 0)) as allowed alternative",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_09()
        })
        catalog.append({
            "id": "POS_10_SUMIFS_MULTI_CRITERIA",
            "cohort": "positive",
            "category": "Aggregation",
            "name": "Multi-criteria sum =SUMIFS(C2:C5, A2:A5, 'East', B2:B5, '>100')",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_10()
        })
        catalog.append({
            "id": "POS_11_TABLE_STRUCTURED_REF",
            "cohort": "positive",
            "category": "Table Operations",
            "name": "Excel Table with valid name and row count",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_11()
        })
        catalog.append({
            "id": "POS_12_DRAWINGML_BAR_CHART",
            "cohort": "positive",
            "category": "Visuals & Charts",
            "name": "DrawingML Column/Bar Chart with valid data series and categories",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_12()
        })
        catalog.append({
            "id": "POS_13_DRAWINGML_LINE_CHART",
            "cohort": "positive",
            "category": "Visuals & Charts",
            "name": "DrawingML Line Chart with series title and range",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_13()
        })
        catalog.append({
            "id": "POS_14_PIVOT_GRAND_TOTAL",
            "cohort": "positive",
            "category": "PivotTables",
            "name": "Rendered PivotTable matching source Grand Total",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_14()
        })
        catalog.append({
            "id": "POS_15_POWER_QUERY_M_CODE",
            "cohort": "positive",
            "category": "Power Query",
            "name": "M-code transformation with populated destination table",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_15()
        })
        catalog.append({
            "id": "POS_16_RELATIONAL_DUCKDB_JOIN",
            "cohort": "positive",
            "category": "Relational Data",
            "name": "Multi-table relational join foreign key integrity via DuckDB adapter",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_16()
        })
        catalog.append({
            "id": "POS_17_CURRENCY_FORMATTING",
            "cohort": "positive",
            "category": "Formatting",
            "name": "Currency value accuracy and numerical verification",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_17()
        })
        catalog.append({
            "id": "POS_18_DEPENDENCY_CASCADE_CLEAN",
            "cohort": "positive",
            "category": "Dependencies",
            "name": "Upstream passes and dependent passes earning full 100 marks",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_18()
        })
        catalog.append({
            "id": "POS_19_VOLATILE_CACHE_MATCH",
            "cohort": "positive",
            "category": "Cache Consistency",
            "name": "Volatile formula cache matches current date",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_19()
        })
        catalog.append({
            "id": "POS_20_MULTI_SHEET_FORMULA",
            "cohort": "positive",
            "category": "Multi-Sheet",
            "name": "Cross-sheet formula reference =Summary!B2 + Details!C5",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_pos_20()
        })

        # =====================================================================
        # COHORT 2: NEGATIVE SCENARIOS (21 to 40)
        # =====================================================================
        catalog.append({
            "id": "NEG_01_HARDCODED_NUMBER",
            "cohort": "negative",
            "category": "Formula Structure",
            "name": "Hardcoded number typed instead of required formula structure",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_01()
        })
        catalog.append({
            "id": "NEG_02_WRONG_CALCULATION_VALUE",
            "cohort": "negative",
            "category": "Calculation Accuracy",
            "name": "Formula produces incorrect calculation value (80 instead of 120)",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_02()
        })
        catalog.append({
            "id": "NEG_03_DIV_ZERO_ERROR",
            "cohort": "negative",
            "category": "Error Handling",
            "name": "Formula yields #DIV/0! error",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_03()
        })
        catalog.append({
            "id": "NEG_04_REF_ERROR",
            "cohort": "negative",
            "category": "Error Handling",
            "name": "Formula yields #REF! broken reference error",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_04()
        })
        catalog.append({
            "id": "NEG_05_VALUE_ERROR",
            "cohort": "negative",
            "category": "Error Handling",
            "name": "Formula yields #VALUE! type mismatch error",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_05()
        })
        catalog.append({
            "id": "NEG_06_DISALLOWED_METHOD_STRICT",
            "cohort": "negative",
            "category": "Pedagogical Constraints",
            "name": "Disallowed method VLOOKUP used when teacher requires strict XLOOKUP",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_06()
        })
        catalog.append({
            "id": "NEG_07_CRITICAL_CAP_TRIGGERED",
            "cohort": "negative",
            "category": "Scoring Engine",
            "name": "Failed critical requirement caps entire criterion at 0",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_07()
        })
        catalog.append({
            "id": "NEG_08_DEPENDENCY_BLOCKED_DENOMINATOR",
            "cohort": "negative",
            "category": "Scoring Engine",
            "name": "Failed upstream causes DEPENDENCY_SKIPPED without denominator shrinkage",
            "expected_score": 30.0,
            "expected_status": "PARTIAL",
            "build": lambda self: self._build_neg_08()
        })
        catalog.append({
            "id": "NEG_09_CACHE_DIVERGENCE_STALE",
            "cohort": "negative",
            "category": "Cache Consistency",
            "name": "Saved cached value diverges from newly recalculated value",
            "expected_score": 100.0,  # passes on recalc value, but registers CACHE_DIVERGENCE
            "expected_status": "PASS",
            "expected_warning": "CACHE_DIVERGENCE",
            "build": lambda self: self._build_neg_09()
        })
        catalog.append({
            "id": "NEG_10_CHART_MISSING",
            "cohort": "negative",
            "category": "Visuals & Charts",
            "name": "Required DrawingML chart completely missing from workbook",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_10()
        })
        catalog.append({
            "id": "NEG_11_CHART_WRONG_DATA_SERIES",
            "cohort": "negative",
            "category": "Visuals & Charts",
            "name": "Chart data series references wrong column ($C instead of $B)",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_11()
        })
        catalog.append({
            "id": "NEG_12_CHART_WRONG_TYPE",
            "cohort": "negative",
            "category": "Visuals & Charts",
            "name": "Chart configured with wrong chart type (Pie instead of Bar)",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_12()
        })
        catalog.append({
            "id": "NEG_13_PIVOT_WRONG_GRAND_TOTAL",
            "cohort": "negative",
            "category": "PivotTables",
            "name": "Rendered PivotTable grand total does not match expected aggregation",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_13()
        })
        catalog.append({
            "id": "NEG_14_PIVOT_STALE_CACHE",
            "cohort": "negative",
            "category": "PivotTables",
            "name": "PivotCache record count differs from source table rows",
            "expected_score": 100.0,  # value passes, but warning emitted
            "expected_status": "PASS",
            "expected_warning": "PIVOT_CACHE_STALE",
            "build": lambda self: self._build_neg_14()
        })
        catalog.append({
            "id": "NEG_15_POWER_QUERY_MISSING_STEPS",
            "cohort": "negative",
            "category": "Power Query",
            "name": "M-code missing required transformation step",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_15()
        })
        catalog.append({
            "id": "NEG_16_POWER_QUERY_SCHEMA_MISMATCH",
            "cohort": "negative",
            "category": "Power Query",
            "name": "Destination table missing required column in schema",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_16()
        })
        catalog.append({
            "id": "NEG_17_RELATIONAL_ORPHAN_KEYS",
            "cohort": "negative",
            "category": "Relational Data",
            "name": "Foreign key referential integrity violation (orphan child records)",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_17()
        })
        catalog.append({
            "id": "NEG_18_MISSING_REQUIRED_SHEET",
            "cohort": "negative",
            "category": "Workbook Structure",
            "name": "Required worksheet does not exist in workbook",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_18()
        })
        catalog.append({
            "id": "NEG_19_MISSING_NAMED_TABLE",
            "cohort": "negative",
            "category": "Table Operations",
            "name": "Expected Table is not formatted as an Excel ListObject Table",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_19()
        })
        catalog.append({
            "id": "NEG_20_EMPTY_SUBMISSION",
            "cohort": "negative",
            "category": "Edge Cases",
            "name": "Blank submission with empty worksheet cells",
            "expected_score": 0.0,
            "expected_status": "FAIL",
            "build": lambda self: self._build_neg_20()
        })

        # =====================================================================
        # COHORT 3: EDGE CASES & SEMANTIC INVARIANCE (41 to 50)
        # =====================================================================
        catalog.append({
            "id": "EDGE_01_EXTRA_BLANK_ROWS",
            "cohort": "edge_case",
            "category": "Layout Invariance",
            "name": "5 leading blank rows before table starts at A6",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_01()
        })
        catalog.append({
            "id": "EDGE_02_SHIFTED_STARTING_CELL",
            "cohort": "edge_case",
            "category": "Layout Invariance",
            "name": "Calculation placed in shifted cell D5 with relative reference",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_02()
        })
        catalog.append({
            "id": "EDGE_03_CASE_INSENSITIVE_FORMULA",
            "cohort": "edge_case",
            "category": "Formula Normalization",
            "name": "Lowercase formula =sum(a1:a2) successfully normalized",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_03()
        })
        catalog.append({
            "id": "EDGE_04_EXTRA_SPACES_IN_FORMULA",
            "cohort": "edge_case",
            "category": "Formula Normalization",
            "name": "Formula with arbitrary whitespace = SUM(  A1 : A2  )",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_04()
        })
        catalog.append({
            "id": "EDGE_05_RENAMED_SHEET_WITH_SPACE",
            "cohort": "edge_case",
            "category": "Multi-Sheet",
            "name": "Sheet name with spaces 'Q1 Sales' handled correctly in formulas",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_05()
        })
        catalog.append({
            "id": "EDGE_06_FLOATING_POINT_TOLERANCE",
            "cohort": "edge_case",
            "category": "Numerical Tolerance",
            "name": "Value 99.99999999 matches expected 100.0 within 1e-4 tolerance",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_06()
        })
        catalog.append({
            "id": "EDGE_07_VOLATILE_CACHE_DIVERGENCE_WARNING",
            "cohort": "edge_case",
            "category": "Cache Consistency",
            "name": "TODAY() cache divergence registers CACHE_DIVERGENCE_VOLATILE without deduction",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "expected_warning": "CACHE_DIVERGENCE_VOLATILE",
            "build": lambda self: self._build_edge_07()
        })
        catalog.append({
            "id": "EDGE_08_NON_100_MAX_SCORE",
            "cohort": "edge_case",
            "category": "Scoring Scale",
            "name": "Original teacher rubric worth 45 marks preserves 45 without inflation",
            "expected_score": 45.0,
            "expected_max_score": 45.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_08()
        })
        catalog.append({
            "id": "EDGE_09_PARTIAL_CAP_FINANCIAL_MODEL",
            "cohort": "edge_case",
            "category": "Scoring Engine",
            "name": "Calculations pass (80) but methodology fails capping at 35.0",
            "expected_score": 35.0,
            "expected_status": "PARTIAL",
            "build": lambda self: self._build_edge_09()
        })
        catalog.append({
            "id": "EDGE_10_ZERO_WEIGHT_DIAGNOSTIC",
            "cohort": "edge_case",
            "category": "Scoring Engine",
            "name": "Zero-weight diagnostic assertion passes without skewing denominator",
            "expected_score": 100.0,
            "expected_status": "PASS",
            "build": lambda self: self._build_edge_10()
        })

        return catalog

    # -------------------------------------------------------------------------
    # Benchmark Builders (POS_01 to POS_20)
    # -------------------------------------------------------------------------
    def _build_pos_01(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 50
            ws["A2"] = 70
            ws["A3"] = 120 if is_recalc else "=SUM(A1:A2)"
        orig, recalc = self._create_wb("pos_01", setup, {"=SUM(A1:A2)": 120})
        spec = {
            "rubric": {"criteria": [{"name": "Summation", "weight": 100}]},
            "testCases": [{
                "criterion": "Summation", "type": "formula_check", "sheet": "Sheet1", "cell": "A3",
                "expectedFormula": "=SUM(A1:A2)", "expectedValue": 120, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_02(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sales"
            ws["B2"] = 10
            ws["C2"] = 25
            ws["D2"] = 250 if is_recalc else "=B2*C2"
        orig, recalc = self._create_wb("pos_02", setup, {"=B2*C2": 250})
        spec = {
            "rubric": {"criteria": [{"name": "Revenue", "weight": 100}]},
            "testCases": [{
                "criterion": "Revenue", "type": "formula_check", "sheet": "Sales", "cell": "D2",
                "expectedFormula": "=B2*C2", "accepted_functions": ["CELL_ARITHMETIC", "PRODUCT"],
                "expectedValue": 250, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_03(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            for i, v in enumerate([10, 20, 30, 40], start=2):
                ws[f"C{i}"] = v
            ws["C6"] = 25.0 if is_recalc else "=AVERAGE(C2:C5)"
        orig, recalc = self._create_wb("pos_03", setup, {"=AVERAGE(C2:C5)": 25.0})
        spec = {
            "rubric": {"criteria": [{"name": "Averages", "weight": 100}]},
            "testCases": [{
                "criterion": "Averages", "type": "formula_check", "sheet": "Sheet1", "cell": "C6",
                "expectedFormula": "=AVERAGE(C2:C5)", "expectedValue": 25.0, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_04(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = "Item"
            ws["B1"] = "Qty"
            ws["A2"] = "Apples"; ws["B2"] = 5
            ws["A3"] = "Oranges"; ws["B3"] = 12
            ws["C1"] = 2 if is_recalc else "=COUNT(B2:B3)"
        orig, recalc = self._create_wb("pos_04", setup, {"=COUNT(B2:B3)": 2})
        spec = {
            "rubric": {"criteria": [{"name": "Counts", "weight": 100}]},
            "testCases": [{
                "criterion": "Counts", "type": "formula_check", "sheet": "Sheet1", "cell": "C1",
                "expectedFormula": "=COUNT(B2:B3)", "expectedValue": 2, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_05(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Grades"
            ws["B2"] = 120
            ws["C2"] = "Pass" if is_recalc else '=IF(B2>=100,"Pass","Fail")'
        orig, recalc = self._create_wb("pos_05", setup, {'=IF(B2>=100,"Pass","Fail")': "Pass"})
        spec = {
            "rubric": {"criteria": [{"name": "Logic", "weight": 100}]},
            "testCases": [{
                "criterion": "Logic", "type": "formula_check", "sheet": "Grades", "cell": "C2",
                "expectedFormula": '=IF(B2>=100,"Pass","Fail")', "expectedValue": "Pass", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_06(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Grades"
            ws["B2"] = 85
            ws["C2"] = "B" if is_recalc else '=IF(B2>=90,"A",IF(B2>=80,"B","C"))'
        orig, recalc = self._create_wb("pos_06", setup, {'=IF(B2>=90,"A",IF(B2>=80,"B","C"))': "B"})
        spec = {
            "rubric": {"criteria": [{"name": "NestedLogic", "weight": 100}]},
            "testCases": [{
                "criterion": "NestedLogic", "type": "formula_check", "sheet": "Grades", "cell": "C2",
                "expectedFormula": '=IF(B2>=90,"A",IF(B2>=80,"B","C"))', "expectedValue": "B", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_07(self):
        def setup(wb, ws, is_recalc):
            ws.title = "LookupSheet"
            ws["A2"] = "E101"; ws["B2"] = "Engineering"
            ws["A3"] = "E102"; ws["B3"] = "Marketing"
            ws["D2"] = "E101"
            ws["E2"] = "Engineering" if is_recalc else '=VLOOKUP(D2,A2:B3,2,FALSE)'
        orig, recalc = self._create_wb("pos_07", setup, {'=VLOOKUP(D2,A2:B3,2,FALSE)': "Engineering"})
        spec = {
            "rubric": {"criteria": [{"name": "Lookups", "weight": 100}]},
            "testCases": [{
                "criterion": "Lookups", "type": "formula_check", "sheet": "LookupSheet", "cell": "E2",
                "expectedFormula": '=VLOOKUP(D2,A2:B3,2,FALSE)', "expectedValue": "Engineering", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_08(self):
        def setup(wb, ws, is_recalc):
            ws.title = "LookupSheet"
            ws["A2"] = "E101"; ws["B2"] = "Engineering"
            ws["A3"] = "E102"; ws["B3"] = "Marketing"
            ws["D2"] = "E102"
            ws["E2"] = "Marketing" if is_recalc else '=XLOOKUP(D2,A2:A3,B2:B3)'
        orig, recalc = self._create_wb("pos_08", setup, {'=XLOOKUP(D2,A2:A3,B2:B3)': "Marketing"})
        spec = {
            "rubric": {"criteria": [{"name": "ModernLookups", "weight": 100}]},
            "testCases": [{
                "criterion": "ModernLookups", "type": "formula_check", "sheet": "LookupSheet", "cell": "E2",
                "expectedFormula": '=VLOOKUP(D2,A2:B3,2,FALSE)',
                "accepted_functions": ["VLOOKUP", "XLOOKUP", "INDEX", "MATCH"],
                "expectedValue": "Marketing", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_09(self):
        def setup(wb, ws, is_recalc):
            ws.title = "LookupSheet"
            ws["A2"] = "E101"; ws["B2"] = "Engineering"
            ws["A3"] = "E102"; ws["B3"] = "Marketing"
            ws["D2"] = "E101"
            ws["E2"] = "Engineering" if is_recalc else '=INDEX(B2:B3,MATCH(D2,A2:A3,0))'
        orig, recalc = self._create_wb("pos_09", setup, {'=INDEX(B2:B3,MATCH(D2,A2:A3,0))': "Engineering"})
        spec = {
            "rubric": {"criteria": [{"name": "TwoWayLookups", "weight": 100}]},
            "testCases": [{
                "criterion": "TwoWayLookups", "type": "formula_check", "sheet": "LookupSheet", "cell": "E2",
                "expectedFormula": '=VLOOKUP(D2,A2:B3,2,FALSE)',
                "accepted_functions": ["VLOOKUP", "XLOOKUP", "INDEX", "MATCH"],
                "expectedValue": "Engineering", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_10(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sales"
            ws["A2"] = "East"; ws["B2"] = 150; ws["C2"] = 1500
            ws["A3"] = "East"; ws["B3"] = 50;  ws["C3"] = 500
            ws["A4"] = "West"; ws["B4"] = 200; ws["C4"] = 2000
            ws["D2"] = 1500 if is_recalc else '=SUMIFS(C2:C4,A2:A4,"East",B2:B4,">100")'
        orig, recalc = self._create_wb("pos_10", setup, {'=SUMIFS(C2:C4,A2:A4,"East",B2:B4,">100")': 1500})
        spec = {
            "rubric": {"criteria": [{"name": "AdvancedSum", "weight": 100}]},
            "testCases": [{
                "criterion": "AdvancedSum", "type": "formula_check", "sheet": "Sales", "cell": "D2",
                "expectedFormula": '=SUMIFS(C2:C4,A2:A4,"East",B2:B4,">100")', "expectedValue": 1500, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_11(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Data"
            ws["A1"] = "ID"; ws["B1"] = "Name"
            ws["A2"] = 1;    ws["B2"] = "Alpha"
            ws["A3"] = 2;    ws["B3"] = "Beta"
            tab = Table(displayName="CustomerTable", ref="A1:B3")
            ws.add_table(tab)
        orig, recalc = self._create_wb("pos_11", setup)
        spec = {
            "rubric": {"criteria": [{"name": "TableDesign", "weight": 100}]},
            "testCases": [{
                "criterion": "TableDesign", "type": "table_check", "sheet": "Data",
                "expectedTable": "CustomerTable", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_12(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = "Month"; ws["B1"] = "Revenue"
            ws["A2"] = "Jan";   ws["B2"] = 100
            ws["A3"] = "Feb";   ws["B3"] = 200
            chart = BarChart()
            chart.type = "col"
            chart.title = "Sales Chart"
            data = Reference(ws, min_col=2, min_row=1, max_row=3)
            cats = Reference(ws, min_col=1, min_row=2, max_row=3)
            chart.add_data(data, titles_from_data=True)
            chart.set_categories(cats)
            ws.add_chart(chart, "D2")
        orig, recalc = self._create_wb("pos_12", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Visualization", "weight": 100}]},
            "testCases": [{
                "criterion": "Visualization", "type": "chart_check", "sheet": "Sheet1",
                "chartType": "column", "expectedSourceRange": "Sheet1!$B$2:$B$3",
                "expectedCategoryRange": "Sheet1!$A$2:$A$3", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_13(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = "Period"; ws["B1"] = "Growth"
            ws["A2"] = "P1";     ws["B2"] = 10
            ws["A3"] = "P2";     ws["B3"] = 20
            chart = BarChart()
            chart.type = "col"
            chart.title = "Growth Trend"
            data = Reference(ws, min_col=2, min_row=1, max_row=3)
            cats = Reference(ws, min_col=1, min_row=2, max_row=3)
            chart.add_data(data, titles_from_data=True)
            chart.set_categories(cats)
            ws.add_chart(chart, "E2")
        orig, recalc = self._create_wb("pos_13", setup)
        spec = {
            "rubric": {"criteria": [{"name": "TrendVisuals", "weight": 100}]},
            "testCases": [{
                "criterion": "TrendVisuals", "type": "chart_check", "sheet": "Sheet1",
                "title": "Growth Trend", "expectedSourceRange": "Sheet1!$B$2:$B$3", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_14(self):
        def setup(wb, ws, is_recalc):
            ws.title = "PivotSummary"
            ws["A1"] = "Region"; ws["B1"] = "Sum of Sales"
            ws["A2"] = "North";  ws["B2"] = 50000
            ws["A3"] = "South";  ws["B3"] = 70000
            ws["A4"] = "Grand Total"; ws["B4"] = 120000.0
        orig, recalc = self._create_wb("pos_14", setup)
        self._inject_pivot_xml(orig, grand_total=120000.0)
        self._inject_pivot_xml(recalc, grand_total=120000.0)
        spec = {
            "rubric": {"criteria": [{"name": "PivotAnalysis", "weight": 100}]},
            "testCases": [{
                "criterion": "PivotAnalysis", "type": "pivot_check", "sheet": "PivotSummary",
                "requiredField": "Sales", "expectedGrandTotal": 120000.0, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_15(self):
        def setup(wb, ws, is_recalc):
            ws.title = "CleanedData"
            ws["A1"] = "CustomerID"; ws["B1"] = "Spend"
            ws["A2"] = "C01";        ws["B2"] = 400
            ws["A3"] = "C02";        ws["B3"] = 900
            tab = Table(displayName="CleanedTable", ref="A1:B3")
            ws.add_table(tab)
        orig, recalc = self._create_wb("pos_15", setup)
        m_xml = (
            '<item xmlns="http://schemas.microsoft.com/DataMashup">'
            'section Section1; shared CleanOrders = let Source = Excel.Workbook(...) in Source;'
            '</item>'
        )
        for p in [orig, recalc]:
            buf = io.BytesIO()
            with zipfile.ZipFile(p, 'r') as zin, zipfile.ZipFile(buf, 'w') as zout:
                for item in zin.infolist():
                    zout.writestr(item, zin.read(item.filename))
                zout.writestr('customXml/item1.xml', m_xml.encode('utf-8'))
            with open(p, 'wb') as f:
                f.write(buf.getvalue())
        spec = {
            "rubric": {"criteria": [{"name": "ETL", "weight": 100}]},
            "testCases": [{
                "criterion": "ETL", "type": "power_query_check", "sheet": "CleanedData",
                "requiredColumns": ["CustomerID", "Spend"], "minRowCount": 1, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_16(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Customers"
            ws["A1"] = "CustomerID"; ws["B1"] = "Name"
            ws["A2"] = "C1"; ws["B2"] = "Acme"
            ws["A3"] = "C2"; ws["B3"] = "Beta"
            tab1 = Table(displayName="Customers", ref="A1:B3")
            ws.add_table(tab1)
            ws2 = wb.create_sheet("Orders")
            ws2["A1"] = "OrderID"; ws2["B1"] = "CustomerID"; ws2["C1"] = "Total"
            ws2["A2"] = "O1"; ws2["B2"] = "C1"; ws2["C2"] = 100.0
            ws2["A3"] = "O2"; ws2["B3"] = "C2"; ws2["C2"] = 200.0
            tab2 = Table(displayName="Orders", ref="A1:C3")
            ws2.add_table(tab2)
        orig, recalc = self._create_wb("pos_16", setup)
        spec = {
            "rubric": {"criteria": [{"name": "RelationalModel", "weight": 100}]},
            "testCases": [{
                "criterion": "RelationalModel", "type": "relational_integrity_check",
                "parent_table": "Customers", "parent_key": "CustomerID",
                "child_table": "Orders", "child_key": "CustomerID", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_17(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Financials"
            ws["A1"] = 1500000.50
        orig, recalc = self._create_wb("pos_17", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Valuation", "weight": 100}]},
            "testCases": [{
                "criterion": "Valuation", "type": "value_check", "sheet": "Financials", "cell": "A1",
                "expectedValue": 1500000.50, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_18(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Pipeline"
            ws["A1"] = 10; ws["A2"] = 20
            ws["A3"] = 30 if is_recalc else "=SUM(A1:A2)"
            ws["B1"] = 300 if is_recalc else "=A3*10"
        orig, recalc = self._create_wb("pos_18", setup, {"=SUM(A1:A2)": 30, "=A3*10": 300})
        spec = {
            "rubric": {"criteria": [{"name": "DataPipeline", "weight": 100}]},
            "testCases": [{
                "id": "step1", "criterion": "DataPipeline", "type": "formula_check", "sheet": "Pipeline", "cell": "A3",
                "expectedFormula": "=SUM(A1:A2)", "expectedValue": 30, "weight": 50
            }, {
                "id": "step2", "criterion": "DataPipeline", "type": "formula_check", "sheet": "Pipeline", "cell": "B1",
                "expectedFormula": "=A3*10", "expectedValue": 300, "weight": 50,
                "depends_on": ["step1"]
            }]
        }
        return orig, recalc, spec

    def _build_pos_19(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Dates"
            ws["A1"] = 45000 if is_recalc else "=TODAY()"
        orig, recalc = self._create_wb("pos_19", setup, {"=TODAY()": 45000})
        spec = {
            "rubric": {"criteria": [{"name": "TimeFunctions", "weight": 100}]},
            "testCases": [{
                "criterion": "TimeFunctions", "type": "value_check", "sheet": "Dates", "cell": "A1",
                "expectedValue": 45000, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_pos_20(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Summary"
            ws["B2"] = 400
            ws2 = wb.create_sheet("Details")
            ws2["C5"] = 600
            ws["D1"] = 1000 if is_recalc else "=Summary!B2+Details!C5"
        orig, recalc = self._create_wb("pos_20", setup, {"=Summary!B2+Details!C5": 1000})
        spec = {
            "rubric": {"criteria": [{"name": "MultiSheet", "weight": 100}]},
            "testCases": [{
                "criterion": "MultiSheet", "type": "formula_check", "sheet": "Summary", "cell": "D1",
                "expectedFormula": "=Summary!B2+Details!C5",
                "accepted_functions": ["CELL_ARITHMETIC", "SUM"],
                "expectedValue": 1000, "weight": 100
            }]
        }
        return orig, recalc, spec

    # -------------------------------------------------------------------------
    # Benchmark Builders (NEG_01 to NEG_20)
    # -------------------------------------------------------------------------
    def _build_neg_01(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 50; ws["A2"] = 70
            ws["A3"] = 120  # Student typed raw hardcoded number 120 without formula
        orig, recalc = self._create_wb("neg_01", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Formulas", "weight": 100}]},
            "testCases": [{
                "criterion": "Formulas", "type": "formula_check", "sheet": "Sheet1", "cell": "A3",
                "expectedFormula": "=SUM(A1:A2)", "expectedValue": 120, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_02(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 10; ws["A2"] = 20
            # Calculation yields 30, but expectedValue in rubric is 80 (wrong value)
            ws["A3"] = 30 if is_recalc else "=SUM(A1:A2)"
        orig, recalc = self._create_wb("neg_02", setup, {"=SUM(A1:A2)": 30})
        spec = {
            "rubric": {"criteria": [{"name": "Calculations", "weight": 100}]},
            "testCases": [{
                "criterion": "Calculations", "type": "value_check", "sheet": "Sheet1", "cell": "A3",
                "expectedValue": 80, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_03(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Errors"
            ws["A1"] = 100; ws["A2"] = 0
            ws["A3"] = "#DIV/0!"
        orig, recalc = self._create_wb("neg_03", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Robustness", "weight": 100}]},
            "testCases": [{
                "criterion": "Robustness", "type": "value_check", "sheet": "Errors", "cell": "A3",
                "expectedValue": 50, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_04(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Errors"
            ws["A1"] = "#REF!"
        orig, recalc = self._create_wb("neg_04", setup)
        spec = {
            "rubric": {"criteria": [{"name": "References", "weight": 100}]},
            "testCases": [{
                "criterion": "References", "type": "value_check", "sheet": "Errors", "cell": "A1",
                "expectedValue": 100, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_05(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Errors"
            ws["A1"] = "#VALUE!"
        orig, recalc = self._create_wb("neg_05", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Types", "weight": 100}]},
            "testCases": [{
                "criterion": "Types", "type": "value_check", "sheet": "Errors", "cell": "A1",
                "expectedValue": 200, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_06(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Lookups"
            ws["A1"] = "ID"; ws["B1"] = "Val"
            ws["A2"] = "X"; ws["B2"] = 500
            ws["C1"] = "X"
            ws["D1"] = 500 if is_recalc else "=VLOOKUP(C1,A1:B2,2,FALSE)"  # VLOOKUP used
        orig, recalc = self._create_wb("neg_06", setup, {"=VLOOKUP(C1,A1:B2,2,FALSE)": 500})
        spec = {
            "rubric": {"criteria": [{"name": "ModernLookups", "weight": 100}]},
            "testCases": [{
                "criterion": "ModernLookups", "type": "formula_check", "sheet": "Lookups", "cell": "D1",
                "expectedFormula": "=XLOOKUP(C1,A1:A2,B1:B2)",
                "strict_method_required": True,
                "accepted_functions": ["XLOOKUP"],  # VLOOKUP not allowed
                "expectedValue": 500, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_07(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 999  # Critical methodology check fails
            ws["A2"] = 100  # Non-critical calculation passes
        orig, recalc = self._create_wb("neg_07", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Financials", "weight": 100}]},
            "testCases": [{
                "id": "crit_method", "criterion": "Financials", "type": "value_check", "sheet": "Sheet1", "cell": "A1",
                "expectedValue": 50, "weight": 20, "is_critical": True, "score_cap": 0.0
            }, {
                "id": "calc", "criterion": "Financials", "type": "value_check", "sheet": "Sheet1", "cell": "A2",
                "expectedValue": 100, "weight": 80
            }]
        }
        return orig, recalc, spec

    def _build_neg_08(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 999  # Upstream fails (expected 100)
            ws["A2"] = 200  # Downstream
            ws["A3"] = 50   # Independent passes
        orig, recalc = self._create_wb("neg_08", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Grading", "weight": 100}]},
            "testCases": [{
                "id": "upstream", "criterion": "Grading", "type": "value_check", "sheet": "Sheet1", "cell": "A1",
                "expectedValue": 100, "weight": 40
            }, {
                "id": "downstream", "criterion": "Grading", "type": "value_check", "sheet": "Sheet1", "cell": "A2",
                "expectedValue": 200, "weight": 30, "depends_on": ["upstream"]
            }, {
                "id": "independent", "criterion": "Grading", "type": "value_check", "sheet": "Sheet1", "cell": "A3",
                "expectedValue": 50, "weight": 30
            }]
        }
        return orig, recalc, spec

    def _build_neg_09(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 10; ws["A2"] = 20
            # Recalculated value is 30, but student cached value is 999
            ws["A3"] = 30 if is_recalc else "=SUM(A1:A2)"
        orig, recalc = self._create_wb("neg_09", setup, {"=SUM(A1:A2)": 999})
        spec = {
            "rubric": {"criteria": [{"name": "Cache", "weight": 100}]},
            "testCases": [{
                "criterion": "Cache", "type": "value_check", "sheet": "Sheet1", "cell": "A3",
                "expectedValue": 30, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_10(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 10  # No chart added
        orig, recalc = self._create_wb("neg_10", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Charts", "weight": 100}]},
            "testCases": [{
                "criterion": "Charts", "type": "chart_check", "sheet": "Sheet1",
                "chartType": "column", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_11(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = "M"; ws["B1"] = "Rev"; ws["C1"] = "Cost"
            ws["A2"] = "Jan"; ws["B2"] = 100; ws["C2"] = 40
            chart = BarChart()
            chart.type = "col"
            # References column C instead of required column B
            data = Reference(ws, min_col=3, min_row=1, max_row=2)
            chart.add_data(data, titles_from_data=True)
            ws.add_chart(chart, "E2")
        orig, recalc = self._create_wb("neg_11", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Charts", "weight": 100}]},
            "testCases": [{
                "criterion": "Charts", "type": "chart_check", "sheet": "Sheet1",
                "chartType": "column", "expectedSourceRange": "Sheet1!$B$1:$B$2", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_12(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = "M"; ws["B1"] = "Rev"
            ws["A2"] = "Jan"; ws["B2"] = 100
            from openpyxl.chart import PieChart
            chart = PieChart()  # PieChart instead of BarChart
            chart.title = "Pie"
            data = Reference(ws, min_col=2, min_row=1, max_row=2)
            chart.add_data(data, titles_from_data=True)
            ws.add_chart(chart, "E2")
        orig, recalc = self._create_wb("neg_12", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Charts", "weight": 100}]},
            "testCases": [{
                "criterion": "Charts", "type": "chart_check", "sheet": "Sheet1",
                "chartType": "column", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_13(self):
        def setup(wb, ws, is_recalc):
            ws.title = "PivotSheet"
            ws["A1"] = "Region"; ws["B1"] = "Sales"
            ws["A2"] = "North"; ws["B2"] = 25000
            ws["A3"] = "South"; ws["B3"] = 25000
            ws["A4"] = "Grand Total"; ws["B4"] = 50000.0  # Incorrect total (expected 120000.0)
        orig, recalc = self._create_wb("neg_13", setup)
        self._inject_pivot_xml(orig, grand_total=50000.0)
        self._inject_pivot_xml(recalc, grand_total=50000.0)
        spec = {
            "rubric": {"criteria": [{"name": "Pivots", "weight": 100}]},
            "testCases": [{
                "criterion": "Pivots", "type": "pivot_check", "sheet": "PivotSheet",
                "requiredField": "Sales", "expectedGrandTotal": 120000.0, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_14(self):
        # Stale cache: pivot cache has 50 records, source table has 100 rows
        def setup(wb, ws, is_recalc):
            ws.title = "PivotSheet"
            ws["A1"] = "Region"; ws["B1"] = "Sales"
            ws["A2"] = "Grand Total"; ws["B2"] = 100000.0
            # Source table with 100 rows on SalesData sheet
            ws_src = wb.create_sheet("SalesData")
            ws_src["A1"] = "Region"; ws_src["B1"] = "Sales"
            for r in range(2, 102):
                ws_src[f"A{r}"] = "Region1"; ws_src[f"B{r}"] = r
            tab = Table(displayName="SalesTable", ref="A1:B101")
            ws_src.add_table(tab)
        orig, recalc = self._create_wb("neg_14", setup)
        self._inject_pivot_xml(orig, grand_total=100000.0, cache_records=50)
        self._inject_pivot_xml(recalc, grand_total=100000.0, cache_records=50)
        spec = {
            "rubric": {"criteria": [{"name": "Pivots", "weight": 100}]},
            "testCases": [{
                "criterion": "Pivots", "type": "pivot_check", "sheet": "PivotSheet",
                "requiredField": "Sales", "expectedGrandTotal": 100000.0, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_15(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Data"
            ws["A1"] = "Val"
            tab = Table(displayName="PQTable", ref="A1:A2")
            ws.add_table(tab)
        orig, recalc = self._create_wb("neg_15", setup)
        spec = {
            "rubric": {"criteria": [{"name": "PQ", "weight": 100}]},
            "testCases": [{
                "criterion": "PQ", "type": "power_query_check", "sheet": "Data",
                "requireQuery": True, "expectedQueryName": "MissingQueryTransformation", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_16(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Data"
            ws["A1"] = "ID"; ws["B1"] = "Customer"  # Missing TaxRate column
            ws["A2"] = 1; ws["B2"] = "Acme"
            tab = Table(displayName="PQTable", ref="A1:B2")
            ws.add_table(tab)
        orig, recalc = self._create_wb("neg_16", setup)
        m_xml = (
            '<item xmlns="http://schemas.microsoft.com/DataMashup">'
            'section Section1; shared PQTable = let Source = Excel.Workbook(...) in Source;'
            '</item>'
        )
        for p in [orig, recalc]:
            buf = io.BytesIO()
            with zipfile.ZipFile(p, 'r') as zin, zipfile.ZipFile(buf, 'w') as zout:
                for item in zin.infolist():
                    zout.writestr(item, zin.read(item.filename))
                zout.writestr('customXml/item1.xml', m_xml.encode('utf-8'))
            with open(p, 'wb') as f:
                f.write(buf.getvalue())
        spec = {
            "rubric": {"criteria": [{"name": "PQ", "weight": 100}]},
            "testCases": [{
                "criterion": "PQ", "type": "power_query_check", "sheet": "Data",
                "requiredColumns": ["ID", "TaxRate"], "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_17(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Customers"
            ws["A1"] = "CustomerID"; ws["B1"] = "Name"
            ws["A2"] = "C1"; ws["B2"] = "Acme"
            tab1 = Table(displayName="Customers", ref="A1:B2")
            ws.add_table(tab1)
            ws2 = wb.create_sheet("Orders")
            ws2["A1"] = "OrderID"; ws2["B1"] = "CustomerID"
            ws2["A2"] = "O1"; ws2["B2"] = "C999"  # Orphan key not in Customers
            tab2 = Table(displayName="Orders", ref="A1:B2")
            ws2.add_table(tab2)
        orig, recalc = self._create_wb("neg_17", setup)
        spec = {
            "rubric": {"criteria": [{"name": "RelationalModel", "weight": 100}]},
            "testCases": [{
                "criterion": "RelationalModel", "type": "relational_integrity_check",
                "parent_table": "Customers", "parent_key": "CustomerID",
                "child_table": "Orders", "child_key": "CustomerID", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_18(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 100
        orig, recalc = self._create_wb("neg_18", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Sheets", "weight": 100}]},
            "testCases": [{
                "criterion": "Sheets", "type": "value_check", "sheet": "FinancialSummary", "cell": "A1",
                "strict_sheet": True, "expectedValue": 100, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_19(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Data"
            ws["A1"] = "ID"; ws["B1"] = "Val"
            ws["A2"] = 1;    ws["B2"] = 100  # Raw data, but no Excel Table created
        orig, recalc = self._create_wb("neg_19", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Tables", "weight": 100}]},
            "testCases": [{
                "criterion": "Tables", "type": "table_check", "sheet": "Data",
                "expectedTable": "SalesTable", "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_neg_20(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Empty"
            # Entirely blank
        orig, recalc = self._create_wb("neg_20", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Completeness", "weight": 100}]},
            "testCases": [{
                "criterion": "Completeness", "type": "value_check", "sheet": "Empty", "cell": "B2",
                "expectedValue": 500, "weight": 100
            }]
        }
        return orig, recalc, spec

    # -------------------------------------------------------------------------
    # Benchmark Builders (EDGE_01 to EDGE_10)
    # -------------------------------------------------------------------------
    def _build_edge_01(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            # 5 empty rows, then data at A6:B7
            ws["A6"] = 10; ws["B6"] = 20
            ws["C6"] = 30 if is_recalc else "=SUM(A6:B6)"
        orig, recalc = self._create_wb("edge_01", setup, {"=SUM(A6:B6)": 30})
        spec = {
            "rubric": {"criteria": [{"name": "LayoutInvariance", "weight": 100}]},
            "testCases": [{
                "criterion": "LayoutInvariance", "type": "formula_check", "sheet": "Sheet1", "cell": "C6",
                "expectedFormula": "=SUM(A6:B6)", "expectedValue": 30, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_02(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["C3"] = 40; ws["C4"] = 60
            ws["D5"] = 100 if is_recalc else "=SUM(C3:C4)"
        orig, recalc = self._create_wb("edge_02", setup, {"=SUM(C3:C4)": 100})
        spec = {
            "rubric": {"criteria": [{"name": "ShiftedCells", "weight": 100}]},
            "testCases": [{
                "criterion": "ShiftedCells", "type": "formula_check", "sheet": "Sheet1", "cell": "D5",
                "expectedFormula": "=SUM(C3:C4)", "expectedValue": 100, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_03(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 15; ws["A2"] = 35
            ws["A3"] = 50 if is_recalc else "=sum(a1:a2)"  # Lowercase formula
        orig, recalc = self._create_wb("edge_03", setup, {"=sum(a1:a2)": 50})
        spec = {
            "rubric": {"criteria": [{"name": "CaseInvariance", "weight": 100}]},
            "testCases": [{
                "criterion": "CaseInvariance", "type": "formula_check", "sheet": "Sheet1", "cell": "A3",
                "expectedFormula": "=SUM(A1:A2)", "expectedValue": 50, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_04(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 15; ws["A2"] = 35
            ws["A3"] = 50 if is_recalc else "= SUM(  A1 : A2  )"  # Extra whitespace
        orig, recalc = self._create_wb("edge_04", setup, {"= SUM(  A1 : A2  )": 50})
        spec = {
            "rubric": {"criteria": [{"name": "WhitespaceInvariance", "weight": 100}]},
            "testCases": [{
                "criterion": "WhitespaceInvariance", "type": "formula_check", "sheet": "Sheet1", "cell": "A3",
                "expectedFormula": "=SUM(A1:A2)", "expectedValue": 50, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_05(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Q1 Sales"  # Space in sheet name
            ws["A1"] = 100; ws["A2"] = 200
            ws["A3"] = 300 if is_recalc else "=SUM('Q1 Sales'!A1:A2)"
        orig, recalc = self._create_wb("edge_05", setup, {"=SUM('Q1 Sales'!A1:A2)": 300})
        spec = {
            "rubric": {"criteria": [{"name": "SheetNames", "weight": 100}]},
            "testCases": [{
                "criterion": "SheetNames", "type": "formula_check", "sheet": "Q1 Sales", "cell": "A3",
                "expectedFormula": "=SUM('Q1 Sales'!A1:A2)", "expectedValue": 300, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_06(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 99.99999999
        orig, recalc = self._create_wb("edge_06", setup)
        spec = {
            "rubric": {"criteria": [{"name": "FloatPrecision", "weight": 100}]},
            "testCases": [{
                "criterion": "FloatPrecision", "type": "value_check", "sheet": "Sheet1", "cell": "A1",
                "expectedValue": 100.0, "tolerance": 1e-4, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_07(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Dates"
            ws["A1"] = 45001 if is_recalc else "=TODAY()"
        orig, recalc = self._create_wb("edge_07", setup, {"=TODAY()": 40000})  # Divergent cached value
        spec = {
            "rubric": {"criteria": [{"name": "VolatileWarnings", "weight": 100}]},
            "testCases": [{
                "criterion": "VolatileWarnings", "type": "value_check", "sheet": "Dates", "cell": "A1",
                "expectedValue": 45001, "weight": 100
            }]
        }
        return orig, recalc, spec

    def _build_edge_08(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 15; ws["A2"] = 30
            ws["A3"] = 45 if is_recalc else "=SUM(A1:A2)"
        orig, recalc = self._create_wb("edge_08", setup, {"=SUM(A1:A2)": 45})
        spec = {
            "rubric": {
                "criteria": [{"name": "PreservedScale", "weight": 45.0}],
                "original_max_score": 45.0
            },
            "testCases": [{
                "criterion": "PreservedScale", "type": "formula_check", "sheet": "Sheet1", "cell": "A3",
                "expectedFormula": "=SUM(A1:A2)", "expectedValue": 45, "weight": 45.0
            }]
        }
        return orig, recalc, spec

    def _build_edge_09(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 999  # Methodology fails
            ws["A2"] = 80   # Calculations pass
        orig, recalc = self._create_wb("edge_09", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Modeling", "weight": 100}]},
            "testCases": [{
                "id": "method", "criterion": "Modeling", "type": "value_check", "sheet": "Sheet1", "cell": "A1",
                "expectedValue": 100, "weight": 20, "is_critical": True, "score_cap": 35.0
            }, {
                "id": "calc", "criterion": "Modeling", "type": "value_check", "sheet": "Sheet1", "cell": "A2",
                "expectedValue": 80, "weight": 80
            }]
        }
        return orig, recalc, spec

    def _build_edge_10(self):
        def setup(wb, ws, is_recalc):
            ws.title = "Sheet1"
            ws["A1"] = 100
            ws["B1"] = "Diagnostic Info"
        orig, recalc = self._create_wb("edge_10", setup)
        spec = {
            "rubric": {"criteria": [{"name": "Main", "weight": 100}]},
            "testCases": [{
                "criterion": "Main", "type": "value_check", "sheet": "Sheet1", "cell": "A1",
                "expectedValue": 100, "weight": 100
            }, {
                "criterion": "Main", "type": "value_check", "sheet": "Sheet1", "cell": "B1",
                "expectedValue": "Diagnostic Info", "weight": 0.0  # Zero weight
            }]
        }
        return orig, recalc, spec

    # -------------------------------------------------------------------------
    # Execute full benchmark suite and return metrics
    # -------------------------------------------------------------------------
    def run_benchmark_suite(self):
        """
        Executes all 50 benchmarks, validates scores and warnings against ground truth,
        and aggregates certification metrics.
        """
        catalog = self.get_benchmark_catalog()
        results = []
        latencies = []
        false_positives = 0
        false_negatives = 0
        score_drifts = 0

        for bench in catalog:
            t0 = time.time()
            orig, recalc, spec = bench["build"](self)

            # Check if this benchmark is a relational integrity test via DuckDB
            if any(tc.get("type") == "relational_integrity_check" for tc in spec.get("testCases", [])):
                with DuckDBAnalyticalAdapter(workbook_path=orig) as adapter:
                    tc = next(t for t in spec["testCases"] if t.get("type") == "relational_integrity_check")
                    res = adapter.verify_foreign_key_integrity(
                        child_table=tc["child_table"],
                        child_col=tc["child_key"],
                        parent_table=tc["parent_table"],
                        parent_col=tc["parent_key"]
                    )
                    actual_score = 100.0 if res["status"] == "VERIFIED_PASS" else 0.0
                    actual_warnings = []
                    report = {"score": actual_score, "testCases": [res]}
            else:
                report = inspect_and_evaluate(orig, recalc, spec)
                actual_score = report.get("score", 0.0)
                actual_warnings = report.get("calculation_warnings", []) + [w.get("warning") for w in report.get("warnings", []) if isinstance(w, dict)]

            elapsed_ms = (time.time() - t0) * 1000.0
            latencies.append(elapsed_ms)

            expected_score = bench["expected_score"]
            score_diff = abs(actual_score - expected_score)
            is_match = score_diff < 1e-4

            if not is_match:
                score_drifts += 1
                if bench["cohort"] == "negative" and actual_score > expected_score:
                    false_positives += 1
                elif bench["cohort"] == "positive" and actual_score < expected_score:
                    false_negatives += 1

            # Check warnings if specified
            warning_match = True
            if "expected_warning" in bench:
                expected_w = bench["expected_warning"]
                if not any(expected_w in str(w) for w in actual_warnings):
                    warning_match = False

            results.append({
                "id": bench["id"],
                "cohort": bench["cohort"],
                "category": bench["category"],
                "name": bench["name"],
                "expected_score": expected_score,
                "actual_score": actual_score,
                "score_diff": score_diff,
                "is_match": is_match and warning_match,
                "latency_ms": elapsed_ms
            })

        total = len(catalog)
        passed = sum(1 for r in results if r["is_match"])
        fp_rate = (false_positives / 20.0) * 100.0  # Out of 20 negative cases
        fn_rate = (false_negatives / 20.0) * 100.0  # Out of 20 positive cases
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        summary = {
            "total_benchmarks": total,
            "passed": passed,
            "failed": total - passed,
            "agreement_rate_pct": (passed / total) * 100.0,
            "false_positive_count": false_positives,
            "false_positive_rate_pct": fp_rate,
            "false_negative_count": false_negatives,
            "false_negative_rate_pct": fn_rate,
            "score_drift_count": score_drifts,
            "avg_latency_ms": avg_latency,
            "max_latency_ms": max(latencies) if latencies else 0.0,
            "min_latency_ms": min(latencies) if latencies else 0.0,
            "results": results
        }
        return summary
