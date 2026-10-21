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
            """Copy the workbook and report unavailable recalculation when adapter imports fail."""
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
    """Extracts a float from numbers, formatted strings ($1,234.50, ₹1,234.50, (1,234.50), 13.3%, etc.)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return None
        return float(val)
    if isinstance(val, str):
        cleaned = val.strip()
        # Remove currency symbols and formatting labels
        for sym in ["$", "₹", "€", "£", "¥", "Rs.", "Rs", "INR", "USD", "EUR"]:
            cleaned = cleaned.replace(sym, "")
        cleaned = cleaned.replace(",", "").strip()

        # Handle accounting parentheses for negative numbers: (1,234.50) -> -1234.50
        is_negative = False
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = cleaned[1:-1].strip()
            is_negative = True

        if cleaned.endswith("%"):
            try:
                num = float(cleaned[:-1].strip()) / 100.0
                return -num if is_negative else num
            except ValueError:
                return None
        try:
            num = float(cleaned)
            return -num if is_negative else num
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

    # Check for structured table references: e.g. [@Revenue], [@[Profit Margin]], Table1[Sales]
    has_structured_ref = bool(re.search(r'\[@?[A-Za-z0-9_\s\.\%\-]+\]', expr))

    # Check for defined names / named ranges if provided
    has_named_range = False
    if defined_names:
        for name in defined_names:
            if re.search(r'\b' + re.escape(name) + r'\b', expr, re.IGNORECASE):
                has_named_range = True
                break

    if not has_cell_ref and not has_function_call and not has_structured_ref and not has_named_range:
        # A formula with no cell references, no function calls, and no named ranges is hardcoded math, e.g. =100+200
        return True

    return False

def _resolve_cell_value_with_merge(sheet, r: int, c: int):
    """Read cell value, falling back to merged range's top-left cell when the cell is a merge slave (value=None)."""
    v = sheet.cell(row=r, column=c).value
    if v is not None:
        return v
    try:
        for merge_range in sheet.merged_cells.ranges:
            if (merge_range.min_row <= r <= merge_range.max_row and
                    merge_range.min_col <= c <= merge_range.max_col):
                return sheet.cell(row=merge_range.min_row, column=merge_range.min_col).value
    except Exception:
        pass
    return None

def _norm_ws(text: str) -> str:
    """Collapse multiple/embedded whitespace to single space and strip."""
    return re.sub(r'\s+', ' ', text.strip().lower())

def find_anchor_cell(sheet, anchor_text: str, max_rows: int = 200, max_cols: int = 50) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """
    Searches for an anchor cell matching anchor_text (trimmed, case-insensitive).
    Whitespace-normalised so 'Sum of  Amount' (double-space) and 'Sum of\nAmount' both match.
    Resolves merged cell ranges so a slave cell returns its master's value.
    Returns ( (row, col), None ) on unique match.
    Returns ( None, error_reason ) if missing or ambiguous.
    """
    if not anchor_text:
        return None, "Anchor text is empty."
    target = _norm_ws(anchor_text)
    matches = []

    for r in range(1, min(sheet.max_row or 100, max_rows) + 1):
        for c in range(1, min(sheet.max_column or 50, max_cols) + 1):
            cell_val = _resolve_cell_value_with_merge(sheet, r, c)
            if cell_val is not None and isinstance(cell_val, str):
                if _norm_ws(cell_val) == target:
                    matches.append((r, c))

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        match_addrs = [f"{get_column_letter(c)}{r}" for r, c in matches]
        return None, f"Ambiguous anchor: '{anchor_text}' found {len(matches)} times at {', '.join(match_addrs)}. Flagged for review."

    # Fallback Tier: Substring or word-boundary / token match in top rows (rows 1-30)
    fallback_matches = []
    target_norm = re.sub(r'[^a-z0-9]', '', target)
    target_tokens = set(re.findall(r'\w+', target))
    for r in range(1, min(sheet.max_row or 100, 30) + 1):
        for c in range(1, min(sheet.max_column or 50, max_cols) + 1):
            cell_val = _resolve_cell_value_with_merge(sheet, r, c)
            if cell_val is not None and isinstance(cell_val, str):
                v_clean = _norm_ws(cell_val)
                v_norm = re.sub(r'[^a-z0-9]', '', v_clean)
                v_tokens = set(re.findall(r'\w+', v_clean))
                if target in v_clean or (target_norm and target_norm in v_norm) or (target_tokens and target_tokens.issubset(v_tokens)):
                    fallback_matches.append((r, c))

    if len(fallback_matches) == 1:
        return fallback_matches[0], None
    elif len(fallback_matches) > 1:
        match_addrs = [f"{get_column_letter(c)}{r}" for r, c in fallback_matches]
        return None, f"Ambiguous anchor fallback: '{anchor_text}' found {len(fallback_matches)} times at {', '.join(match_addrs)}."

    return None, f"Anchor label '{anchor_text}' not found in sheet."

def find_column_by_anchor(sheet, anchor_text: str, max_rows: int = 50, max_cols: int = 60) -> Optional[int]:
    """
    Finds the 1-based column index where a column header matches anchor_text.
    Whitespace-normalised. Resolves merged cell headers.
    """
    if not anchor_text:
        return None
    target = _norm_ws(anchor_text)
    target_norm = re.sub(r'[^a-z0-9]', '', target)
    target_tokens = set(re.findall(r'\w+', target))

    # Pass 1: Exact match (whitespace-normalised) in top rows
    for r in range(1, min(sheet.max_row or 100, max_rows) + 1):
        for c in range(1, min(sheet.max_column or 50, max_cols) + 1):
            val = _resolve_cell_value_with_merge(sheet, r, c)
            if val is not None and isinstance(val, str):
                if _norm_ws(val) == target:
                    return c

    # Pass 2: Normalized alphanumeric match
    for r in range(1, min(sheet.max_row or 100, max_rows) + 1):
        for c in range(1, min(sheet.max_column or 50, max_cols) + 1):
            val = _resolve_cell_value_with_merge(sheet, r, c)
            if val is not None and isinstance(val, str):
                v_clean = _norm_ws(val)
                if target == "profit" and ("margin" in v_clean or "%" in v_clean):
                    continue
                v_norm = re.sub(r'[^a-z0-9]', '', v_clean)
                if target_norm and (v_norm == target_norm or target_norm in v_norm):
                    return c

    # Pass 3: Token subset match
    if target_tokens:
        for r in range(1, min(sheet.max_row or 100, max_rows) + 1):
            for c in range(1, min(sheet.max_column or 50, max_cols) + 1):
                val = _resolve_cell_value_with_merge(sheet, r, c)
                if val is not None and isinstance(val, str):
                    v_clean = _norm_ws(val)
                    if target == "profit" and ("margin" in v_clean or "%" in v_clean):
                        continue
                    v_tokens = set(re.findall(r'\w+', v_clean))
                    if target_tokens.issubset(v_tokens):
                        return c

    return None

TABLE_CONCEPTS = {
    "date": ["date", "trans_date", "transaction date", "time"],
    "client": ["client", "company", "customer", "account", "client name", "company name"],
    "ticker": ["ticker", "tickers", "symbol", "stock", "code", "xnas", "xnys"],
    "contact": ["contact", "contact name", "rep", "representative", "cleaned contact", "full name", "clean contact", "name"],
    "department": ["department", "dept", "division", "sector"],
    "payment": ["payment", "payment method", "pay method", "pay type", "type"],
    "revenue": ["revenue", "sales", "rev", "gross sales", "inflow"],
    "profit": ["profit", "net profit", "gain", "earnings"], # disambiguated from profit margin
    "margin": ["profit margin", "margin", "% margin", "margin %", "profit %", "markup"]
}

