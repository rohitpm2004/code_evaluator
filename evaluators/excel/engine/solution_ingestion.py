"""
Teacher Solution Ingestion Engine & Canonical Rubric Extractor (Sprint 4)

Transforms teacher inputs (assignment instructions, rubric criteria, and reference
solution workbook) into a deterministic, versioned, and immutable Canonical Assignment Manifest.

Key Guarantees:
1. Dynamic & Anchor-Based: Never hardcodes fragile cell positions when column anchors exist.
2. Alternative-Aware: Auto-expands accepted formula functions (XLOOKUP <-> INDEX/MATCH).
3. Deterministic: Pure deterministic generation for reproducibility; zero AI mark-guessing.
4. Cryptographically Versioned: Emits SHA-256 manifest hash and immutable schema version.
"""

import os
import re
import sys
import json
import hashlib
import datetime
from typing import Dict, Any, List, Optional, Tuple

import openpyxl
from openpyxl.utils import get_column_letter

# Ensure engine path is available for imports
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from workbook_registry import WorkbookObjectRegistry
from excel_runner import parse_number, FUNCTION_EQUIVALENTS


class TeacherSolutionIngestionEngine:
    """Ingests teacher reference solution workbooks and extracts canonical evaluation manifests."""

    def __init__(self):
        pass

    def ingest(
        self,
        reference_workbook_path: str,
        rubric: Any,
        instructions: Optional[str] = None,
        assignment_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Main ingestion pipeline: extracts features, maps criteria, and builds canonical manifest."""
        if not os.path.exists(reference_workbook_path):
            raise FileNotFoundError(f"Reference workbook not found: {reference_workbook_path}")

        # 1. Normalize rubric criteria
        normalized_criteria = self._normalize_rubric_criteria(rubric)

        # 2. Extract deep workbook features via WorkbookObjectRegistry
        features = self.extract_reference_features(reference_workbook_path)

        # 3. Generate canonical assertions matching rubric criteria to discovered features
        test_cases = self._generate_canonical_assertions(normalized_criteria, features, instructions)

        # 4. Normalize weights so rubric sums to exactly 100.0
        normalized_criteria = self._normalize_weights(normalized_criteria)

        # 5. Compute cryptographic manifest hash
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest_payload = {
            "assignment_id": assignment_id or f"asgn_{hashlib.md5(reference_workbook_path.encode()).hexdigest()[:8]}",
            "version": "1.0.0",
            "created_at": created_at,
            "status": "PROPOSED",
            "rubric": normalized_criteria,
            "testCases": test_cases,
            "supported_features": features.get("supported_features", []),
            "unsupported_features": features.get("unsupported_features", [])
        }

        # Canonical hash excludes volatile timestamps
        canonical_str = json.dumps({
            "rubric": normalized_criteria,
            "testCases": test_cases
        }, sort_keys=True)
        manifest_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

        manifest_payload["manifest_hash"] = manifest_hash
        return manifest_payload

    def extract_reference_features(self, reference_workbook_path: str) -> Dict[str, Any]:
        """Extracts structural, formula, table, pivot, and chart features from reference workbook."""
        registry = WorkbookObjectRegistry(reference_workbook_path)

        wb_formula = openpyxl.load_workbook(reference_workbook_path, data_only=False)
        wb_cached = openpyxl.load_workbook(reference_workbook_path, data_only=True)

        formula_columns = []
        worksheets_meta = []
        supported_features = ["data_tables", "value_matching", "triple_state_formulas"]
        unsupported_features = []

        # Analyze sheets and formula columns
        for s_name in wb_formula.sheetnames:
            ws_f = wb_formula[s_name]
            ws_c = wb_cached[s_name]

            max_r = min(ws_f.max_row or 100, 200)
            max_c = min(ws_f.max_column or 30, 40)

            # Detect header row
            header_row = 1
            headers = {}
            for c in range(1, max_c + 1):
                val = ws_f.cell(row=1, column=c).value
                if val is not None and str(val).strip():
                    headers[c] = str(val).strip()

            # Scan columns for student/teacher formulas
            for col_idx in range(1, max_c + 1):
                col_formulas = []
                col_cells = []
                for r in range(header_row + 1, max_r + 1):
                    val = ws_f.cell(row=r, column=col_idx).value
                    val_str = str(val or "").strip()
                    if val_str.startswith("="):
                        col_formulas.append(val_str)
                        col_cells.append(f"{get_column_letter(col_idx)}{r}")

                if col_formulas:
                    funcs_used = set()
                    for f in col_formulas:
                        for fn in re.findall(r'([A-Z_]{2,})\s*\(', f.upper()):
                            funcs_used.add(fn)
                        if any(op in f for op in ['*', '+', '-', '/', '^']) and re.search(r'[A-Z]+[0-9]+', f.upper()):
                            funcs_used.add("CELL_ARITHMETIC")

                    header_name = headers.get(col_idx, f"Column {get_column_letter(col_idx)}")
                    start_cell = col_cells[0]
                    end_cell = col_cells[-1]

                    # Auto-expand accepted alternative functions
                    accepted = set(funcs_used)
                    for fn in list(funcs_used):
                        if fn in FUNCTION_EQUIVALENTS:
                            accepted.update(FUNCTION_EQUIVALENTS[fn])

                    # Sample expected values for assertion
                    sample_cell = start_cell
                    sample_r = int(re.search(r'[0-9]+', sample_cell).group(0))
                    sample_exp_val = ws_c.cell(row=sample_r, column=col_idx).value

                    formula_columns.append({
                        "sheet": s_name,
                        "header": header_name,
                        "column_letter": get_column_letter(col_idx),
                        "range": f"{start_cell}:{end_cell}",
                        "formula_sample": col_formulas[0],
                        "functions_used": sorted(list(funcs_used)),
                        "accepted_functions": sorted(list(accepted)),
                        "sample_cell": sample_cell,
                        "sample_expected_value": parse_number(sample_exp_val) if parse_number(sample_exp_val) is not None else sample_exp_val
                    })

        # Enrich charts with rendered sample values
        enriched_charts = []
        for ch in registry.charts:
            ch_copy = dict(ch)
            rendered_vals = []
            for ref in ch_copy.get("num_refs", []):
                ref_sh = ch_copy.get("sheet", wb_formula.sheetnames[0])
                ref_rng = ref
                if "!" in ref:
                    ref_sh_raw, ref_rng = ref.split("!", 1)
                    ref_sh = ref_sh_raw.replace("'", "")
                if ref_sh in wb_cached.sheetnames:
                    try:
                        clean_rng = ref_rng.replace("$", "")
                        cells = wb_cached[ref_sh][clean_rng]
                        if not isinstance(cells, tuple):
                            cells = ((cells,),)
                        elif cells and not isinstance(cells[0], tuple):
                            cells = (cells,)
                        for row in cells:
                            for cell in row:
                                nv = parse_number(cell.value)
                                if nv is not None:
                                    rendered_vals.append(nv)
                    except Exception:
                        pass
            ch_copy["rendered_values"] = rendered_vals[:10]
            enriched_charts.append(ch_copy)
            supported_features.append(f"chart_{ch_copy.get('type', 'generic').lower()}")

        # Enrich pivot tables with rendered Grand Total
        enriched_pivots = []
        for p in registry.pivot_tables:
            p_copy = dict(p)
            p_sheet = p_copy.get("sheet", "")
            loc_ref = p_copy.get("location_ref", "")
            grand_total_val = None

            if p_sheet in wb_cached.sheetnames and loc_ref and ":" in loc_ref:
                sh_c = wb_cached[p_sheet]
                try:
                    s_c, s_r, e_c, e_r = openpyxl.utils.range_boundaries(loc_ref)
                    for r in range(s_r, min(e_r + 5, 200)):
                        for c in range(s_c, min(e_c + 5, 50)):
                            cv = str(sh_c.cell(row=r, column=c).value or "").lower()
                            if "total" in cv:
                                for cand_c in range(s_c, min(e_c + 5, 50)):
                                    cand_v = parse_number(sh_c.cell(row=r, column=cand_c).value)
                                    if cand_v is not None:
                                        grand_total_val = cand_v
                                        break
                except Exception:
                    pass

            p_copy["grand_total"] = grand_total_val
            enriched_pivots.append(p_copy)
            supported_features.append("pivot_tables")

        if registry.power_queries:
            supported_features.append("power_query_m_code")

        wb_formula.close()
        wb_cached.close()
        registry.close()

        return {
            "worksheets": list(registry.worksheets.keys()),
            "tables": registry.tables,
            "charts": enriched_charts,
            "pivot_tables": enriched_pivots,
            "power_queries": registry.power_queries,
            "formula_columns": formula_columns,
            "supported_features": list(dict.fromkeys(supported_features)),
            "unsupported_features": list(dict.fromkeys(unsupported_features))
        }

    def _normalize_rubric_criteria(self, rubric: Any) -> List[Dict[str, Any]]:
        """Normalizes teacher rubric input into structured criteria list."""
        if isinstance(rubric, dict):
            raw_criteria = rubric.get("criteria", [])
        elif isinstance(rubric, list):
            raw_criteria = rubric
        else:
            raw_criteria = [{"name": "Standard Evaluation", "weight": 100}]

        criteria = []
        for idx, crit in enumerate(raw_criteria):
            if isinstance(crit, str):
                crit = {"name": crit, "weight": 100.0 / len(raw_criteria)}
            name = crit.get("name", f"Criterion {idx + 1}")
            weight = float(crit.get("weight", 10.0))
            desc = crit.get("description", crit.get("desc", ""))
            criteria.append({
                "name": name,
                "weight": weight,
                "description": desc
            })
        return criteria

    def _normalize_weights(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensures criteria weights sum to exactly 100.0."""
        tot = sum(float(c.get("weight", 0)) for c in criteria)
        if tot <= 0:
            for c in criteria:
                c["weight"] = round(100.0 / max(len(criteria), 1), 2)
        elif abs(tot - 100.0) > 0.01:
            factor = 100.0 / tot
            for c in criteria:
                c["weight"] = round(float(c.get("weight", 0)) * factor, 2)
        return criteria

    def _generate_canonical_assertions(
        self,
        criteria: List[Dict[str, Any]],
        features: Dict[str, Any],
        instructions: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Generates deterministic assertions mapped to excel_runner check handlers."""
        test_cases = []
        matched_formula_cols = set()
        matched_charts = set()
        matched_pivots = set()
        matched_pqs = set()

        for crit in criteria:
            crit_name = crit["name"]
            crit_text = f"{crit_name} {crit.get('description', '')}".lower()

            # 1. Chart / Visualization Mapping
            if any(kw in crit_text for kw in ["chart", "graph", "plot", "column chart", "bar chart", "visual"]):
                chart_match = None
                for ch in features.get("charts", []):
                    ch_key = (ch.get("type"), ch.get("title"))
                    if ch_key not in matched_charts:
                        chart_match = ch
                        matched_charts.add(ch_key)
                        break

                if chart_match:
                    tc = {
                        "criterion": crit_name,
                        "type": "chart_check",
                        "chartType": chart_match.get("type", "Chart"),
                        "title": chart_match.get("title", ""),
                        "expectedSourceRange": chart_match.get("num_refs", [None])[0] if chart_match.get("num_refs") else None,
                        "expectedValues": chart_match.get("rendered_values") if chart_match.get("rendered_values") else None,
                        "weight": 1.0
                    }
                    test_cases.append(tc)
                    continue

            # 2. Pivot Table Mapping
            if any(kw in crit_text for kw in ["pivot", "pivottable", "summary table", "aggregation"]):
                pivot_match = None
                for p in features.get("pivot_tables", []):
                    p_key = p.get("name")
                    if p_key not in matched_pivots:
                        pivot_match = p
                        matched_pivots.add(p_key)
                        break

                if pivot_match:
                    req_field = ""
                    if pivot_match.get("data_fields"):
                        req_field = pivot_match["data_fields"][0].get("name", "")
                    elif pivot_match.get("fields"):
                        req_field = pivot_match["fields"][0]

                    tc = {
                        "criterion": crit_name,
                        "type": "pivot_check",
                        "sheet": pivot_match.get("sheet", ""),
                        "requiredField": req_field,
                        "expectedGrandTotal": pivot_match.get("grand_total"),
                        "weight": 1.0
                    }
                    test_cases.append(tc)
                    continue

            # 3. Power Query Mapping
            if any(kw in crit_text for kw in ["power query", "query", "etl", "m-code", "clean data", "import"]):
                pq_match = None
                for pq in features.get("power_queries", []):
                    pq_key = pq.get("query_name")
                    if pq_key not in matched_pqs:
                        pq_match = pq
                        matched_pqs.add(pq_key)
                        break

                if pq_match:
                    # Find loaded destination table columns
                    table_cols = []
                    for t in features.get("tables", []):
                        if t.get("name", "").lower() == pq_match["query_name"].lower() or pq_match["query_name"].lower() in t.get("name", "").lower():
                            table_cols = t.get("columns", [])
                            break

                    tc = {
                        "criterion": crit_name,
                        "type": "power_query_check",
                        "expectedQueryName": pq_match["query_name"],
                        "requiredColumns": table_cols[:5] if table_cols else None,
                        "minRowCount": 1,
                        "weight": 1.0
                    }
                    test_cases.append(tc)
                    continue

            # 4. Formula Column Mapping
            matched_col = None
            # Match by column header text overlap
            for fc in features.get("formula_columns", []):
                h_name = fc["header"].lower()
                if fc["range"] not in matched_formula_cols and (h_name in crit_text or any(token in crit_text for token in h_name.split() if len(token) > 3)):
                    matched_col = fc
                    matched_formula_cols.add(fc["range"])
                    break

            if not matched_col and features.get("formula_columns"):
                # Fallback to next unused formula column
                for fc in features.get("formula_columns", []):
                    if fc["range"] not in matched_formula_cols:
                        matched_col = fc
                        matched_formula_cols.add(fc["range"])
                        break

            if matched_col:
                # Primary formula structure check
                tc_formula = {
                    "criterion": crit_name,
                    "type": "formula_check",
                    "range": matched_col["range"],
                    "columnAnchor": matched_col["header"],
                    "acceptedFunctions": matched_col["accepted_functions"],
                    "mustBeFormula": True,
                    "forbiddenConstants": True,
                    "weight": 0.7
                }
                # Verification of recalculated sample output value
                tc_value = {
                    "criterion": crit_name,
                    "type": "value_check",
                    "sheet": matched_col["sheet"],
                    "cell": matched_col["sample_cell"],
                    "expectedValue": matched_col["sample_expected_value"],
                    "weight": 0.3
                }
                test_cases.append(tc_formula)
                test_cases.append(tc_value)
                continue

            # 5. Default Table / Data Verification
            if features.get("tables"):
                tbl = features["tables"][0]
                tc_data = {
                    "criterion": crit_name,
                    "type": "data_check",
                    "columnAnchor": tbl.get("columns", ["Data"])[0],
                    "weight": 1.0
                }
                test_cases.append(tc_data)
            else:
                tc_fallback = {
                    "criterion": crit_name,
                    "type": "data_check",
                    "range": "A2:B10",
                    "weight": 1.0
                }
                test_cases.append(tc_fallback)

        return test_cases


def ingest_teacher_solution(
    reference_workbook_path: str,
    rubric: Any,
    instructions: Optional[str] = None,
    assignment_id: Optional[str] = None
) -> Dict[str, Any]:
    """Convenience helper to ingest teacher reference files and return canonical manifest."""
    engine = TeacherSolutionIngestionEngine()
    return engine.ingest(reference_workbook_path, rubric, instructions, assignment_id)
