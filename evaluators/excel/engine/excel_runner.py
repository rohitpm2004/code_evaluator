#!/usr/bin/env python3
"""
Dual-Workbook Assertion Inspector & Scoring Runner
Part of Central Evaluator - excelWorker Engine Layer

Responsibilities:
1. Loads original workbook with openpyxl(data_only=False) to inspect AST formula syntax,
   references, and anti-cheat static literal violations.
2. Loads recalculated workbook with openpyxl(data_only=True) to read verified calculated values.
3. Implements dual tolerance: |actual - expected| <= max(abs_tol, rel_tol * |expected|).
4. Implements anchor search with disambiguation guards (Explicit -> Exact -> Offset -> REVIEW).
5. Maps test assertions deterministically to rubric weights.
6. Emits structured JSON summary with strict evaluation_status.
"""

import sys
import os
import json
import math
import re
import argparse
import zipfile
import xml.etree.ElementTree as ET
import shutil
import tempfile
from typing import Dict, Any, List, Optional, Tuple

try:
    from recalculate import recalculate_via_cli
except ImportError:
    try:
        from engine.recalculate import recalculate_via_cli
    except ImportError:
        def recalculate_via_cli(in_f, out_f, prof_d, timeout_sec=20):
            shutil.copy2(in_f, out_f)
            return False, "recalculate_via_cli not available"

try:
    import openpyxl
    from openpyxl.utils import coordinate_to_tuple, get_column_letter
except ImportError:
    print(json.dumps({
        "success": False,
        "error": "openpyxl is not installed. Please install openpyxl: pip install openpyxl"
    }))
    sys.exit(1)