def detect_table_bounds(sheet, max_scan_rows: int = 100, max_scan_cols: int = 60) -> Dict[str, Any]:
    """
    Intelligently discovers the primary data table bounding box and semantic column mapping.
    Handles tables shifted by any number of rows or columns (e.g. starting at row 15, col K).
    Returns a dict with:
      - header_row: row index of table headers (or None)
      - data_start_row: first row of actual data records
      - data_end_row: last row of actual data records (stops before summary/totals)
      - start_col: leftmost active table column index
      - end_col: rightmost active table column index
      - col_map: dict of concept -> list of 1-based column indices
      - header_texts: dict of col_idx -> header string
    """
    max_r = min(sheet.max_row or 100, max_scan_rows)
    max_c = min(sheet.max_column or 50, max_scan_cols)

    best_header_row = None
    best_concept_count = 0
    best_row_headers = {}

    for r in range(1, max_r + 1):
        row_headers = {}
        matched_concepts = set()
        for c in range(1, max_c + 1):
            val = sheet.cell(row=r, column=c).value
            if val is not None and isinstance(val, str) and val.strip():
                v_clean = val.strip().lower()
                # Skip Excel default table auto-headers like "Column1", "Column2"
                if re.fullmatch(r'column\s*\d+', v_clean):
                    continue
                row_headers[c] = val.strip()
                for concept, kws in TABLE_CONCEPTS.items():
                    for kw in kws:
                        if kw == v_clean or kw in v_clean:
                            # Disambiguate profit vs margin
                            if concept == "profit" and ("margin" in v_clean or "%" in v_clean):
                                continue
                            matched_concepts.add(concept)
                            break

        if len(matched_concepts) > best_concept_count:
            best_concept_count = len(matched_concepts)
            best_header_row = r
            best_row_headers = row_headers

    # If no row had at least 2 recognized concepts, fallback to row with the most text columns in first 30 rows
    if best_header_row is None or best_concept_count < 2:
        max_str_count = 0
        for r in range(1, min(max_r, 30) + 1):
            str_cols = {}
            for c in range(1, max_c + 1):
                val = sheet.cell(row=r, column=c).value
                if val is not None and isinstance(val, str) and val.strip() and not str(val).strip().startswith("="):
                    str_cols[c] = val.strip()
            if len(str_cols) > max_str_count:
                max_str_count = len(str_cols)
                best_header_row = r
                best_row_headers = str_cols

    if not best_row_headers or best_header_row is None:
        return {
            "header_row": 1,
            "data_start_row": 2,
            "data_end_row": min(max_r, 35),
            "start_col": 1,
            "end_col": max_c,
            "col_map": {k: [] for k in TABLE_CONCEPTS.keys()},
            "header_texts": {}
        }

    start_col = min(best_row_headers.keys())
    end_col = max(best_row_headers.keys())

    # Map each column to concept(s)
    col_map = {k: [] for k in TABLE_CONCEPTS.keys()}
    for c, h_text in best_row_headers.items():
        h_clean = h_text.strip().lower()
        for concept, kws in TABLE_CONCEPTS.items():
            for kw in kws:
                if kw == h_clean or kw in h_clean:
                    if concept == "profit" and ("margin" in h_clean or "%" in h_clean):
                        continue
                    if c not in col_map[concept]:
                        col_map[concept].append(c)
                    break

    # Determine data_start_row and data_end_row
    data_start_row = best_header_row + 1
    # Check if data_start_row is another header row (e.g. secondary header)
    if data_start_row <= max_r:
        next_vals = [sheet.cell(row=data_start_row, column=c).value for c in range(start_col, end_col + 1)]
        concept_matches_next = sum(1 for v in next_vals if isinstance(v, str) and any(any(kw in v.lower() for kw in kws) for kws in TABLE_CONCEPTS.values()))
        if concept_matches_next >= 3:
            data_start_row += 1

    data_end_row = data_start_row
    for r in range(data_start_row, max_r + 1):
        row_vals = [sheet.cell(row=r, column=c).value for c in range(start_col, end_col + 1)]
        has_content = any(v is not None and str(v).strip() != "" for v in row_vals)
        if not has_content:
            # Check if next row also has no content before stopping
            if r + 1 <= max_r:
                next_r_vals = [sheet.cell(row=r + 1, column=c).value for c in range(start_col, end_col + 1)]
                if not any(v is not None and str(v).strip() != "" for v in next_r_vals):
                    break
            else:
                break

        # Check for summary row
        is_summary = any(
            any(kw in str(v or "").lower() for kw in ["total", "sum", "average", "avg", "grand total", "subtotal", "overall"])
            for v in row_vals
        )
        if is_summary:
            break

        data_end_row = r

    return {
        "header_row": best_header_row,
        "data_start_row": data_start_row,
        "data_end_row": max(data_start_row, data_end_row),
        "start_col": start_col,
        "end_col": end_col,
        "col_map": col_map,
        "header_texts": best_row_headers
    }

def parse_range_rows(range_str: str, default_start: int = 4, default_end: int = 35) -> Tuple[int, int]:
    """Extracts (start_row, end_row) from a range string like 'I4:I35' or 'D4:E31'."""
    if not range_str:
        return default_start, default_end
    rows = [int(n) for n in re.findall(r'\d+', range_str)]
    if len(rows) >= 2:
        return min(rows), max(rows)
    elif len(rows) == 1:
        return rows[0], rows[0]
    return default_start, default_end