def parse_number(val: Any) -> Optional[float]:
    """Extracts a float from numbers, formatted strings ($1,234.50, 13.3%, etc.)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return None
        return float(val)
    if isinstance(val, str):
        cleaned = val.strip().replace("$", "").replace(",", "")
        if cleaned.endswith("%"):
            try:
                return float(cleaned[:-1]) / 100.0
            except ValueError:
                return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None

def check_dual_tolerance(actual: float, expected: float, abs_tol: float = 0.05, rel_tol: float = 0.001) -> bool:
    """Evaluates dual tolerance: |actual - expected| <= max(abs_tol, rel_tol * |expected|)."""
    diff = abs(actual - expected)
    threshold = max(abs_tol, rel_tol * abs(expected))
    return diff <= threshold

def is_hardcoded_cheat(formula_str: str, defined_names: Optional[set] = None) -> bool:
    """
    Checks if a formula is actually a cheat/static literal:
    e.g., '=128450', '="128450"', '=100000+28450' without any cell/range references or functions.
    Permits:
    - Standard functions: =SUM(A1:A5), =DATE(2024, 1, 1)
    - Zero-arg functions: =TODAY(), =NOW(), =PI(), =RAND()
    - Named ranges / defined names: e.g. =Tax_Rate * Subtotal
    """
    if not isinstance(formula_str, str) or not formula_str.startswith("="):
        return True # Not even a formula
    
    expr = formula_str[1:].strip()
    
    # Pure numeric or string literal: e.g. =12345 or ="hello"
    if re.fullmatch(r'[-+]?\d+(\.\d+)?', expr):
        return True
    if re.fullmatch(r'["\'].*["\']', expr):
        return True

    # Permit standard zero-argument functions and boolean constants
    zero_arg_functions = r'^(TODAY|NOW|PI|RAND|RANDBETWEEN|TRUE|FALSE)\s*\('
    if re.search(zero_arg_functions, expr, re.IGNORECASE):
        return False

    # Check for cell references like A1, $B$12, Sheet!C4
    cell_ref_pattern = r'(\$?[A-Za-z]{1,3}\$?[0-9]{1,7})'
    has_cell_ref = bool(re.search(cell_ref_pattern, expr))

    # Check for any valid Excel function call: e.g. SUM(, DATE(, IF(
    has_function_call = bool(re.search(r'[A-Za-z_][A-Za-z0-9_\.]*\s*\(', expr))

    # Check for defined names / named ranges if provided
    has_named_range = False
    if defined_names:
        for name in defined_names:
            if re.search(r'\b' + re.escape(name) + r'\b', expr, re.IGNORECASE):
                has_named_range = True
                break

    if not has_cell_ref and not has_function_call and not has_named_range:
        # A formula with no cell references, no function calls, and no named ranges is hardcoded math, e.g. =100+200
        return True

    return False

def find_anchor_cell(sheet, anchor_text: str, max_rows: int = 200, max_cols: int = 50) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """
    Searches for an anchor cell matching anchor_text (trimmed, case-insensitive).
    Returns ( (row, col), None ) on unique match.
    Returns ( None, error_reason ) if missing or ambiguous (multiple matches).
    """
    target = anchor_text.strip().lower()
    matches = []

    for r in range(1, min(sheet.max_row or 100, max_rows) + 1):
        for c in range(1, min(sheet.max_column or 50, max_cols) + 1):
            cell_val = sheet.cell(row=r, column=c).value
            if cell_val is not None and isinstance(cell_val, str):
                if cell_val.strip().lower() == target:
                    matches.append((r, c))

    if len(matches) == 0:
        return None, f"Anchor label '{anchor_text}' not found in sheet."
    if len(matches) > 1:
        match_addrs = [f"{get_column_letter(c)}{r}" for r, c in matches]
        return None, f"Ambiguous anchor: '{anchor_text}' found {len(matches)} times at {', '.join(match_addrs)}. Flagged for review."

    return matches[0], None

def get_offset_cell(base_row: int, base_col: int, direction: str = "right", offset: int = 1) -> Tuple[int, int]:
    """Computes target row/col given direction and offset."""
    d = direction.lower()
    if d == "right":
        return base_row, base_col + offset
    elif d == "left":
        return base_row, max(1, base_col - offset)
    elif d == "below" or d == "down":
        return base_row + offset, base_col
    elif d == "above" or d == "up":
        return max(1, base_row - offset), base_col
    return base_row, base_col + offset

def resolve_sheet(wb, target_name: Optional[str] = None, anchor_hint: Optional[str] = None) -> Optional[str]:
    """
    Intelligent 4-tier fuzzy sheet name resolver:
    1. Exact Match
    2. Normalized Match (trimmed, case-insensitive)
    3. Substring Match
    4. Unique Anchor Scan
    Fallback: first sheet in workbook
    """
    if not wb.sheetnames:
        return None

    if not target_name or not str(target_name).strip():
        if anchor_hint:
            anchor_matches = []
            for s in wb.sheetnames:
                coords, _ = find_anchor_cell(wb[s], anchor_hint)
                if coords:
                    anchor_matches.append(s)
            if len(anchor_matches) == 1:
                return anchor_matches[0]
        return wb.sheetnames[0]

    raw_target = str(target_name).strip()
    clean_target = raw_target.lower()

    # Tier 1: Exact
    if raw_target in wb.sheetnames:
        return raw_target

    # Tier 2: Normalized
    for s in wb.sheetnames:
        if s.strip().lower() == clean_target:
            return s

    # Tier 3: Substring
    for s in wb.sheetnames:
        s_clean = s.strip().lower()
        if clean_target in s_clean or s_clean in clean_target:
            return s

    # Tier 4: Content Anchor Hint
    if anchor_hint:
        anchor_matches = []
        for s in wb.sheetnames:
            coords, _ = find_anchor_cell(wb[s], anchor_hint)
            if coords:
                anchor_matches.append(s)
        if len(anchor_matches) == 1:
            return anchor_matches[0]

    # Fallback
    return wb.sheetnames[0]

def inspect_openxml_pivots(xlsx_path: str) -> List[Dict[str, Any]]:
    """Inspects native Excel Pivot Tables and PivotCaches from OpenXML package."""
    pivots = []
    try:
        with zipfile.ZipFile(xlsx_path, 'r') as z:
            for fname in z.namelist():
                if fname.startswith('xl/pivotTables/pivotTable') and fname.endswith('.xml'):
                    try:
                        content = z.read(fname)
                        root = ET.fromstring(content)
                        p_name = root.attrib.get('name', '')
                        data_fields = []
                        row_fields = []
                        for elem in root.iter():
                            tag = elem.tag.split('}')[-1]
                            if tag == 'dataField':
                                df_name = elem.attrib.get('name', '')
                                df_sub = elem.attrib.get('subtotal', 'sum')
                                data_fields.append({"name": df_name, "function": df_sub})
                            elif tag == 'pivotField':
                                pf_name = elem.attrib.get('name') or elem.attrib.get('dataField')
                                if pf_name:
                                    row_fields.append(pf_name)
                        pivots.append({
                            "file": fname,
                            "name": p_name,
                            "dataFields": data_fields,
                            "fields": row_fields
                        })
                    except Exception:
                        pass
    except Exception:
        pass
    return pivots

def inspect_openxml_macros_and_queries(xlsx_path: str) -> Dict[str, Any]:
    """Inspects OpenXML archive statically for VBA macros, subroutines, and Power Query connections."""
    info = {
        "has_macros": False,
        "macro_names": [],
        "has_power_query": False,
        "query_names": []
    }
    try:
        with zipfile.ZipFile(xlsx_path, 'r') as z:
            names = z.namelist()
            if any('vbaProject.bin' in n for n in names):
                info["has_macros"] = True
                try:
                    vba_bytes = z.read('xl/vbaProject.bin')
                    matches = re.findall(rb'(?:Sub|Function)\s+([a-zA-Z0-9_]+)', vba_bytes)
                    info["macro_names"] = sorted(list(set([m.decode('latin1', errors='ignore') for m in matches])))
                except Exception:
                    pass

            if any(n.startswith('xl/queryTables/') or 'connections.xml' in n for n in names):
                info["has_power_query"] = True
                for n in names:
                    if 'connections.xml' in n:
                        try:
                            c_bytes = z.read(n)
                            c_root = ET.fromstring(c_bytes)
                            for elem in c_root.iter():
                                if elem.tag.split('}')[-1] == 'connection':
                                    c_name = elem.attrib.get('name')
                                    if c_name:
                                        info["query_names"].append(c_name)
                        except Exception:
                            pass
    except Exception:
        pass
    return info

def inspect_and_evaluate(original_path: str, recalculated_path: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    wb_orig = openpyxl.load_workbook(original_path, data_only=False)
    wb_recalc = openpyxl.load_workbook(recalculated_path, data_only=True)

    defined_name_set = set()
    try:
        if hasattr(wb_orig, 'defined_names') and wb_orig.defined_names:
            for dn in wb_orig.defined_names.definedName:
                if dn.name:
                    defined_name_set.add(dn.name)
    except Exception:
        pass

    rubric = spec.get("rubric", {})
    criteria = rubric.get("criteria", [])
    test_cases = spec.get("testCases", [])

    criterion_weights = {}
    total_rubric_weight = 0
    for crit in criteria:
        name = crit.get("name", "")
        weight = float(crit.get("weight", 0))
        criterion_weights[name] = weight
        total_rubric_weight += weight

    if total_rubric_weight == 0:
        total_rubric_weight = 100

    assertion_results = []
    criterion_passes = {crit.get("name"): {"total": 0, "passed": 0} for crit in criteria}
    detected_formulas = set()
    calculation_warnings = []
    needs_review = False

    default_sheet_name = wb_orig.sheetnames[0]

    # Global inspections for Pivots, Macros, and Power Query
    detected_pivots = inspect_openxml_pivots(original_path)
    macro_info = inspect_openxml_macros_and_queries(original_path)
    detected_charts = []

    # Global formula and content scan across all sheets
    all_formulas = []
    has_any_data = False
    table_blank_cells = []
    total_table_cells = 0
    populated_table_cells = 0

    for s_name in wb_orig.sheetnames:
        sh = wb_orig[s_name]
        min_r, max_r = None, None
        min_c, max_c = None, None

        # Inspect charts on this sheet
        raw_charts = getattr(sh, '_charts', []) or []
        for ch in raw_charts:
            ch_type = type(ch).__name__
            ch_title = ""
            if hasattr(ch, 'title'):
                if isinstance(ch.title, str):
                    ch_title = ch.title
                elif hasattr(ch.title, 'text'):
                    ch_title = str(ch.title.text or '')
                else:
                    ch_title = str(ch.title or '')
            series_count = len(getattr(ch, 'series', []) or [])
            detected_charts.append({
                "sheet": s_name,
                "type": ch_type,
                "title": ch_title.strip(),
                "seriesCount": series_count
            })

        # Find bounds of cells that contain content
        for r in range(1, min(sh.max_row or 100, 200) + 1):
            for c in range(1, min(sh.max_column or 50, 50) + 1):
                val = sh.cell(row=r, column=c).value
                if val is not None and str(val).strip() != "":
                    has_any_data = True
                    min_r = r if min_r is None else min(min_r, r)
                    max_r = r if max_r is None else max(max_r, r)
                    min_c = c if min_c is None else min(min_c, c)
                    max_c = c if max_c is None else max(max_c, c)

                    val_str = str(val).strip()
                    if val_str.startswith("="):
                        clean_f = val_str.upper()
                        all_formulas.append(clean_f)
                        func_matches = re.findall(r'([A-Z_]{2,})\s*\(', clean_f)
                        for fn in func_matches:
                            detected_formulas.add(fn)
                        # Also detect cell arithmetic formulas without function calls e.g. =C2*D2, =E2*0.1
                        if any(op in clean_f for op in ['*', '+', '-', '/', '^']) and re.search(r'[A-Z]+[0-9]+', clean_f):
                            detected_formulas.add("CELL_ARITHMETIC")

        # In any sheet with a tabular data region, check for empty cells inside the populated bounding box
        if min_r is not None and max_r is not None and min_c is not None and max_c is not None:
            if (max_r - min_r >= 1) and (max_c - min_c >= 1):
                for r in range(min_r, max_r + 1):
                    # Check if this row has at least one populated cell
                    row_vals = [sh.cell(row=r, column=c).value for c in range(min_c, max_c + 1)]
                    has_row_val = any(v is not None and str(v).strip() != "" for v in row_vals)
                    if has_row_val:
                        # Summary rows (e.g., Total Revenue in Col A, SUM in Col E) naturally have empty cells in non-summed columns.
                        # Do not falsely flag empty spacer cells in summary/total rows as missing table data.
                        is_summary_row = any(
                            any(kw in str(v or "").lower() for kw in ["total", "sum", "average", "avg", "grand total", "subtotal", "overall", "mean", "min", "max"])
                            for v in row_vals
                        ) or (r == max_r and any(str(v or "").startswith("=") for v in row_vals))

                        if is_summary_row:
                            for c in range(min_c, max_c + 1):
                                v = sh.cell(row=r, column=c).value
                                if v is not None and str(v).strip() != "":
                                    total_table_cells += 1
                                    populated_table_cells += 1
                            continue

                        for c in range(min_c, max_c + 1):
                            total_table_cells += 1
                            v = sh.cell(row=r, column=c).value
                            if v is None or str(v).strip() == "":
                                coord = f"{get_column_letter(c)}{r}"
                                table_blank_cells.append(coord)
                            else:
                                populated_table_cells += 1

    if table_blank_cells:
        calculation_warnings.append(
            f"Table has {len(table_blank_cells)} blank cell(s) in populated rows: {', '.join(table_blank_cells[:5])}"
        )

    for tc in test_cases:
        crit_name = tc.get("criterion", criteria[0].get("name") if criteria else "Standard Evaluation")
        if crit_name not in criterion_passes:
            criterion_passes[crit_name] = {"total": 0, "passed": 0}
        criterion_passes[crit_name]["total"] += 1

        # Use intelligent 4-tier fuzzy sheet name resolver
        target_sheet_req = tc.get("sheet")
        sheet_name = resolve_sheet(wb_orig, target_sheet_req, anchor_hint=tc.get("anchor"))
        if not sheet_name or sheet_name not in wb_orig.sheetnames:
            assertion_results.append({
                "criterion": crit_name,
                "type": tc.get("type"),
                "passed": False,
                "reason": f"Sheet '{target_sheet_req or 'default'}' not found in workbook.",
                "target": target_sheet_req
            })
            continue

        sheet_orig = wb_orig[sheet_name]
        sheet_recalc = wb_recalc[sheet_name]

        tc_type = tc.get("type", "value_check")

        # -------------------------------------------------------------
        # TYPE 1: Value Check (Explicit Cell or Anchor-Based)
        # -------------------------------------------------------------
        if tc_type == "value_check":
            target_cell_coord = None
            if tc.get("cell"):
                target_cell_coord = tc.get("cell")
            elif tc.get("anchor"):
                anchor_coords, err = find_anchor_cell(sheet_orig, tc.get("anchor"))
                if err:
                    calculation_warnings.append(err)
                    needs_review = True
                    assertion_results.append({
                        "criterion": crit_name,
                        "type": tc_type,
                        "passed": False,
                        "reason": err,
                        "anchor": tc.get("anchor")
                    })
                    continue
                r_target, c_target = get_offset_cell(
                    anchor_coords[0], anchor_coords[1],
                    direction=tc.get("direction", "right"),
                    offset=int(tc.get("offset", 1))
                )
                target_cell_coord = f"{get_column_letter(c_target)}{r_target}"

            if not target_cell_coord:
                assertion_results.append({
                    "criterion": crit_name,
                    "type": tc_type,
                    "passed": False,
                    "reason": "Neither explicit cell nor anchor specified in test case."
                })
                continue

            cell_val = sheet_recalc[target_cell_coord].value
            actual_num = parse_number(cell_val)
            expected_num = parse_number(tc.get("expectedValue"))
            abs_tol = float(tc.get("absTolerance", tc.get("tolerance", 0.05)))
            rel_tol = float(tc.get("relTolerance", 0.001))

            passed = False
            reason = ""
            if actual_num is not None and expected_num is not None:
                passed = check_dual_tolerance(actual_num, expected_num, abs_tol, rel_tol)
                if passed:
                    reason = f"Cell {target_cell_coord} matched expected value {expected_num} within tolerance."
                else:
                    reason = f"Cell {target_cell_coord} value {actual_num} did not match expected {expected_num} (diff: {abs(actual_num - expected_num):.4f})."
            else:
                str_actual = str(cell_val).strip() if cell_val is not None else ""
                str_expected = str(tc.get("expectedValue")).strip()
                passed = (str_actual.lower() == str_expected.lower())
                reason = f"String match for {target_cell_coord}: '{str_actual}' vs expected '{str_expected}'."

            if passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "cell": target_cell_coord,
                "actual": cell_val,
                "expected": tc.get("expectedValue"),
                "passed": passed,
                "reason": reason
            })

        # -------------------------------------------------------------
        # TYPE 2: Formula Range Check (Anti-Cheat & Dynamic References)
        # -------------------------------------------------------------
        elif tc_type in ["formula_range_check", "formula_check", "function_check"]:
            range_str = tc.get("range", tc.get("cell", ""))
            accepted_funcs = [f.upper() for f in tc.get("acceptedFunctions", [])]
            forbidden_constants = tc.get("forbiddenConstants", True)
            must_be_formula = tc.get("mustBeFormula", True)

            cells_to_check = []
            if ":" in range_str:
                for row in sheet_orig[range_str]:
                    for cell in row:
                        cells_to_check.append(cell)
            elif range_str:
                cells_to_check.append(sheet_orig[range_str])

            passed_cells = 0
            total_cells = len(cells_to_check)
            failures = []

            for cell in cells_to_check:
                raw_val = cell.value
                coord = cell.coordinate
                formula_str = str(raw_val).strip() if raw_val is not None else ""

                if not formula_str.startswith("="):
                    if must_be_formula:
                        failures.append(f"{coord}: Hardcoded static value '{formula_str}' instead of formula.")
                        continue
                else:
                    clean_formula = formula_str.upper()
                    # Extract function names e.g. SUM, XLOOKUP
                    func_matches = re.findall(r'([A-Z_]{2,})\s*\(', clean_formula)
                    for fn in func_matches:
                        detected_formulas.add(fn)

                    if forbidden_constants and is_hardcoded_cheat(formula_str, defined_names=defined_name_set):
                        failures.append(f"{coord}: Anti-cheat triggered: formula '{formula_str}' contains no cell references.")
                        continue

                    if accepted_funcs:
                        has_accepted = any(fn in clean_formula for fn in accepted_funcs)
                        if not has_accepted:
                            failures.append(f"{coord}: Formula '{formula_str}' does not use required functions: {', '.join(accepted_funcs)}.")
                            continue

                passed_cells += 1

            tc_passed = (passed_cells == total_cells and total_cells > 0)
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"All {total_cells} cells in {range_str} verified successfully."
            else:
                reason = f"{total_cells - passed_cells}/{total_cells} cells failed formula checks. First issues: {'; '.join(failures[:3])}"

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "range": range_str,
                "totalCells": total_cells,
                "passedCells": passed_cells,
                "passed": tc_passed,
                "reason": reason,
                "failures": failures[:10]
            })

        # -------------------------------------------------------------
        # TYPE 3: Format & Style Check (Headers, Borders, Bold, Alignment, Currency)
        # -------------------------------------------------------------
        elif tc_type in ["format_check", "style_check"]:
            range_str = tc.get("range", tc.get("cell", ""))
            req_bold = tc.get("bold")
            req_align = tc.get("align")
            req_fill = tc.get("hasFill")
            req_border = tc.get("hasBorder")
            req_format = tc.get("numberFormat")

            cells_to_check = []
            if ":" in range_str:
                for row in sheet_orig[range_str]:
                    for cell in row:
                        cells_to_check.append(cell)
            elif range_str:
                cells_to_check.append(sheet_orig[range_str])

            passed_cells = 0
            total_cells = len(cells_to_check)
            failures = []

            for cell in cells_to_check:
                coord = cell.coordinate

                # Bold check
                if req_bold is not None:
                    is_bold = bool(cell.font and cell.font.bold)
                    if is_bold != req_bold:
                        failures.append(f"{coord}: Font bold is {is_bold}, expected {req_bold}.")
                        continue

                # Alignment check
                if req_align is not None:
                    curr_align = (cell.alignment.horizontal if cell.alignment else "") or ""
                    if curr_align.lower() != str(req_align).lower():
                        failures.append(f"{coord}: Text alignment is '{curr_align}', expected '{req_align}'.")
                        continue

                # Fill background color check
                if req_fill is not None:
                    has_fill = bool(cell.fill and cell.fill.fill_type and cell.fill.fill_type != "none")
                    if has_fill != req_fill:
                        failures.append(f"{coord}: Background fill is {has_fill}, expected {req_fill}.")
                        continue

                # Border check
                if req_border is not None:
                    b = cell.border
                    has_b = bool(b and (b.left.style or b.right.style or b.top.style or b.bottom.style))
                    if has_b != req_border:
                        failures.append(f"{coord}: Border style is {has_b}, expected {req_border}.")
                        continue

                # Empty cell check when formatting or data presence is required
                if req_format is not None or tc.get("requireValue") or tc.get("nonEmpty"):
                    if cell.value is None or str(cell.value).strip() == "":
                        failures.append(f"{coord}: Cell is empty; expected entered data.")
                        continue

                # Number format / currency check
                if req_format is not None:
                    fmt = str(cell.number_format or "")
                    val_str = str(cell.value or "")
                    if str(req_format).lower() == "currency":
                        is_curr = any(sym in fmt for sym in ["$", "₹", "€", "£", "¥", "Rs", "INR", "USD", "EUR"]) or "[$" in fmt or "0.00" in fmt or any(sym in val_str for sym in ["$", "₹", "€", "£", "¥", "Rs"])
                        if not is_curr:
                            failures.append(f"{coord}: Number format '{fmt}' is not a currency format.")
                            continue

                passed_cells += 1

            tc_passed = (passed_cells == total_cells and total_cells > 0)
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"All {total_cells} cells in {range_str} satisfied formatting requirements."
            else:
                reason = f"{total_cells - passed_cells}/{total_cells} cells failed formatting. First issues: {'; '.join(failures[:3])}"

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "range": range_str,
                "totalCells": total_cells,
                "passedCells": passed_cells,
                "passed": tc_passed,
                "reason": reason,
                "failures": failures[:10]
            })

        # -------------------------------------------------------------
        # TYPE 4: Data Check / Non-Empty Table Data
        # -------------------------------------------------------------
        elif tc_type in ["data_check", "non_empty_check", "table_check"]:
            range_str = tc.get("range", tc.get("cell", ""))
            cells_to_check = []
            if ":" in range_str:
                for row in sheet_orig[range_str]:
                    for cell in row:
                        cells_to_check.append(cell)
            elif range_str:
                cells_to_check.append(sheet_orig[range_str])

            passed_cells = 0
            total_cells = len(cells_to_check)
            failures = []

            for cell in cells_to_check:
                coord = cell.coordinate
                if cell.value is None or str(cell.value).strip() == "":
                    failures.append(f"{coord}: Missing data (cell is blank).")
                else:
                    passed_cells += 1

            tc_passed = (passed_cells == total_cells and total_cells > 0)
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"All {total_cells} cells in {range_str} contain required data."
            else:
                reason = f"{total_cells - passed_cells}/{total_cells} cells are missing data. Issues: {'; '.join(failures[:3])}"

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "range": range_str,
                "totalCells": total_cells,
                "passedCells": passed_cells,
                "passed": tc_passed,
                "reason": reason,
                "failures": failures[:10]
            })

        # -------------------------------------------------------------
        # TYPE 5: Chart & Visual Graph Check
        # -------------------------------------------------------------
        elif tc_type in ["chart_check", "graph_check"]:
            min_charts = int(tc.get("minCharts", 1))
            req_type = str(tc.get("chartType", "")).strip().lower()
            req_title = str(tc.get("title", "")).strip().lower()

            target_sheet_charts = [c for c in detected_charts if c.get("sheet") == sheet_name] if tc.get("sheet") else detected_charts
            tc_passed = len(target_sheet_charts) >= min_charts
            reason = ""

            if not tc_passed:
                reason = f"Expected at least {min_charts} chart(s), found {len(target_sheet_charts)} in sheet '{sheet_name}'."
            else:
                if req_type:
                    type_matched = any(req_type in c.get("type", "").lower() for c in target_sheet_charts)
                    if not type_matched:
                        tc_passed = False
                        found_types = ", ".join([c.get("type", "") for c in target_sheet_charts])
                        reason = f"Expected chart type matching '{req_type}', found: {found_types or 'None'}."

                if tc_passed and req_title:
                    title_matched = any(req_title in c.get("title", "").lower() for c in target_sheet_charts)
                    if not title_matched:
                        tc_passed = False
                        found_titles = ", ".join([f"'{c.get('title')}'" for c in target_sheet_charts if c.get("title")])
                        reason = f"Expected chart title containing '{req_title}', found: {found_titles or 'Untitled'}."

                if tc_passed:
                    reason = f"Verified {len(target_sheet_charts)} chart(s) successfully matching requirements."

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "reason": reason,
                "foundCharts": target_sheet_charts
            })

        # -------------------------------------------------------------
        # TYPE 6: Native Pivot Table Check
        # -------------------------------------------------------------
        elif tc_type in ["pivot_check", "pivot_table_check"]:
            min_pivots = int(tc.get("minPivotTables", 1))
            req_field = str(tc.get("requiredField", "")).strip().lower()

            tc_passed = len(detected_pivots) >= min_pivots
            reason = ""

            if not tc_passed:
                reason = f"Expected at least {min_pivots} native Pivot Table(s), found {len(detected_pivots)} in workbook."
            else:
                if req_field:
                    field_found = False
                    for p in detected_pivots:
                        all_fields = [f.lower() for f in p.get("fields", [])]
                        all_data = [df.get("name", "").lower() for df in p.get("dataFields", [])]
                        if any(req_field in f for f in all_fields + all_data):
                            field_found = True
                            break
                    if not field_found:
                        tc_passed = False
                        reason = f"Pivot table does not include required field '{req_field}'."
                    else:
                        reason = f"Verified native Pivot Table with field '{req_field}'."
                else:
                    reason = f"Verified {len(detected_pivots)} native Pivot Table(s) in workbook."

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "reason": reason,
                "foundPivots": detected_pivots
            })

        # -------------------------------------------------------------
        # TYPE 7: VBA Macro Check
        # -------------------------------------------------------------
        elif tc_type in ["macro_check", "vba_check"]:
            require_vba = tc.get("requireVba", True)
            req_sub = str(tc.get("requiredSub", "")).strip().lower()

            tc_passed = macro_info.get("has_macros", False) if require_vba else True
            reason = ""

            if require_vba and not tc_passed:
                reason = "No VBA macros or vbaProject.bin found in workbook."
            elif req_sub:
                sub_names = [s.lower() for s in macro_info.get("macro_names", [])]
                if not any(req_sub in s for s in sub_names):
                    tc_passed = False
                    found_subs = ", ".join(macro_info.get("macro_names", []))
                    reason = f"Expected VBA Sub/Function '{req_sub}', found: {found_subs or 'None'}."
                else:
                    reason = f"Verified VBA subroutine '{req_sub}'."
            else:
                reason = f"Verified VBA macro project with {len(macro_info.get('macro_names', []))} procedure(s)."

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "reason": reason,
                "foundMacros": macro_info.get("macro_names", [])
            })

        # -------------------------------------------------------------
        # TYPE 8: Power Query Check
        # -------------------------------------------------------------
        elif tc_type in ["power_query_check", "query_check"]:
            require_query = tc.get("requireQuery", True)
            tc_passed = macro_info.get("has_power_query", False) if require_query else True
            reason = "Verified Power Query / data connections in workbook." if tc_passed else "No Power Query connections detected."

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "reason": reason,
                "foundQueries": macro_info.get("query_names", [])
            })

        # -------------------------------------------------------------
        # TYPE 9: Controlled Formula-Behavior Check (Scenario Mutations)
        # -------------------------------------------------------------
        elif tc_type in ["formula_behavior_check", "scenario_check", "behavior_check"]:
            scenarios = tc.get("scenarios", [])
            if not scenarios and tc.get("inputs") and tc.get("expectedOutputs"):
                scenarios = [{
                    "name": tc.get("name", "Default Mutation Scenario"),
                    "inputs": tc.get("inputs", []),
                    "expectedOutputs": tc.get("expectedOutputs", [])
                }]

            scenario_results = []
            all_scenarios_passed = True

            for sc_idx, sc in enumerate(scenarios):
                sc_name = sc.get("name", f"Scenario {sc_idx + 1}")
                sc_inputs = sc.get("inputs", [])
                sc_expected = sc.get("expectedOutputs", [])

                with tempfile.TemporaryDirectory(prefix=f"scen_{sc_idx}_") as sc_tmpdir:
                    sc_orig_path = os.path.join(sc_tmpdir, "scenario_input.xlsx")
                    sc_recalc_path = os.path.join(sc_tmpdir, "scenario_recalc.xlsx")
                    sc_profile_dir = os.path.join(sc_tmpdir, "lo_profile")

                    shutil.copy2(original_path, sc_orig_path)

                    wb_scen = openpyxl.load_workbook(sc_orig_path, data_only=False)
                    target_sc_sheet = resolve_sheet(wb_scen, tc.get("sheet", default_sheet_name))
                    sc_sheet = wb_scen[target_sc_sheet]

                    input_errors = []
                    for inp in sc_inputs:
                        cell_addr = inp.get("cell")
                        new_val = inp.get("value")
                        if not cell_addr:
                            continue
                        current_cell = sc_sheet[cell_addr]
                        current_raw = str(current_cell.value or "").strip()
                        if current_raw.startswith("="):
                            input_errors.append(f"{cell_addr} contains student formula '{current_raw}'; expected input variable.")
                        current_cell.value = new_val

                    wb_scen.save(sc_orig_path)
                    wb_scen.close()

                    recalc_ok, recalc_msg = recalculate_via_cli(sc_orig_path, sc_recalc_path, sc_profile_dir, timeout_sec=20)
                    if not recalc_ok:
                        shutil.copy2(sc_orig_path, sc_recalc_path)

                    wb_scen_recalc = openpyxl.load_workbook(sc_recalc_path, data_only=True)
                    recalc_sheet = wb_scen_recalc[target_sc_sheet]

                    sc_passed = True
                    output_diffs = []

                    for exp in sc_expected:
                        out_cell = exp.get("cell")
                        exp_val = parse_number(exp.get("expectedValue"))
                        abs_tol = float(exp.get("absTolerance", exp.get("tolerance", 0.05)))
                        rel_tol = float(exp.get("relTolerance", 0.001))

                        actual_raw = recalc_sheet[out_cell].value
                        actual_num = parse_number(actual_raw)

                        if actual_num is not None and exp_val is not None:
                            diff_ok = check_dual_tolerance(actual_num, exp_val, abs_tol, rel_tol)
                            if not diff_ok:
                                sc_passed = False
                                output_diffs.append(f"{out_cell}: actual {actual_num} != expected {exp_val} (diff: {abs(actual_num - exp_val):.4f})")
                        else:
                            str_act = str(actual_raw or "").strip().lower()
                            str_exp = str(exp.get("expectedValue") or "").strip().lower()
                            if str_act != str_exp:
                                sc_passed = False
                                output_diffs.append(f"{out_cell}: actual '{actual_raw}' != expected '{exp.get('expectedValue')}'")

                    wb_scen_recalc.close()

                    if not sc_passed:
                        all_scenarios_passed = False

                    scenario_results.append({
                        "name": sc_name,
                        "passed": sc_passed,
                        "recalculated": recalc_ok,
                        "issues": output_diffs,
                        "inputWarnings": input_errors
                    })

            if all_scenarios_passed and len(scenarios) > 0:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"All {len(scenarios)} controlled scenario mutation(s) passed successfully. Formulas dynamically respond to changed inputs."
            else:
                failed_sc = [s["name"] for s in scenario_results if not s["passed"]]
                reason = f"{len(failed_sc)}/{len(scenarios)} scenario(s) failed. Failed: {', '.join(failed_sc[:3])}."

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": all_scenarios_passed,
                "reason": reason,
                "scenarios": scenario_results
            })

    # Compute Criterion Weighted Score
    rubric_breakdown = {}
    total_score = 0.0

    for crit in criteria:
        c_name = crit.get("name", "")
        c_desc = (crit.get("description", "") or "").lower()
        c_lower = c_name.lower()
        weight = float(crit.get("weight", 0))
        stats = criterion_passes.get(c_name, {"total": 0, "passed": 0})

        is_formula_crit = any(k in c_lower or k in c_desc for k in [
            "formula", "calculation", "dynamic", "function", "formulae",
            "summary", "accuracy", "numerical", "total", "totals", "sum", "math", "arithmetic", "logical"
        ])
        is_data_crit = any(k in c_lower or k in c_desc for k in ["data", "table", "structure", "formatting", "format", "entry", "completeness"])
        is_chart_crit = any(k in c_lower or k in c_desc for k in ["chart", "graph", "plot", "visualization", "visual"])
        is_pivot_crit = any(k in c_lower or k in c_desc for k in ["pivot", "pivottable", "pivot table"])
        is_macro_crit = any(k in c_lower or k in c_desc for k in ["vba", "macro", "macros", "automation"])
        is_query_crit = any(k in c_lower or k in c_desc for k in ["power query", "powerquery", "data model", "etl", "transformation"])

        if stats["total"] > 0:
            fraction = stats["passed"] / stats["total"]
        else:
            # If no specific test cases were mapped to this criterion
            if is_chart_crit:
                if len(detected_charts) == 0:
                    fraction = 0.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "chart_scan",
                        "passed": False,
                        "reason": f"No charts or graphs detected in workbook for '{c_name}'."
                    })
                else:
                    fraction = 1.0
                    chart_types = ", ".join([c["type"] for c in detected_charts[:3]])
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "chart_scan",
                        "passed": True,
                        "reason": f"Verified {len(detected_charts)} chart(s) in workbook ({chart_types})."
                    })
            elif is_pivot_crit:
                if len(detected_pivots) == 0:
                    fraction = 0.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "pivot_scan",
                        "passed": False,
                        "reason": f"No native Pivot Tables detected in workbook for '{c_name}'."
                    })
                else:
                    fraction = 1.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "pivot_scan",
                        "passed": True,
                        "reason": f"Verified {len(detected_pivots)} native Pivot Table(s) in workbook."
                    })
            elif is_macro_crit:
                if not macro_info.get("has_macros"):
                    fraction = 0.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "macro_scan",
                        "passed": False,
                        "reason": f"No VBA macros or procedure definitions detected in workbook for '{c_name}'."
                    })
                else:
                    fraction = 1.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "macro_scan",
                        "passed": True,
                        "reason": f"Verified VBA macros in workbook ({', '.join(macro_info.get('macro_names', [])[:3])})."
                    })
            elif is_query_crit:
                if not macro_info.get("has_power_query"):
                    fraction = 0.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "query_scan",
                        "passed": False,
                        "reason": f"No Power Query or data model connections detected for '{c_name}'."
                    })
                else:
                    fraction = 1.0
            elif is_formula_crit:
                is_summary_target = any(k in c_lower for k in ["summary", "total", "accuracy", "numerical"])
                has_sum_formula = any(any(fn in f for fn in ["SUM", "AVERAGE", "COUNT", "MAX", "MIN"]) for f in all_formulas)
                
                if len(all_formulas) == 0:
                    fraction = 0.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "formula_scan",
                        "passed": False,
                        "reason": f"No dynamic formulas detected in workbook. Student entered static values instead of spreadsheet calculations for '{c_name}'."
                    })
                elif is_summary_target:
                    if has_sum_formula:
                        fraction = 1.0
                        assertion_results.append({
                            "criterion": c_name,
                            "type": "summary_formula_scan",
                            "passed": True,
                            "reason": f"Verified summary calculation formulas in workbook for '{c_name}'."
                        })
                    else:
                        fraction = 0.4
                        assertion_results.append({
                            "criterion": c_name,
                            "type": "summary_formula_scan",
                            "passed": False,
                            "reason": f"Workbook contains formulas, but summary formulas (=SUM) were not detected for '{c_name}'."
                        })
                else:
                    fraction = 1.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "formula_scan",
                        "passed": True,
                        "reason": f"Verified dynamic spreadsheet formulas ({len(all_formulas)} detected) for '{c_name}'."
                    })
            elif is_data_crit:
                if not has_any_data:
                    fraction = 0.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "data_scan",
                        "passed": False,
                        "reason": f"No data entered in workbook for '{c_name}'."
                    })
                elif len(table_blank_cells) > 0:
                    fraction = round(max(0.0, populated_table_cells / max(1, total_table_cells)), 2)
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "data_completeness",
                        "passed": False,
                        "reason": f"Incomplete table data: {len(table_blank_cells)} blank cell(s) detected in rows ({', '.join(table_blank_cells[:5])}). Student left required values unentered."
                    })
                else:
                    fraction = 1.0
                    assertion_results.append({
                        "criterion": c_name,
                        "type": "data_completeness",
                        "passed": True,
                        "reason": f"Table data is complete. All {total_table_cells} table cells are populated."
                    })
            else:
                fraction = 0.0
                needs_review = True
                assertion_results.append({
                    "criterion": c_name,
                    "type": "unmapped_rubric",
                    "passed": False,
                    "reason": f"No automated test assertion or keyword rule mapped for criterion '{c_name}'. Flagged for instructor manual review."
                })

        awarded = round(fraction * weight, 2)
        rubric_breakdown[c_name] = awarded
        total_score += awarded

    total_score = min(100.0, max(0.0, round(total_score, 2)))

    eval_status = "COMPLETED"
    if needs_review:
        eval_status = "NEEDS_REVIEW"

    return {
        "success": True,
        "score": total_score,
        "rubric_breakdown": rubric_breakdown,
        "evaluation_status": eval_status,
        "detected_formulas": sorted(list(detected_formulas)),
        "detected_charts": detected_charts,
        "detected_pivots": detected_pivots,
        "detected_macros": macro_info.get("macro_names", []),
        "calculation_warnings": calculation_warnings,
        "assertion_results": assertion_results
    }

def main():
    parser = argparse.ArgumentParser(description="Dual-Workbook Assertion Inspector")
    parser.add_argument("--original", required=True, help="Path to original student .xlsx")
    parser.add_argument("--recalculated", required=True, help="Path to recalculated .xlsx")
    parser.add_argument("--spec", required=True, help="Path to JSON test spec file or JSON string")
    args = parser.parse_args()

    spec_data = None
    if os.path.exists(args.spec):
        with open(args.spec, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
    else:
        try:
            raw_spec = args.spec.strip()
            if (raw_spec.startswith("'") and raw_spec.endswith("'")) or (raw_spec.startswith('"') and raw_spec.endswith('"')):
                raw_spec = raw_spec[1:-1].strip()
            spec_data = json.loads(raw_spec)
        except Exception as e:
            print(json.dumps({"success": False, "error": f"Invalid spec JSON: {str(e)}"}))
            sys.exit(1)

    try:
        report = inspect_and_evaluate(args.original, args.recalculated, spec_data)
        print(json.dumps(report, indent=2))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({
            "success": False,
            "error": f"Inspection runtime exception: {str(e)}",
            "evaluation_status": "NEEDS_REVIEW"
        }))
        sys.exit(1)

if __name__ == "__main__":
    main()