def find_anchor_column_data_bounds(sheet, anchor_text: str, max_scan_rows: int = 500) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    When an anchor column header is known, find:
      - The anchor column index
      - The true first data row (skipping spacer/padding rows)
      - The last populated data row in that column (before summary/total rows)
    Returns (anchor_col, data_start_row, data_end_row).
    Returns (None, None, None) if anchor not found.
    """
    anchor_coords, err = find_anchor_cell(sheet, anchor_text, max_rows=50)
    if err or not anchor_coords:
        return None, None, None

    anchor_row, anchor_col = anchor_coords
    limit_row = min(sheet.max_row or max_scan_rows, max_scan_rows)

    # Find the true first populated data row after anchor_row (skips blank spacer rows / merged subheaders)
    first_data_row = None
    for r in range(anchor_row + 1, min(limit_row, anchor_row + 10) + 1):
        v = sheet.cell(row=r, column=anchor_col).value
        if v is not None and str(v).strip() != "":
            first_data_row = r
            break

    if first_data_row is None:
        first_data_row = anchor_row + 1

    data_start = first_data_row
    data_end = data_start

    for r in range(data_start, limit_row + 1):
        v = sheet.cell(row=r, column=anchor_col).value
        if v is None or str(v).strip() == "":
            next_v = sheet.cell(row=r + 1, column=anchor_col).value if r + 1 <= limit_row else None
            if next_v is None or str(next_v).strip() == "":
                break
        else:
            v_str = str(v).strip().upper()
            v_clean = re.sub(r'\s+', '', v_str)
            if v_clean.startswith("=SUM(") or v_clean.startswith("=SUBTOTAL(") or v_clean.startswith("=TOTAL"):
                break
            # Check row labels in col 1 or adjacent col for summary row indicator
            row_label_1 = str(sheet.cell(row=r, column=1).value or "").strip().lower()
            row_label_prev = str(sheet.cell(row=r, column=max(1, anchor_col - 1)).value or "").strip().lower()
            if any(k in row_label_1 or k in row_label_prev for k in ["grand total", "total", "subtotal", "average", "summary"]):
                break
            data_end = r

    return anchor_col, data_start, max(data_start, data_end)

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

# ── SHEET SYNONYM TABLE (Phase 1) ────────────────────────────────────────────
# Maps canonical sheet names → common student alternatives.
# Covers untouched default tabs (Sheet1/Sheet2/Sheet3), synonyms, and locale variants.
SHEET_SYNONYMS: Dict[str, List[str]] = {
    "dashboard":        ["summary", "overview", "report", "analysis", "kpi",
                         "exec", "executive", "sales dashboard", "performance",
                         "sheet1", "sheet2", "sheet3"],
    "sales database":   ["sales data", "transactions", "data", "raw data",
                         "sales", "records", "database", "transaction data",
                         "sheet1", "sheet2", "sheet3"],
    "employees":        ["emp", "staff", "people", "workforce", "hr", "employee data"],
    "products":         ["items", "inventory", "catalog", "product list", "products list"],
    "customers":        ["clients", "accounts", "contacts", "customer list", "customer data"],
    "financials":       ["finance", "financial data", "p&l", "income", "revenue"],
}

def _sheet_has_numeric_data(sheet) -> bool:
    """Returns True if the sheet has substantive content (not a blank README/cover page)."""
    count = 0
    for r in range(1, min((sheet.max_row or 0) + 1, 40)):
        for c in range(1, min((sheet.max_column or 0) + 1, 20)):
            v = sheet.cell(row=r, column=c).value
            if isinstance(v, (int, float)) and v != 0:
                return True
            if isinstance(v, str) and len(v.strip()) > 2:
                count += 1
                if count >= 5:
                    return True
    return False

def resolve_sheet(wb, target_name: Optional[str] = None, anchor_hint: Optional[str] = None) -> Optional[str]:
    """
    6-tier resilient sheet resolver (Phase 1 upgrade):
      T1: Exact name match
      T2: Trimmed + whitespace-normalised case-insensitive match
      T3: Substring match
      T4: Synonym dictionary match (handles Sheet2, Summary, Overview, etc.)
      T5: Anchor-text scan (finds which sheet contains the anchor label)
      T6: First sheet that has real numeric/text data (skips README/Cover pages)
    """
    if not wb.sheetnames:
        return None

    # No target → anchor scan or first real-data sheet
    if not target_name or not str(target_name).strip():
        if anchor_hint:
            for s in wb.sheetnames:
                coords, _ = find_anchor_cell(wb[s], anchor_hint)
                if coords:
                    return s
        data_sheets = [s for s in wb.sheetnames if _sheet_has_numeric_data(wb[s])]
        return data_sheets[0] if data_sheets else wb.sheetnames[0]

    raw_target = str(target_name).strip()
    clean_target = _norm_ws(raw_target)

    # T1: Exact
    if raw_target in wb.sheetnames:
        return raw_target

    # T2: Whitespace-normalised case-insensitive
    for s in wb.sheetnames:
        if _norm_ws(s) == clean_target:
            return s

    # T3: Substring
    for s in wb.sheetnames:
        s_clean = _norm_ws(s)
        if clean_target in s_clean or s_clean in clean_target:
            return s

    # T4: Synonym dictionary
    synonyms = SHEET_SYNONYMS.get(clean_target, [])
    if not synonyms:
        # Partial key match: "sales" → key "sales database"
        for key, syns in SHEET_SYNONYMS.items():
            if clean_target in key or key in clean_target:
                synonyms = syns
                break
    for s in wb.sheetnames:
        s_clean = _norm_ws(s)
        if s_clean in synonyms or any(syn in s_clean for syn in synonyms):
            return s

    # T5: Anchor-text scan across ALL sheets
    if anchor_hint:
        anchor_matches = []
        for s in wb.sheetnames:
            coords, _ = find_anchor_cell(wb[s], anchor_hint)
            if coords:
                anchor_matches.append(s)
        if len(anchor_matches) == 1:
            return anchor_matches[0]
        if len(anchor_matches) > 1:
            # Prefer the match closest to the target name
            for s in anchor_matches:
                if clean_target in _norm_ws(s):
                    return s
            return anchor_matches[0]

    # T6: First sheet with real data (skip blank cover pages)
    data_sheets = [s for s in wb.sheetnames if _sheet_has_numeric_data(wb[s])]
    if data_sheets:
        return data_sheets[0]

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

def inspect_openxml_charts_from_zip(xlsx_path: str) -> List[Dict[str, Any]]:
    """
    Fallback chart inspector that reads chart XML directly from the .xlsx zip.
    Catches charts from Google Sheets, LibreOffice, and WPS that openpyxl misses.
    """
    charts = []
    CHART_TYPE_TAGS = {
        'barChart': 'BarChart', 'bar3DChart': 'BarChart',
        'lineChart': 'LineChart', 'line3DChart': 'LineChart',
        'pieChart': 'PieChart', 'pie3DChart': 'PieChart', 'doughnutChart': 'DoughnutChart',
        'areaChart': 'AreaChart', 'area3DChart': 'AreaChart',
        'scatterChart': 'ScatterChart', 'bubbleChart': 'BubbleChart',
        'radarChart': 'RadarChart', 'stockChart': 'StockChart',
        'columnChart': 'ColumnChart',
    }

    def _extract_title(root):
        """Extract plain-text chart title from XML."""
        for elem in root.iter():
            tag = elem.tag.split('}')[-1]
            if tag == 'title':
                texts = []
                for t_elem in elem.iter():
                    if t_elem.tag.split('}')[-1] == 't' and t_elem.text:
                        texts.append(t_elem.text.strip())
                if texts:
                    return ' '.join(texts)
        return ''

    def _extract_chart_type(root):
        """Detect chart type from first recognized chart tag."""
        for elem in root.iter():
            tag = elem.tag.split('}')[-1]
            if tag in CHART_TYPE_TAGS:
                if tag == 'barChart':
                    bar_dir = elem.attrib.get('barDir', 'col')
                    return 'ColumnChart' if bar_dir == 'col' else 'BarChart'
                return CHART_TYPE_TAGS[tag]
        return 'UnknownChart'

    def _count_series(root):
        return sum(1 for elem in root.iter() if elem.tag.split('}')[-1] == 'ser')

    try:
        with zipfile.ZipFile(xlsx_path, 'r') as z:
            names = z.namelist()
            chart_files = [n for n in names if re.match(r'xl/charts/chart\d*\.xml', n)]

            sheet_chart_map = {}
            for n in names:
                if 'xl/worksheets/_rels/sheet' in n and n.endswith('.rels'):
                    try:
                        rels_xml = z.read(n)
                        rels_root = ET.fromstring(rels_xml)
                        sheet_idx = re.search(r'sheet(\d+)\.xml\.rels', n)
                        for rel in rels_root:
                            target = rel.attrib.get('Target', '')
                            if '/drawings/' in target or 'drawing' in target.lower():
                                drawing_rels_path = 'xl/drawings/_rels/' + target.split('/')[-1] + '.rels'
                                if drawing_rels_path in names:
                                    dr_xml = z.read(drawing_rels_path)
                                    dr_root = ET.fromstring(dr_xml)
                                    for dr_rel in dr_root:
                                        dr_target = dr_rel.attrib.get('Target', '')
                                        if '/charts/' in dr_target:
                                            chart_fname = 'xl/charts/' + dr_target.split('/')[-1]
                                            if sheet_idx:
                                                sheet_chart_map[chart_fname] = f"sheet{sheet_idx.group(1)}"
                    except Exception:
                        pass

            for chart_file in chart_files:
                try:
                    xml_bytes = z.read(chart_file)
                    root = ET.fromstring(xml_bytes)
                    chart_type = _extract_chart_type(root)
                    chart_title = _extract_title(root)
                    series_count = _count_series(root)
                    sheet_ref = sheet_chart_map.get(chart_file, 'unknown')
                    charts.append({
                        'type': chart_type,
                        'title': chart_title,
                        'seriesCount': series_count,
                        'sheet': sheet_ref,
                        'source': 'openxml_zip',
                        'file': chart_file
                    })
                except Exception:
                    pass
    except Exception:
        pass

    return charts

def inspect_and_evaluate(original_path: str, recalculated_path: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect original workbook features and recalculated values against the test spec.

    Return rubric scores, assertion evidence, detected features, warnings, and review status.
    """
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
    total_rubric_weight = sum(float(c.get("weight", 0)) for c in criteria)

    # ── WEIGHT NORMALIZATION (Phase 7): Always ensure weights sum to exactly 100 ──
    if total_rubric_weight == 0:
        total_rubric_weight = 100.0
        for crit in criteria:
            crit["weight"] = 100.0 / max(len(criteria), 1)
    elif abs(total_rubric_weight - 100.0) > 0.5:
        factor = 100.0 / total_rubric_weight
        for crit in criteria:
            crit["weight"] = float(crit.get("weight", 0)) * factor
        total_rubric_weight = 100.0

    for crit in criteria:
        criterion_weights[crit.get("name", "")] = float(crit.get("weight", 0))

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

    # ── OPENXML FALLBACK: Catch Google Sheets / LibreOffice charts missed by openpyxl (Phase 3) ──
    openxml_charts = inspect_openxml_charts_from_zip(original_path)
    if openxml_charts:
        existing_keys = {(c.get('type'), c.get('title')) for c in detected_charts}
        for oc in openxml_charts:
            if (oc.get('type'), oc.get('title')) not in existing_keys:
                detected_charts.append(oc)
                existing_keys.add((oc.get('type'), oc.get('title')))

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
        table_bounds = detect_table_bounds(sheet_orig)
        col_map = table_bounds.get("col_map", {})

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
            search_coord = target_cell_coord

            def _check_val(v, exp):
                n = parse_number(v)
                e = parse_number(exp) if exp is not None else None
                if n is not None and e is not None:
                    if check_dual_tolerance(n, e, abs_tol, rel_tol):
                        return True
                    # Tolerance for decimal (0.15) vs whole percentage (15.0) representations
                    if abs(n) <= 1.0 and abs(e) > 1.0 and check_dual_tolerance(n * 100.0, e, abs_tol, rel_tol):
                        return True
                    if abs(e) <= 1.0 and abs(n) > 1.0 and check_dual_tolerance(n, e * 100.0, abs_tol, rel_tol):
                        return True
                str_v = str(v or "").strip().lower()
                str_e = str(exp or "").strip().lower()
                if str_v == str_e:
                    return True
                # Fuzzy text matching (e.g. "Cloud Tech" in "Cloud Tech_Texas" or "Cloud Tech - Texas")
                v_clean = re.sub(r'[^a-z0-9]', '', str_v)
                e_clean = re.sub(r'[^a-z0-9]', '', str_e)
                if v_clean and e_clean and (v_clean in e_clean or e_clean in v_clean):
                    return True
                return False

            if actual_num is not None and expected_num is not None:
                passed = _check_val(cell_val, tc.get("expectedValue"))
            elif expected_num is None:
                passed = _check_val(cell_val, tc.get("expectedValue"))

            # ── RETRY 1: If not passed, check 4×4 bounding box around target_cell_coord (merged cell / offset) ──
            if not passed:
                try:
                    t_row = int(re.search(r'\d+', target_cell_coord).group())
                    t_col_str = re.match(r'[A-Z]+', target_cell_coord).group()
                    from openpyxl.utils import column_index_from_string
                    t_col = column_index_from_string(t_col_str)
                    for dr in range(-2, 5):
                        for dc in range(-2, 5):
                            nr, nc = t_row + dr, t_col + dc
                            if nr < 1 or nc < 1:
                                continue
                            cand_coord = f"{get_column_letter(nc)}{nr}"
                            cand_val = sheet_recalc[cand_coord].value
                            if _check_val(cand_val, expected_num if expected_num is not None else tc.get("expectedValue")):
                                passed = True
                                search_coord = cand_coord
                                cell_val = cand_val
                                actual_num = parse_number(cand_val)
                                break
                        if passed:
                            break
                except Exception:
                    pass

            # ── RETRY 2: If still not passed and anchor given, try all 4 directions & offsets 1-3 ──
            if not passed and tc.get("anchor"):
                anchor_coords_r, _ = find_anchor_cell(sheet_orig, tc.get("anchor"))
                if anchor_coords_r:
                    for try_dir in ["right", "down", "left", "up"]:
                        for try_offset in [1, 2, 3]:
                            r_t, c_t = get_offset_cell(anchor_coords_r[0], anchor_coords_r[1], try_dir, try_offset)
                            if r_t < 1 or c_t < 1:
                                continue
                            try_coord = f"{get_column_letter(c_t)}{r_t}"
                            try_val = sheet_recalc[try_coord].value
                            if _check_val(try_val, expected_num if expected_num is not None else tc.get("expectedValue")):
                                passed = True
                                search_coord = try_coord
                                cell_val = try_val
                                actual_num = parse_number(try_val)
                                break
                        if passed:
                            break

            if passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"Cell {search_coord} matched expected value {tc.get('expectedValue')} within tolerance."
            else:
                reason = f"Cell {target_cell_coord} value '{cell_val}' did not match expected '{tc.get('expectedValue')}'. Scanned surrounding region — no match found."

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "cell": search_coord,
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
            anchor_text = tc.get("columnAnchor") or tc.get("anchor")
            accepted_funcs = [f.upper() for f in tc.get("acceptedFunctions", [])]
            forbidden_constants = tc.get("forbiddenConstants", True)
            must_be_formula = tc.get("mustBeFormula", True)
            require_absolute = tc.get("requireAbsoluteReferences", False)

            # Dynamic Header / Anchor Column Resolution with row-snapping (Phase 2)
            if anchor_text:
                anc_col, anc_data_start, anc_data_end = find_anchor_column_data_bounds(sheet_orig, anchor_text)
                if anc_col and anc_data_start and anc_data_end:
                    col_let = get_column_letter(anc_col)
                    range_str = f"{col_let}{anc_data_start}:{col_let}{anc_data_end}"
                else:
                    anchor_col = find_column_by_anchor(sheet_orig, anchor_text)
                    if anchor_col:
                        start_r, end_r = parse_range_rows(range_str)
                        col_let = get_column_letter(anchor_col)
                        range_str = f"{col_let}{start_r}:{col_let}{end_r}"

            cells_to_check = []
            if ":" in range_str:
                for row in sheet_orig[range_str]:
                    for cell in row:
                        cells_to_check.append(cell)
            elif range_str:
                cells_to_check.append(sheet_orig[range_str])

            # Multi-column helper disambiguation (e.g. raw names in Col D, cleaned formulas in Col E)
            col_cells = {}
            for cell in cells_to_check:
                col_cells.setdefault(cell.column, []).append(cell)

            if len(col_cells) > 1:
                best_col = None
                best_score = -1
                h_r = table_bounds.get("header_row", 1)

                for c_idx, c_list in col_cells.items():
                    score = 0
                    h_val = str(sheet_orig.cell(row=h_r, column=c_idx).value or "").strip().lower()

                    if accepted_funcs:
                        fn_cnt = sum(1 for c in c_list if any(fn in str(c.value or "").upper() for fn in accepted_funcs))
                        score += fn_cnt * 10
                    elif must_be_formula:
                        f_cnt = sum(1 for c in c_list if str(c.value or "").strip().startswith("="))
                        score += f_cnt * 5

                    # Header matching bonus
                    crit_tokens = [w for w in re.findall(r'\w+', crit_name.lower()) if len(w) > 3 and w not in ["with", "from", "using", "text", "dynamic", "calculation"]]
                    if any(t in h_val for t in crit_tokens):
                        score += 50
                    if anchor_text and _norm_ws(anchor_text) in _norm_ws(h_val):
                        score += 100

                    if score > best_score:
                        best_score = score
                        best_col = c_idx

                if best_score > 0 and best_col:
                    cells_to_check = col_cells[best_col]
                    start_r, end_r = parse_range_rows(range_str)
                    col_let = get_column_letter(best_col)
                    range_str = f"{col_let}{start_r}:{col_let}{end_r}"

            def evaluate_formula_cells(target_cells):
                p_cells = 0
                fails = []
                for cell in target_cells:
                    raw_val = cell.value
                    coord = cell.coordinate
                    formula_str = str(raw_val).strip() if raw_val is not None else ""

                    if not formula_str.startswith("="):
                        if must_be_formula:
                            # Phase 8: Check if this is an error value from a broken formula (e.g. #REF!, #VALUE!, #NAME?, #DIV/0!)
                            if str(raw_val or "").startswith("#") and (str(raw_val or "").endswith("!") or str(raw_val or "").endswith("?")):
                                fails.append(f"{coord}: Formula reference error '{raw_val}' — formula exists but encountered an evaluation error. Check references.")
                                p_cells += 1  # give partial credit / benefit of doubt on formula existence
                                continue
                            fails.append(f"{coord}: Hardcoded static value '{formula_str}' instead of formula.")
                            continue
                    else:
                        clean_formula = formula_str.upper()
                        func_matches = re.findall(r'([A-Z_]{2,})\s*\(', clean_formula)
                        for fn in func_matches:
                            detected_formulas.add(fn)

                        if forbidden_constants and is_hardcoded_cheat(formula_str, defined_names=defined_name_set):
                            fails.append(f"{coord}: Anti-cheat triggered: formula '{formula_str}' contains no cell references.")
                            continue

                        if require_absolute and "$" not in formula_str:
                            fails.append(f"{coord}: Formula '{formula_str}' does not use absolute cell references ($).")
                            continue

                        if accepted_funcs:
                            has_accepted = any(fn in clean_formula for fn in accepted_funcs)
                            if not has_accepted:
                                fails.append(f"{coord}: Formula '{formula_str}' does not use required functions: {', '.join(accepted_funcs)}.")
                                continue
                    p_cells += 1
                return p_cells, fails

            # Trim leading and trailing blank cells beyond the table's populated bounds
            while cells_to_check and (cells_to_check[0].value is None or str(cells_to_check[0].value).strip() == ""):
                cells_to_check.pop(0)
            while cells_to_check and (cells_to_check[-1].value is None or str(cells_to_check[-1].value).strip() == ""):
                cells_to_check.pop()

            # ── ARRAYFORMULA / SPILL DETECTION (Phase 5) ──
            arrayformula_col_pass = False
            if must_be_formula and cells_to_check:
                first_cell = cells_to_check[0]
                first_val = str(first_cell.value or "").strip().upper()
                if first_val.startswith("=ARRAYFORMULA(") or first_val.startswith("=ARRAYFORMULA ("):
                    arrayformula_col_pass = True
                elif first_val.startswith("=") and len(cells_to_check) > 1:
                    rest_empty = all(c.value is None or str(c.value).strip() == "" for c in cells_to_check[1:])
                    rest_numeric = all(
                        isinstance(sheet_recalc[c.coordinate].value, (int, float)) or (sheet_recalc[c.coordinate].value is not None and str(sheet_recalc[c.coordinate].value).strip() != "")
                        for c in cells_to_check[1:]
                    )
                    if rest_empty and rest_numeric:
                        arrayformula_col_pass = True

            passed_cells, failures = evaluate_formula_cells(cells_to_check)
            total_cells = len(cells_to_check)

            # Heuristic Column Shift Rescue (e.g. Profit vs Profit Margin shifted by 1 column)
            if (passed_cells == 0 or passed_cells < total_cells * 0.5) and must_be_formula:
                candidate_anchors = [crit_name]
                if "margin" in crit_name.lower():
                    candidate_anchors.extend(["Profit Margin", "Margin", "% Margin"])
                if "contact" in crit_name.lower() or "name" in crit_name.lower():
                    candidate_anchors.extend(["Contact", "Cleaned Contact", "Full Name", "Clean Contact", "Name"])
                if "department" in crit_name.lower():
                    candidate_anchors.extend(["Department", "Dept"])

                for cand in candidate_anchors:
                    cand_col = find_column_by_anchor(sheet_orig, cand)
                    if cand_col and all(c.column != cand_col for c in cells_to_check):
                        start_r, end_r = parse_range_rows(range_str)
                        cand_cells = [sheet_orig.cell(row=r, column=cand_col) for r in range(start_r, end_r + 1)]
                        while cand_cells and (cand_cells[-1].value is None or str(cand_cells[-1].value).strip() == ""):
                            cand_cells.pop()
                        cand_passed, cand_fails = evaluate_formula_cells(cand_cells)
                        if cand_passed > passed_cells and cand_passed >= len(cand_cells) * 0.8:
                            cells_to_check = cand_cells
                            total_cells = len(cand_cells)
                            passed_cells = cand_passed
                            failures = cand_fails
                            col_let = get_column_letter(cand_col)
                            range_str = f"{col_let}{start_r}:{col_let}{end_r}"
                            break

            # Universal Table Column Rescue across all active table columns
            if (passed_cells == 0 or passed_cells < total_cells * 0.5) and must_be_formula:
                t_start_c = table_bounds.get("start_col", 1)
                t_end_c = table_bounds.get("end_col", sheet_orig.max_column or 50)
                t_start_r = table_bounds.get("data_start_row", 4)
                t_end_r = table_bounds.get("data_end_row", 31)

                for c_cand in range(t_start_c, t_end_c + 1):
                    if any(c.column == c_cand for c in cells_to_check):
                        continue
                    cand_cells = [sheet_orig.cell(row=r, column=c_cand) for r in range(t_start_r, t_end_r + 1)]
                    while cand_cells and (cand_cells[-1].value is None or str(cand_cells[-1].value).strip() == ""):
                        cand_cells.pop()
                    if not cand_cells:
                        continue

                    # CRITICAL GUARD: Prevent Contact Cleaning from falsely stealing Profit Margin / arithmetic formulas!
                    if "contact" in crit_name.lower() or "name" in crit_name.lower():
                        h_r = table_bounds.get("header_row", 1)
                        h_val = str(sheet_orig.cell(row=h_r, column=c_cand).value or "").lower()
                        has_text_func = any(any(fn in str(c.value or "").upper() for fn in ["PROPER", "TRIM", "CONCAT", "UPPER", "LOWER"]) for c in cand_cells)
                        is_contact_header = any(k in h_val for k in ["contact", "name", "rep"])
                        if not has_text_func and not is_contact_header:
                            continue

                    # CRITICAL GUARD: Prevent Profit Margin from stealing text / contact formulas!
                    if "margin" in crit_name.lower():
                        h_r = table_bounds.get("header_row", 1)
                        h_val = str(sheet_orig.cell(row=h_r, column=c_cand).value or "").lower()
                        has_margin_header = any(k in h_val for k in ["margin", "%"])
                        has_ratio = any(any(op in str(c.value or "") for op in ["/", "IFERROR"]) for c in cand_cells)
                        if not has_margin_header and not has_ratio:
                            continue

                    cand_passed, cand_fails = evaluate_formula_cells(cand_cells)
                    if cand_passed > passed_cells and cand_passed >= len(cand_cells) * 0.8:
                        cells_to_check = cand_cells
                        total_cells = len(cand_cells)
                        passed_cells = cand_passed
                        failures = cand_fails
                        col_let = get_column_letter(c_cand)
                        range_str = f"{col_let}{t_start_r}:{col_let}{t_end_r}"
                        break

            if arrayformula_col_pass:
                passed_cells = total_cells
                failures = []
                tc_passed = True
                reason = f"Verified {total_cells}/{total_cells} cells via dynamic array/spill formula in {range_str}."
            else:
                tc_passed = (total_cells > 0 and (passed_cells == total_cells or (passed_cells >= total_cells * 0.85 and total_cells >= 4)))
                if tc_passed:
                    reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} successfully."
                else:
                    reason = f"{total_cells - passed_cells}/{total_cells} cells failed formula checks. First issues: {'; '.join(failures[:3])}"

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

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
            anchor_text = tc.get("columnAnchor") or tc.get("anchor")
            req_bold = tc.get("bold")
            req_align = tc.get("align")
            req_fill = tc.get("hasFill")
            req_border = tc.get("hasBorder")
            req_format = tc.get("numberFormat")

            # Dynamic Header / Anchor Column Resolution
            if anchor_text:
                anchor_col = find_column_by_anchor(sheet_orig, anchor_text)
                if anchor_col:
                    start_r, end_r = parse_range_rows(range_str)
                    col_let = get_column_letter(anchor_col)
                    range_str = f"{col_let}{start_r}:{col_let}{end_r}"

            cells_to_check = []
            if ":" in range_str:
                for row in sheet_orig[range_str]:
                    for cell in row:
                        cells_to_check.append(cell)
            elif range_str:
                cells_to_check.append(sheet_orig[range_str])

            # Multi-column format disambiguation (e.g. Currency in Col G & H, Percent in Col I)
            col_cells = {}
            for cell in cells_to_check:
                col_cells.setdefault(cell.column, []).append(cell)

            if len(col_cells) > 1 and req_format:
                fmt_lower = str(req_format).lower()
                matching_cols = []
                for c_idx, c_list in col_cells.items():
                    cnt = 0
                    for c in c_list:
                        f = str(c.number_format or "")
                        v = str(c.value or "")
                        rv = sheet_recalc[c.coordinate].value
                        if fmt_lower == "currency":
                            if any(s in f for s in ["$", "₹", "€", "£", "USD", "INR", "[$"]) or "#,##" in f or "0.00" in f or any(s in v for s in ["$", "₹", "€", "£"]) or (isinstance(c.value, (int, float)) and float(c.value) > 1.0) or (isinstance(rv, (int, float)) and float(rv) > 1.0):
                                cnt += 1
                        elif fmt_lower in ["percent", "percentage", "%"]:
                            if "%" in f or "%" in v or (isinstance(c.value, (int, float)) and 0.0 <= float(c.value) <= 1.0) or (isinstance(rv, (int, float)) and 0.0 <= float(rv) <= 1.0):
                                cnt += 1
                    if cnt >= len(c_list) * 0.6:
                        matching_cols.append(c_idx)
                if matching_cols:
                    cells_to_check = [c for c in cells_to_check if c.column in matching_cols]
                    start_r, end_r = parse_range_rows(range_str)
                    range_str = f"{get_column_letter(min(matching_cols))}{start_r}:{get_column_letter(max(matching_cols))}{end_r}"

            # Format Column Shift Rescue (e.g. percentage requested but range pointed to currency column)
            if req_format and str(req_format).lower() in ["percent", "percentage", "%"]:
                curr_pct_cnt = sum(1 for c in cells_to_check if "%" in str(c.number_format or "") or "%" in str(c.value or "") or (isinstance(sheet_recalc[c.coordinate].value, (int, float)) and 0.0 <= float(sheet_recalc[c.coordinate].value) <= 1.0))
                if curr_pct_cnt == 0:
                    cand_col = find_column_by_anchor(sheet_orig, "Margin") or find_column_by_anchor(sheet_orig, "Profit Margin")
                    if cand_col and all(c.column != cand_col for c in cells_to_check):
                        start_r, end_r = parse_range_rows(range_str)
                        cand_cells = [sheet_orig.cell(row=r, column=cand_col) for r in range(start_r, end_r + 1)]
                        cand_pct_cnt = sum(1 for c in cand_cells if "%" in str(c.number_format or "") or "%" in str(c.value or "") or (isinstance(sheet_recalc[c.coordinate].value, (int, float)) and 0.0 <= float(sheet_recalc[c.coordinate].value) <= 1.0))
                        if cand_pct_cnt > curr_pct_cnt:
                            cells_to_check = cand_cells
                            col_let = get_column_letter(cand_col)
                            range_str = f"{col_let}{start_r}:{col_let}{end_r}"

            # Format Column Shift Rescue for Currency (e.g. range pointed to Payment column G instead of Revenue H & Profit I)
            if req_format and str(req_format).lower() == "currency":
                curr_curr_cnt = sum(1 for c in cells_to_check if any(s in str(c.number_format or "") for s in ["$", "₹", "€", "£", "USD", "INR", "[$", "#,##", "0.00"]) or any(s in str(c.value or "") for s in ["$", "₹", "€", "£"]) or (isinstance(sheet_recalc[c.coordinate].value, (int, float)) and float(sheet_recalc[c.coordinate].value) > 1.0))
                if curr_curr_cnt < len(cells_to_check) * 0.7:
                    cand_cols = []
                    if col_map.get("revenue"):
                        cand_cols.extend(col_map["revenue"])
                    if col_map.get("profit"):
                        cand_cols.extend(col_map["profit"])
                    if not cand_cols:
                        for cand in ["Revenue", "Profit", "Sales"]:
                            c = find_column_by_anchor(sheet_orig, cand)
                            if c and c not in cand_cols:
                                cand_cols.append(c)
                    if cand_cols:
                        start_r = table_bounds.get("data_start_row", 4)
                        end_r = table_bounds.get("data_end_row", 31)
                        cand_cells = []
                        for col_idx in sorted(list(set(cand_cols))):
                            cand_cells.extend([sheet_orig.cell(row=r, column=col_idx) for r in range(start_r, end_r + 1)])
                        if cand_cells:
                            cells_to_check = cand_cells
                            range_str = f"{get_column_letter(min(cand_cols))}{start_r}:{get_column_letter(max(cand_cols))}{end_r}"

            # Group cells by row to verify header row formatting across tables
            has_bold_header_row = False
            if req_bold is True:
                # Support native Excel Table formatting
                if len(getattr(sheet_orig, "tables", {})) > 0:
                    has_bold_header_row = True

                row_cells_map = {}
                for c in cells_to_check:
                    row_cells_map.setdefault(c.row, []).append(c)
                for r_idx, r_list in row_cells_map.items():
                    non_empty = [c for c in r_list if c.value is not None and str(c.value).strip() != ""]
                    bold_cnt = sum(1 for c in non_empty if bool(c.font and c.font.bold))
                    if bold_cnt >= 3 and bold_cnt >= len(non_empty) * 0.4:
                        has_bold_header_row = True
                        break

                if not has_bold_header_row and table_bounds.get("header_row"):
                    h_r = table_bounds["header_row"]
                    h_cells = [sheet_orig.cell(row=h_r, column=c) for c in range(table_bounds["start_col"], table_bounds["end_col"] + 1)]
                    non_empty_h = [c for c in h_cells if c.value is not None and str(c.value).strip() != ""]
                    bold_cnt_h = sum(1 for c in non_empty_h if bool(c.font and c.font.bold))
                    if bold_cnt_h >= 3 and bold_cnt_h >= len(non_empty_h) * 0.4:
                        has_bold_header_row = True

            passed_cells = 0
            total_cells = len(cells_to_check)
            failures = []

            for cell in cells_to_check:
                coord = cell.coordinate
                is_empty = (cell.value is None or str(cell.value).strip() == "")

                # Bold check: if cell is an empty spacer or table has verified bold header row, allow pass
                if req_bold is not None:
                    if has_bold_header_row:
                        pass
                    elif is_empty:
                        pass
                    else:
                        is_bold = bool(cell.font and cell.font.bold)
                        if is_bold != req_bold:
                            failures.append(f"{coord}: Font bold is {is_bold}, expected {req_bold}.")
                            continue

                # Alignment check
                if req_align is not None:
                    if is_empty:
                        pass
                    else:
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
                    has_b = bool(b and (
                        (b.left and b.left.style) or
                        (b.right and b.right.style) or
                        (b.top and b.top.style) or
                        (b.bottom and b.bottom.style)
                    ))
                    if has_b != req_border:
                        failures.append(f"{coord}: Border style is {has_b}, expected {req_border}.")
                        continue

                # Empty cell check when formatting or data presence is required
                if req_format is not None or tc.get("requireValue") or tc.get("nonEmpty"):
                    if is_empty:
                        failures.append(f"{coord}: Cell is empty; expected entered data.")
                        continue

                # Number format / currency / decimal check
                if req_format is not None:
                    fmt = str(cell.number_format or "")
                    val_str = str(cell.value or "")
                    rv = sheet_recalc[coord].value
                    fmt_lower = str(req_format).lower()
                    if fmt_lower == "currency":
                        is_curr = (
                            any(sym in fmt for sym in ["$", "₹", "€", "£", "¥", "Rs", "INR", "USD", "EUR"]) or
                            "[$" in fmt or "0.00" in fmt or "#,##" in fmt or
                            any(sym in val_str for sym in ["$", "₹", "€", "£", "¥", "Rs"]) or
                            (isinstance(cell.value, (int, float)) and float(cell.value) > 1.0) or
                            (isinstance(rv, (int, float)) and float(rv) > 1.0)
                        )
                        # Phase 8: Named style fallback (clicking $ button in Excel assigns style='Currency')
                        if not is_curr:
                            cell_style_name = str(getattr(cell, 'style', '') or '').lower()
                            if any(s in cell_style_name for s in ['currency', 'accounting', 'financial', 'comma']):
                                is_curr = True
                        if not is_curr:
                            failures.append(f"{coord}: Number format '{fmt}' is not a currency format.")
                            continue
                    elif fmt_lower in ["decimal", "decimals", "0.00"]:
                        is_decimal = "0.00" in fmt or ".0" in fmt or (isinstance(cell.value, float) and round(cell.value, 2) == cell.value)
                        if not is_decimal:
                            failures.append(f"{coord}: Number format '{fmt}' is not a two-decimal format.")
                            continue
                    elif fmt_lower in ["percent", "percentage", "%"]:
                        is_pct = (
                            "%" in fmt or "%" in val_str or
                            (isinstance(cell.value, (int, float)) and 0.0 <= float(cell.value) <= 1.0) or
                            (isinstance(rv, (int, float)) and 0.0 <= float(rv) <= 1.0)
                        )
                        if not is_pct:
                            is_pct = "%" in str(sheet_recalc[coord].value or "")
                        if not is_pct:
                            failures.append(f"{coord}: Number format '{fmt}' is not a percentage format.")
                            continue

                passed_cells += 1

            tc_passed = (total_cells > 0 and (passed_cells == total_cells or (passed_cells >= total_cells * 0.85 and total_cells >= 4)))
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} satisfied formatting requirements."
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

            # ---------------------------------------------------------
            # Semantic Sub-Check A: Client & Ticker Separation
            # ---------------------------------------------------------
            is_separation_check = any(k in crit_name.lower() for k in ["client & ticker", "ticker separation", "client separation", "company & ticker", "separation"])
            if is_separation_check:
                # 1. Identify Client Column
                client_col = None
                if col_map.get("client"):
                    client_col = col_map["client"][0]
                else:
                    client_col = find_column_by_anchor(sheet_orig, "Client") or find_column_by_anchor(sheet_orig, "Company")
                if not client_col and cells_to_check:
                    client_col = min(c.column for c in cells_to_check)

                start_r = table_bounds.get("data_start_row", 4)
                end_r = table_bounds.get("data_end_row", 31)
                client_cells = [sheet_orig.cell(row=r, column=client_col) for r in range(start_r, end_r + 1)] if client_col else []
                
                # Check for merged ticker patterns inside the client column
                # e.g., "(XNAS:AMZN)", "(XNYS:ORCL)", "(AMZN)", "XNAS:", "XNYS:"
                merged_pattern = r'(\([A-Z0-9_\.\s]+:[A-Z0-9_\.]+\)|\([A-Z]{2,6}\)|XNAS:|XNYS:)'
                merged_count = 0
                sample_merged = ""
                for c in client_cells:
                    val_str = str(c.value or "").strip()
                    if re.search(merged_pattern, val_str, re.IGNORECASE):
                        merged_count += 1
                        if not sample_merged:
                            sample_merged = val_str

                # 2. Check for dedicated Ticker Column
                ticker_col = None
                if col_map.get("ticker"):
                    ticker_col = col_map["ticker"][0]
                else:
                    ticker_col = find_column_by_anchor(sheet_orig, "Ticker") or find_column_by_anchor(sheet_orig, "Tickers") or find_column_by_anchor(sheet_orig, "Symbol")
                
                if not ticker_col:
                    # Scan table columns for dedicated ticker content (e.g. AMZN, XNAS:AMZN, TSLA)
                    for c_cand in range(table_bounds.get("start_col", 1), table_bounds.get("end_col", sheet_orig.max_column or 50) + 1):
                        if c_cand == client_col:
                            continue
                        cand_vals = [str(sheet_orig.cell(row=r, column=c_cand).value or "").strip() for r in range(start_r, end_r + 1)]
                        cand_tickers = [v for v in cand_vals if re.search(r'(XNAS:|XNYS:|^[A-Z]{2,6}\)?$|:[A-Z]{2,6}\)?)', v, re.IGNORECASE)]
                        if len(cand_tickers) >= len(cand_vals) * 0.4 and len(cand_vals) >= 4:
                            ticker_col = c_cand
                            break

                tc_passed = False
                reason = ""
                if merged_count >= len(client_cells) * 0.25 and len(client_cells) > 0:
                    tc_passed = False
                    reason = f"Client column still contains unseparated ticker symbols (e.g., '{sample_merged}'). Company Name and Ticker were not separated into dedicated columns."
                elif not ticker_col:
                    tc_passed = False
                    reason = "No dedicated Ticker column found with separated stock exchange symbols."
                else:
                    tc_passed = True
                    ticker_let = get_column_letter(ticker_col)
                    client_let = get_column_letter(client_col) if client_col else "C"
                    reason = f"Verified separation: Company Names in Col {client_let} and Tickers in Col {ticker_let} are separated into dedicated columns."

                if tc_passed:
                    criterion_passes[crit_name]["passed"] += 1

                assertion_results.append({
                    "criterion": crit_name,
                    "type": tc_type,
                    "range": range_str,
                    "passed": tc_passed,
                    "reason": reason
                })
                continue

            # ---------------------------------------------------------
            # Semantic Sub-Check B: Department Categorization
            # ---------------------------------------------------------
            is_dept_check = any(k in crit_name.lower() for k in ["department", "dept"])
            if is_dept_check:
                dept_col = None
                if col_map.get("department"):
                    dept_col = col_map["department"][0]
                else:
                    dept_col = find_column_by_anchor(sheet_orig, "Department") or find_column_by_anchor(sheet_orig, "Dept")

                if not dept_col:
                    # Scan columns for department data values
                    start_r = table_bounds.get("data_start_row", 4)
                    end_r = table_bounds.get("data_end_row", 31)
                    for c_cand in range(table_bounds.get("start_col", 1), table_bounds.get("end_col", sheet_orig.max_column or 50) + 1):
                        cand_vals = [str(sheet_orig.cell(row=r, column=c_cand).value or "").strip().lower() for r in range(start_r, min(start_r + 5, end_r + 1))]
                        if any("cloud tech" in v or "texas" in v or "sales" in v or "engineering" in v for v in cand_vals):
                            dept_col = c_cand
                            break

                if not dept_col:
                    assertion_results.append({
                        "criterion": crit_name,
                        "type": tc_type,
                        "range": range_str,
                        "totalCells": 0,
                        "passedCells": 0,
                        "passed": False,
                        "reason": "Department column not found in table. Transaction records are missing required department categorization."
                    })
                    continue
                else:
                    start_r = table_bounds.get("data_start_row", 4)
                    end_r = table_bounds.get("data_end_row", 31)
                    cells_to_check = [sheet_orig.cell(row=r, column=dept_col) for r in range(start_r, end_r + 1)]
                    col_let = get_column_letter(dept_col)
                    range_str = f"{col_let}{start_r}:{col_let}{end_r}"

            # Dynamic Header / Anchor Column Resolution for general data_check
            if not is_dept_check:
                anchor_text = tc.get("columnAnchor") or tc.get("anchor")
                if not anchor_text:
                    for cand in ["Department", "Dept", "Client", "Company", "Ticker", "Date"]:
                        if cand.lower() in crit_name.lower():
                            anchor_text = cand
                            break
                if anchor_text:
                    anc_col, anc_data_start, anc_data_end = find_anchor_column_data_bounds(sheet_orig, anchor_text)
                    if anc_col and anc_data_start and anc_data_end:
                        cand_cells = [sheet_orig.cell(row=r, column=anc_col) for r in range(anc_data_start, anc_data_end + 1)]
                        if any(c.value is not None and str(c.value).strip() != "" for c in cand_cells):
                            cells_to_check = cand_cells
                            col_let = get_column_letter(anc_col)
                            range_str = f"{col_let}{anc_data_start}:{col_let}{anc_data_end}"
                    else:
                        anchor_col = find_column_by_anchor(sheet_orig, anchor_text)
                        if anchor_col:
                            start_r, end_r = parse_range_rows(range_str)
                            cand_cells = [sheet_orig.cell(row=r, column=anchor_col) for r in range(start_r, end_r + 1)]
                            if any(c.value is not None and str(c.value).strip() != "" for c in cand_cells):
                                cells_to_check = cand_cells
                                col_let = get_column_letter(anchor_col)
                                range_str = f"{col_let}{start_r}:{col_let}{end_r}"

            # Filter out completely blank spacer/margin columns (e.g., Column A blank margin)
            col_cells = {}
            for cell in cells_to_check:
                col_cells.setdefault(cell.column, []).append(cell)

            if len(col_cells) > 1:
                active_cells = []
                for c_idx, c_list in col_cells.items():
                    # If 100% of cells in this column are empty, treat as layout margin spacer
                    if all(c.value is None or str(c.value).strip() == "" for c in c_list):
                        continue
                    active_cells.extend(c_list)
                if active_cells:
                    cells_to_check = active_cells

            # Trim leading and trailing blank cells beyond the table's populated bounds
            while cells_to_check and (cells_to_check[0].value is None or str(cells_to_check[0].value).strip() == ""):
                cells_to_check.pop(0)
            while cells_to_check and (cells_to_check[-1].value is None or str(cells_to_check[-1].value).strip() == ""):
                cells_to_check.pop()

            passed_cells = 0
            total_cells = len(cells_to_check)
            failures = []

            for cell in cells_to_check:
                coord = cell.coordinate
                if cell.value is None or str(cell.value).strip() == "":
                    failures.append(f"{coord}: Missing data (cell is blank).")
                else:
                    passed_cells += 1

            tc_passed = (total_cells > 0 and (passed_cells == total_cells or (passed_cells >= total_cells * 0.85 and total_cells >= 4)))
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} contain required data."
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
            req_title_raw = str(tc.get("title", "")).strip()
            req_title = req_title_raw.lower()
            req_title_tokens = set(re.findall(r'\w+', req_title)) if req_title else set()

            def _chart_title_matches(actual_title: str) -> bool:
                if not req_title_raw:
                    return True
                if not actual_title:
                    return False
                act_lower = actual_title.lower()
                if req_title in act_lower or act_lower in req_title:
                    return True
                act_tokens = set(re.findall(r'\w+', act_lower))
                if req_title_tokens and act_tokens:
                    overlap = len(req_title_tokens & act_tokens) / len(req_title_tokens)
                    return overlap >= 0.5
                return False

            target_sheet_charts = [c for c in detected_charts if c.get("sheet") == sheet_name] if tc.get("sheet") else detected_charts
            if len(target_sheet_charts) < min_charts and detected_charts:
                # Fallback to charts found on any sheet in the workbook
                target_sheet_charts = detected_charts

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

                if tc_passed and req_title_raw:
                    title_matched = any(_chart_title_matches(c.get("title", "")) for c in target_sheet_charts)
                    if not title_matched:
                        tc_passed = False
                        found_titles = ", ".join([f"'{c.get('title')}'" for c in target_sheet_charts if c.get("title")])
                        reason = f"Expected chart title containing '{req_title_raw}', found: {found_titles or 'Untitled'}."

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
                fraction = 1.0
                needs_review = True
                assertion_results.append({
                    "criterion": c_name,
                    "type": "unmapped_rubric",
                    "passed": True,
                    "reason": f"No automated assertion mapped for '{c_name}'. Awarded full credit — instructor manual review recommended."
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
    """Read workbook paths and a JSON spec from CLI arguments, then emit a report or error as JSON."""
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
