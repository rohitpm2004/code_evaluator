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
    from workbook_registry import WorkbookObjectRegistry
except ImportError:
    try:
        from engine.workbook_registry import WorkbookObjectRegistry
    except ImportError:
        WorkbookObjectRegistry = None

try:
    import openpyxl
    from openpyxl.utils import coordinate_to_tuple, get_column_letter

    def patch_openpyxl_descriptors():
        """Monkey-patch openpyxl descriptor bug where CalculatedItem.formula or other pivot cache attributes are None."""
        try:
            from openpyxl.pivot import cache
            for item_name in dir(cache):
                item = getattr(cache, item_name, None)
                if isinstance(item, type):
                    for attr in dir(item):
                        val = getattr(item, attr, None)
                        if hasattr(val, 'allow_none'):
                            val.allow_none = True
        except Exception:
            pass
        try:
            from openpyxl.pivot import table
            for item_name in dir(table):
                item = getattr(table, item_name, None)
                if isinstance(item, type):
                    for attr in dir(item):
                        val = getattr(item, attr, None)
                        if hasattr(val, 'allow_none'):
                            val.allow_none = True
        except Exception:
            pass

    patch_openpyxl_descriptors()

    def safe_load_workbook(path: str, data_only: bool = False):
        """Safely load workbook with openpyxl, automatically recovering from pivot cache XML descriptor bugs."""
        patch_openpyxl_descriptors()
        try:
            return openpyxl.load_workbook(path, data_only=data_only)
        except Exception as first_err:
            err_msg = str(first_err)
            # If openpyxl crashes on pivot cache / CalculatedItem / formula / XML descriptor errors:
            try:
                temp_clean = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
                temp_clean_path = temp_clean.name
                temp_clean.close()
                with zipfile.ZipFile(path, 'r') as zin:
                    with zipfile.ZipFile(temp_clean_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                        for item in zin.infolist():
                            # Omit pivotCache definition and record files that crash openpyxl's parser
                            if 'xl/pivotCache/' in item.filename or 'xl/pivotTables/' in item.filename:
                                continue
                            content = zin.read(item.filename)
                            if item.filename.endswith('.rels') and (b'pivotTable' in content or b'pivotCache' in content):
                                try:
                                    r_root = ET.fromstring(content)
                                    to_remove = [r for r in r_root if 'pivotTable' in r.attrib.get('Type', '') or 'pivotCache' in r.attrib.get('Type', '')]
                                    for r in to_remove:
                                        r_root.remove(r)
                                    content = ET.tostring(r_root, encoding='utf-8')
                                except Exception:
                                    pass
                            zout.writestr(item, content)
                wb = openpyxl.load_workbook(temp_clean_path, data_only=data_only)
                try:
                    os.unlink(temp_clean_path)
                except Exception:
                    pass
                return wb
            except Exception:
                pass
            raise first_err
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
        # Pedagogical disambiguation: pick candidate that has populated data cells below it (a real table header)
        scored = []
        for r, c in matches:
            below_cnt = sum(1 for dr in range(1, 6) if sheet.cell(row=r+dr, column=c).value is not None)
            scored.append((below_cnt, (r, c)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1], None

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
        scored_fb = []
        for r, c in fallback_matches:
            below_cnt = sum(1 for dr in range(1, 6) if sheet.cell(row=r+dr, column=c).value is not None)
            scored_fb.append((below_cnt, (r, c)))
        scored_fb.sort(key=lambda x: x[0], reverse=True)
        return scored_fb[0][1], None

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
    "date": ["date", "dates", "trans_date", "transaction date", "time", "order date", "order_date", "orderdate", "year", "years", "quarter", "quarters", "month", "months"],
    "client": ["client", "clients", "company", "customer", "customers", "account", "client name", "company name", "customer name", "customername", "customer_name", "customer id", "customer_id", "customerid"],
    "ticker": ["ticker", "tickers", "symbol", "stock", "code", "xnas", "xnys"],
    "contact": ["contact", "contact name", "rep", "representative", "cleaned contact", "full name", "clean contact", "name", "sales person", "salesperson", "sales rep", "agent", "executive", "executives", "employee", "employees"],
    "department": ["department", "dept", "division", "sector", "segment"],
    "payment": ["payment", "payment method", "pay method", "pay type", "type", "sales method", "method", "channel", "sales channel"],
    "revenue": ["revenue", "revenues", "sales", "rev", "gross sales", "inflow", "amount", "net sales", "netsales", "net_sales", "total sales", "total revenue", "sales volume", "salesvolume", "sales_amount"],
    "profit": ["profit", "net profit", "gain", "earnings"], # disambiguated from profit margin
    "margin": ["profit margin", "margin", "% margin", "margin %", "profit %", "markup"],
    "product": ["product", "products", "product name", "product_name", "product id", "product_id", "productid", "item", "items", "category", "categories", "product category", "segment"],
    "region": ["region", "regions", "geography", "territory", "location", "area", "zone", "country", "countries"]
}

FUNCTION_EQUIVALENTS = {
    "VLOOKUP": ["VLOOKUP", "XLOOKUP", "INDEX", "MATCH", "LOOKUP", "FILTER"],
    "XLOOKUP": ["XLOOKUP", "VLOOKUP", "INDEX", "MATCH", "LOOKUP"],
    "HLOOKUP": ["HLOOKUP", "XLOOKUP", "INDEX", "MATCH"],
    "IF": ["IF", "IFS", "SWITCH", "CHOOSE"],
    "IFS": ["IFS", "IF", "SWITCH"],
    "CONCATENATE": ["CONCATENATE", "CONCAT", "TEXTJOIN"],
    "CONCAT": ["CONCAT", "CONCATENATE", "TEXTJOIN"],
    "TEXTJOIN": ["TEXTJOIN", "CONCAT", "CONCATENATE"],
    "SUM": ["SUM", "SUMIF", "SUMIFS"],
    "AVERAGE": ["AVERAGE", "AVERAGEIF", "AVERAGEIFS"],
    "COUNT": ["COUNT", "COUNTA", "COUNTIF", "COUNTIFS"],
    "COUNTIF": ["COUNTIF", "COUNTIFS"],
    "COUNTIFS": ["COUNTIFS", "COUNTIF"],
    "SUMIF": ["SUMIF", "SUMIFS"],
    "SUMIFS": ["SUMIFS", "SUMIF"]
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
    "employees":        ["emp", "staff", "people", "workforce", "hr", "employee data", "executives"],
    "products":         ["items", "inventory", "catalog", "product list", "products list"],
    "customers":        ["clients", "accounts", "contacts", "customer list", "customer data"],
    "financials":       ["finance", "financial data", "p&l", "income", "revenue"],
    "repository":       ["relationships toolbar", "data model", "model", "sales", "raw data",
                         "data", "sheet1", "sheet2", "sheet3", "power query", "power pivot"],
    "pivot table":      ["pivot", "pivottable", "executive sales", "summary", "sheet1", "sheet2", "sheet3"],
    "data model":       ["relationships toolbar", "power pivot", "power query", "model", "sheet1", "sheet2", "sheet3"],
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

def resolve_sheet(wb, target_name: Optional[str] = None, anchor_hint: Optional[str] = None, fallback_to_default: bool = True) -> Optional[str]:
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

    # If anchor_hint is provided, prioritize sheet containing that anchor
    if anchor_hint:
        # Check if requested sheet directly contains anchor
        if raw_target in wb.sheetnames:
            coords, _ = find_anchor_cell(wb[raw_target], anchor_hint)
            if coords:
                return raw_target
        # Check all sheets for anchor
        for s in wb.sheetnames:
            coords, _ = find_anchor_cell(wb[s], anchor_hint)
            if coords:
                return s

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

    # T6: First sheet with real data (skip blank cover pages) if fallback allowed
    if not fallback_to_default:
        return None

    data_sheets = [s for s in wb.sheetnames if _sheet_has_numeric_data(wb[s])]
    if data_sheets:
        return data_sheets[0]

    return wb.sheetnames[0]

def normalize_field_name(raw_name: str) -> List[str]:
    """Generates standard variants of a field name (handling OLAP [Table].[Col] and underscores)."""
    if not raw_name:
        return []
    variants = [raw_name]
    clean = re.sub(r'[\[\]]', '', raw_name).strip()
    if clean != raw_name:
        variants.append(clean)
    if '.' in clean:
        parts = clean.split('.')
        variants.append(parts[-1].strip())
    if '_' in clean:
        variants.append(clean.replace('_', ' '))
    return list(dict.fromkeys(v for v in variants if v))

def is_single_edit_typo(val: str, exp: str) -> bool:
    """Checks if val and exp differ by at most one single edit (insertion, deletion, substitution, or adjacent swap).
    Strictly restricted to entity/name strings:
    - Never applied if either contains digits (prevents 'Q1 2024' vs 'Q1 2025' or 'Product A12' vs 'Product A21').
    - Never applied to short strings (< 4 chars).
    - Prevents opposite compass directions like 'North-East' vs 'North-West'.
    """
    if not val or not exp:
        return False
    s1 = str(val).strip().lower()
    s2 = str(exp).strip().lower()
    if s1 == s2:
        return True
    c1 = re.sub(r'[^a-z0-9]', '', s1)
    c2 = re.sub(r'[^a-z0-9]', '', s2)
    if c1 == c2:
        return True
    # Never apply if digits are present in either string
    if any(c.isdigit() for c in c1) or any(c.isdigit() for c in c2):
        return False
    # Never apply to short strings
    if len(c1) < 4 or len(c2) < 4:
        return False
    # Length difference must be <= 1
    if abs(len(c1) - len(c2)) > 1:
        return False

    # Check for adjacent transposition or single substitution
    if len(c1) == len(c2):
        diffs = [i for i in range(len(c1)) if c1[i] != c2[i]]
        if len(diffs) == 1:
            return True  # 1 substitution (e.g. "smith" vs "smyth")
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            if c1[diffs[0]] == c2[diffs[1]] and c1[diffs[1]] == c2[diffs[0]]:
                return True  # Single adjacent transposition (e.g. "acmecrop" vs "acmecorp")
        return False

    # Check for single insertion / deletion
    short, long_ = (c1, c2) if len(c1) < len(c2) else (c2, c1)
    i = j = 0
    mismatches = 0
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
        else:
            mismatches += 1
            j += 1
            if mismatches > 1:
                return False
    return True

def detect_cell_based_pivots(wb) -> List[Dict[str, Any]]:
    """Detects pivot tables rendered directly into worksheet cells (e.g. Google Sheets / LibreOffice / Web Excel exports)."""
    detected = []
    try:
        if not wb:
            return detected

        # 1. Index candidate source data sheets (sheets with table headers and row records)
        sheet_columns = {}
        for s_name in wb.sheetnames:
            ws = wb[s_name]
            if ws.max_row < 2:
                continue
            for r in range(1, min(10, ws.max_row + 1)):
                headers = {}
                for c in range(1, min(50, ws.max_column + 1)):
                    val = ws.cell(r, c).value
                    if val and isinstance(val, str) and str(val).strip():
                        headers[c] = str(val).strip()
                if len(headers) >= 3:
                    col_samples = {}
                    for c, h_name in headers.items():
                        samples = set()
                        for sr in range(r + 1, min(r + 100, ws.max_row + 1)):
                            sval = ws.cell(sr, c).value
                            if sval is not None:
                                samples.add(str(sval).strip().lower())
                        col_samples[c] = {"header": h_name, "samples": samples}
                    sheet_columns[s_name] = col_samples
                    break

        # 2. Inspect each sheet for pivot table signatures
        for s_name in wb.sheetnames:
            ws = wb[s_name]
            if ws.max_row < 2:
                continue

            has_row_labels = False
            has_col_labels = False
            has_grand_total = False
            has_agg_header = False

            row_labels_coord = None
            col_labels_coord = None
            grand_total_coords = []
            agg_headers = []

            max_scan_r = min(100, ws.max_row + 1)
            max_scan_c = min(100, ws.max_column + 1)

            for r in range(1, max_scan_r):
                for c in range(1, max_scan_c):
                    val = ws.cell(r, c).value
                    if not val:
                        continue
                    sval = str(val).strip().lower()
                    if "row label" in sval:
                        has_row_labels = True
                        row_labels_coord = (r, c)
                    elif "column label" in sval:
                        has_col_labels = True
                        col_labels_coord = (r, c)
                    elif "grand total" in sval or sval == "total":
                        has_grand_total = True
                        grand_total_coords.append((r, c))

                    m = re.match(r'^(sum|count|average|avg|min|max)\s*(?:of|-|:)?\s*(.+)$', sval, re.I)
                    if m and len(m.group(2).strip()) > 1:
                        has_agg_header = True
                        agg_headers.append((r, c, str(val).strip(), m.group(1).lower(), m.group(2).strip()))

            is_pivot = (has_row_labels or has_grand_total) and (has_agg_header or has_col_labels)
            if not is_pivot and has_grand_total and (len(grand_total_coords) >= 1 and (has_row_labels or has_col_labels or has_agg_header)):
                is_pivot = True

            if is_pivot:
                row_items = set()
                if row_labels_coord:
                    rl_r, rl_c = row_labels_coord
                    for r in range(rl_r + 1, min(ws.max_row + 1, rl_r + 200)):
                        v = ws.cell(r, rl_c).value
                        if v is not None:
                            sv = str(v).strip()
                            if "total" in sv.lower():
                                break
                            if sv:
                                row_items.add(sv.lower())

                col_items = set()
                if col_labels_coord:
                    cl_r, cl_c = col_labels_coord
                    for c in range(cl_c, min(ws.max_column + 1, cl_c + 200)):
                        v = ws.cell(cl_r + 1, c).value
                        if v is not None:
                            sv = str(v).strip()
                            if "total" in sv.lower():
                                continue
                            if sv and not re.match(r'^(sum|count|average|avg|min|max)\s*(?:of|-|:)?', sv, re.I):
                                col_items.add(sv.lower())

                detected_row_fields = []
                detected_col_fields = []
                source_sheet = None
                source_fields = []

                for other_sheet, cols in sheet_columns.items():
                    if other_sheet == s_name:
                        continue
                    for c_idx, c_info in cols.items():
                        h = c_info["header"]
                        samples = c_info["samples"]
                        source_fields.append(h)

                        if row_items and len(row_items & samples) >= min(2, len(row_items)):
                            if h not in detected_row_fields:
                                detected_row_fields.append(h)
                            source_sheet = other_sheet
                        if col_items and len(col_items & samples) >= min(2, len(col_items)):
                            if h not in detected_col_fields:
                                detected_col_fields.append(h)
                            source_sheet = other_sheet

                if not detected_row_fields and row_items:
                    for f in source_fields:
                        if any(term in f.lower() for term in ["person", "rep", "agent", "name", "sales", "region", "segment", "date", "year", "quarter"]):
                            if f not in detected_row_fields:
                                detected_row_fields.append(f)
                if not detected_col_fields and col_items:
                    for f in source_fields:
                        if any(term in f.lower() for term in ["product", "item", "category", "type", "region", "segment"]):
                            if f not in detected_col_fields:
                                detected_col_fields.append(f)

                data_fields = []
                seen_df = set()
                for r, c, full_text, func, src_field in agg_headers:
                    if full_text.lower() not in seen_df:
                        seen_df.add(full_text.lower())
                        matched_src = src_field
                        for sf in source_fields:
                            if sf.lower() == src_field.lower() or src_field.lower() in sf.lower() or sf.lower() in src_field.lower():
                                matched_src = sf
                                break

                        data_fields.append({
                            "name": full_text,
                            "sourceField": matched_src,
                            "function": func,
                            "subtotal": func
                        })

                row_fields = detected_row_fields
                col_fields = detected_col_fields

                active_fields = list(dict.fromkeys(
                    row_fields + col_fields +
                    [df["name"] for df in data_fields] +
                    [df["sourceField"] for df in data_fields]
                ))
                all_fields = list(dict.fromkeys(active_fields + source_fields))

                loc_start = f"A{row_labels_coord[0] if row_labels_coord else 1}"
                pivot_dict = {
                    "name": f"PivotTable_{s_name}",
                    "sheet": s_name,
                    "location": f"{loc_start}:Z{ws.max_row}",
                    "location_ref": f"{loc_start}:Z{ws.max_row}",
                    "rowFields": row_fields,
                    "colFields": col_fields,
                    "dataFields": data_fields,
                    "activeFields": active_fields,
                    "fields": all_fields,
                    "source_sheet": source_sheet or "Source",
                    "is_cell_rendered": True
                }
                detected.append(pivot_dict)

                # If there are multiple Grand Totals or multiple pivot blocks on the same sheet, index the 2nd pivot
                if len(grand_total_coords) >= 2:
                    gt2_r, gt2_c = grand_total_coords[1]
                    p2 = dict(pivot_dict)
                    p2["name"] = f"PivotTable_{s_name}_2"
                    p2["location"] = f"A{gt2_r}:Z{ws.max_row}"
                    p2["location_ref"] = f"A{gt2_r}:Z{ws.max_row}"
                    detected.append(p2)
    except Exception:
        pass
    return detected

def inspect_openxml_pivots(xlsx_path: str, detection_errors: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Inspects native Excel Pivot Tables and PivotCaches with per-pivot sheet and field resolution."""
    pivots = []
    try:
        with zipfile.ZipFile(xlsx_path, 'r') as z:
            names = z.namelist()

            # 1. Map worksheet XML paths to worksheet human names
            sheet_file_to_name = {}
            if 'xl/workbook.xml' in names and 'xl/_rels/workbook.xml.rels' in names:
                try:
                    wb_rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
                    rid_to_target = {r.attrib.get('Id'): r.attrib.get('Target') for r in wb_rels}
                    wb_root = ET.fromstring(z.read('xl/workbook.xml'))
                    for s in wb_root.iter():
                        if s.tag.endswith('sheet'):
                            rid = s.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                            tgt = rid_to_target.get(rid)
                            if tgt:
                                norm_tgt = 'xl/' + tgt.lstrip('/').replace('\\', '/')
                                sheet_file_to_name[norm_tgt] = s.attrib.get('name')
                except Exception as e:
                    if detection_errors is not None:
                        detection_errors.append(f"Pivot workbook rels inspection: {type(e).__name__}: {e}")

            # 2. Map sheet files to their pivot tables
            pt_to_sheet = {}
            for n in names:
                if n.startswith('xl/worksheets/_rels/sheet') and n.endswith('.xml.rels'):
                    sheet_xml = n.replace('/_rels/', '/').replace('.xml.rels', '.xml')
                    sheet_display_name = sheet_file_to_name.get(sheet_xml, '')
                    try:
                        s_rels = ET.fromstring(z.read(n))
                        for r in s_rels:
                            tgt = r.attrib.get('Target', '')
                            if 'pivotTable' in tgt:
                                pt_file = 'xl/' + tgt.replace('../', '').lstrip('/')
                                pt_to_sheet[pt_file] = sheet_display_name
                    except Exception:
                        pass

            # 3. Parse pivotCacheDefinitions (standard + OLAP / Data Model)
            cache_definitions = {} # cache_file -> list of cache field names
            for fname in names:
                if fname.startswith('xl/pivotCache/pivotCacheDefinition') and fname.endswith('.xml'):
                    try:
                        c_bytes = z.read(fname)
                        c_root = ET.fromstring(c_bytes)
                        c_fields = []
                        for elem in c_root.iter():
                            tag = elem.tag.split('}')[-1]
                            if tag in ('cacheField', 'dimension', 'hierarchy', 'level', 'measure', 'kpi'):
                                cf_name = elem.attrib.get('name') or elem.attrib.get('uniqueName') or elem.attrib.get('caption')
                                if cf_name and cf_name not in c_fields:
                                    c_fields.append(cf_name)
                        cache_definitions[fname] = c_fields
                    except Exception:
                        pass

            # 4. Map each pivotTable to its specific cacheDefinition
            pt_to_cache = {}
            for fname in names:
                if fname.startswith('xl/pivotTables/_rels/pivotTable') and fname.endswith('.xml.rels'):
                    pt_file = fname.replace('/_rels/', '/').replace('.xml.rels', '.xml')
                    try:
                        pt_rels = ET.fromstring(z.read(fname))
                        for r in pt_rels:
                            tgt = r.attrib.get('Target', '')
                            if 'pivotCache' in tgt:
                                cache_file = 'xl/' + tgt.replace('../', '').lstrip('/')
                                pt_to_cache[pt_file] = cache_file
                    except Exception:
                        pass

            # 5. Parse each pivotTable
            for fname in names:
                if fname.startswith('xl/pivotTables/pivotTable') and fname.endswith('.xml'):
                    try:
                        content = z.read(fname)
                        root = ET.fromstring(content)
                        p_name = root.attrib.get('name', '')
                        sheet_name = pt_to_sheet.get(fname, '')
                        loc_ref = ''

                        cache_file = pt_to_cache.get(fname, '')
                        cache_fields = cache_definitions.get(cache_file, [])
                        if not cache_fields and cache_definitions:
                            cache_fields = next(iter(cache_definitions.values()))

                        pivot_fields = []
                        for child in root:
                            tag = child.tag.split('}')[-1]
                            if tag == 'location':
                                loc_ref = child.attrib.get('ref', '')
                            elif tag == 'pivotFields':
                                for idx, pf in enumerate(child):
                                    pf_override = pf.attrib.get('name')
                                    base_name = pf_override or (cache_fields[idx] if idx < len(cache_fields) else f"Field_{idx}")
                                    pivot_fields.append({
                                        "name": base_name,
                                        "axis": pf.attrib.get('axis'),
                                        "dataField": pf.attrib.get('dataField') == '1'
                                    })

                        if not pivot_fields and cache_fields:
                            pivot_fields = [{"name": cf, "axis": None, "dataField": False} for cf in cache_fields]

                        row_fields = []
                        col_fields = []
                        data_fields = []

                        for child in root:
                            tag = child.tag.split('}')[-1]
                            if tag == 'rowFields':
                                for f in child:
                                    x = f.attrib.get('x')
                                    if x is not None and x.lstrip('-').isdigit():
                                        x_int = int(x)
                                        if 0 <= x_int < len(pivot_fields):
                                            f_name = pivot_fields[x_int]["name"]
                                            row_fields.extend(normalize_field_name(f_name))
                            elif tag == 'colFields':
                                for f in child:
                                    x = f.attrib.get('x')
                                    if x is not None and x.lstrip('-').isdigit():
                                        x_int = int(x)
                                        if 0 <= x_int < len(pivot_fields):
                                            f_name = pivot_fields[x_int]["name"]
                                            col_fields.extend(normalize_field_name(f_name))
                            elif tag == 'dataFields':
                                for df in child:
                                    df_name = df.attrib.get('name', '')
                                    df_sub = df.attrib.get('subtotal', 'sum')
                                    fld = df.attrib.get('fld')
                                    src_name = ''
                                    if fld is not None and fld.isdigit():
                                        fld_int = int(fld)
                                        if 0 <= fld_int < len(pivot_fields):
                                            src_name = pivot_fields[fld_int]["name"]
                                    data_fields.append({
                                        "name": df_name,
                                        "function": df_sub,
                                        "sourceField": src_name,
                                        "normalizedNames": normalize_field_name(df_name) + normalize_field_name(src_name)
                                    })

                        # Fallback for axis attribute
                        if not row_fields:
                            for pf in pivot_fields:
                                if pf.get("axis") == 'axisRow':
                                    row_fields.extend(normalize_field_name(pf["name"]))
                        if not col_fields:
                            for pf in pivot_fields:
                                if pf.get("axis") == 'axisCol':
                                    col_fields.extend(normalize_field_name(pf["name"]))

                        active_fields = list(dict.fromkeys(row_fields + col_fields))
                        for df in data_fields:
                            active_fields.extend(df.get("normalizedNames", []))
                        active_fields = list(dict.fromkeys(active_fields))

                        all_cache_norm = []
                        for cf in cache_fields:
                            all_cache_norm.extend(normalize_field_name(cf))
                        all_fields = list(dict.fromkeys(active_fields + all_cache_norm))

                        pivots.append({
                            "file": fname,
                            "name": p_name,
                            "sheet": sheet_name,
                            "location": loc_ref,
                            "rowFields": row_fields,
                            "colFields": col_fields,
                            "dataFields": data_fields,
                            "activeFields": active_fields,
                            "fields": all_fields
                        })
                    except Exception as e:
                        if detection_errors is not None:
                            detection_errors.append(f"Pivot parse error on {fname}: {type(e).__name__}: {e}")
    except Exception as e:
        if detection_errors is not None:
            detection_errors.append(f"Zip extraction error for pivots: {type(e).__name__}: {e}")
    try:
        wb_lazy = safe_load_workbook(xlsx_path, data_only=True)
        cell_pivots = detect_cell_based_pivots(wb_lazy)
        if cell_pivots:
            existing_sheets = {p.get("sheet") for p in pivots if p.get("sheet")}
            for cp in cell_pivots:
                if cp.get("sheet") not in existing_sheets:
                    pivots.append(cp)
    except Exception as e:
        if detection_errors is not None:
            detection_errors.append(f"Cell-based pivot detection error: {type(e).__name__}: {e}")
    return pivots

def inspect_openxml_macros_and_queries(xlsx_path: str) -> Dict[str, Any]:
    """Inspects OpenXML archive for VBA macros, Power Query connections, M-code, and query tables."""
    info = {
        "has_macros": False,
        "macro_names": [],
        "has_power_query": False,
        "query_names": [],
        "m_steps": {},
        "query_tables": []
    }
    try:
        with zipfile.ZipFile(xlsx_path, 'r') as z:
            names = z.namelist()

            # 1. VBA Macros
            if any('vbaProject.bin' in n for n in names):
                info["has_macros"] = True
                try:
                    vba_bytes = z.read('xl/vbaProject.bin')
                    matches = re.findall(rb'(?:Sub|Function)\s+([a-zA-Z0-9_]+)', vba_bytes)
                    info["macro_names"] = sorted(list(set([m.decode('latin1', errors='ignore') for m in matches])))
                except Exception:
                    pass

            # 2. Worksheet mapping for tables
            sheet_file_to_name = {}
            if 'xl/workbook.xml' in names and 'xl/_rels/workbook.xml.rels' in names:
                try:
                    wb_rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
                    rid_to_target = {r.attrib.get('Id'): r.attrib.get('Target') for r in wb_rels}
                    wb_root = ET.fromstring(z.read('xl/workbook.xml'))
                    for s in wb_root.iter():
                        if s.tag.endswith('sheet'):
                            rid = s.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                            tgt = rid_to_target.get(rid)
                            if tgt:
                                norm_tgt = 'xl/' + tgt.lstrip('/').replace('\\', '/')
                                sheet_file_to_name[norm_tgt] = s.attrib.get('name')
                except Exception:
                    pass

            table_to_sheet = {}
            for n in names:
                if n.startswith('xl/worksheets/_rels/sheet') and n.endswith('.xml.rels'):
                    sheet_xml = n.replace('/_rels/', '/').replace('.xml.rels', '.xml')
                    sheet_display_name = sheet_file_to_name.get(sheet_xml, '')
                    try:
                        s_rels = ET.fromstring(z.read(n))
                        for r in s_rels:
                            tgt = r.attrib.get('Target', '')
                            if 'table' in tgt.lower():
                                tbl_file = 'xl/' + tgt.replace('../', '').lstrip('/')
                                table_to_sheet[tbl_file] = sheet_display_name
                    except Exception:
                        pass

            # 3. Power Query Connections
            if any(n.startswith('xl/queryTables/') or 'connections.xml' in n or 'customXml/' in n for n in names):
                for n in names:
                    if 'connections.xml' in n:
                        try:
                            c_bytes = z.read(n)
                            c_root = ET.fromstring(c_bytes)
                            for elem in c_root.iter():
                                if elem.tag.endswith('connection'):
                                    c_name = elem.attrib.get('name', '')
                                    c_type = elem.attrib.get('type', '')
                                    if 'query' in c_name.lower() or c_type == '5':
                                        info["has_power_query"] = True
                                        info["query_names"].append(c_name)
                        except Exception:
                            pass

            # 4. M-code / DataMashup check in customXml
            for n in names:
                if n.startswith('customXml/') and n.endswith('.xml'):
                    try:
                        raw_cxml = z.read(n)
                        # Check decoded text and raw binary
                        text_cxml = raw_cxml.decode('utf-16', errors='ignore') if raw_cxml.startswith(b'\xff\xfe') else raw_cxml.decode('utf-8', errors='ignore')
                        if 'DataMashup' in text_cxml or 'DataMashup' in str(raw_cxml):
                            info["has_power_query"] = True
                            if 'Section1' in text_cxml or b'Section1' in raw_cxml:
                                info["has_power_query"] = True
                    except Exception:
                        pass

            # 5. Query Tables (Close & Load outputs)
            for n in names:
                if n.startswith('xl/tables/table') and n.endswith('.xml'):
                    try:
                        t_bytes = z.read(n)
                        t_root = ET.fromstring(t_bytes)
                        t_type = t_root.attrib.get('tableType', '')
                        t_name = t_root.attrib.get('displayName') or t_root.attrib.get('name', '')
                        t_ref = t_root.attrib.get('ref', '')
                        sheet_name = table_to_sheet.get(n, '')

                        cols = []
                        for col in t_root.iter():
                            if col.tag.endswith('tableColumn'):
                                cname = col.attrib.get('name', '')
                                if cname:
                                    cols.append(cname)

                        norm_cols = []
                        for c in cols:
                            norm_cols.extend(normalize_field_name(c))
                        norm_cols = list(dict.fromkeys(norm_cols))

                        is_pq_table = (t_type == 'queryTable') or any('query' in q.lower() and t_name.lower() in q.lower() for q in info["query_names"])
                        if is_pq_table:
                            info["has_power_query"] = True

                        info["query_tables"].append({
                            "name": t_name,
                            "sheet": sheet_name,
                            "ref": t_ref,
                            "type": t_type,
                            "columns": cols,
                            "normalizedColumns": norm_cols,
                            "isQueryTable": is_pq_table
                        })
                    except Exception:
                        pass
    except Exception:
        pass
    return info

def merge_macro_info(base_info: Dict[str, Any], registry_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Cleanly merges macro and Power Query detection facts from OpenXML inspection and WorkbookObjectRegistry."""
    merged = dict(base_info or {})
    if not registry_info:
        return merged
    for k, v in registry_info.items():
        if v and not merged.get(k):
            merged[k] = v
    if registry_info.get("has_power_query"):
        merged["has_power_query"] = True
    if registry_info.get("has_macros"):
        merged["has_macros"] = True
    if registry_info.get("has_data_model"):
        merged["has_data_model"] = True
    for q in registry_info.get("query_names", []):
        if q not in merged.setdefault("query_names", []):
            merged["query_names"].append(q)
    for qt in registry_info.get("query_tables", []):
        if not any(t.get("name") == qt.get("name") for t in merged.setdefault("query_tables", [])):
            merged.setdefault("query_tables", []).append(qt)
    return merged

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
        """Detect chart type from first recognized chart tag, checking barDir child element for column charts."""
        for elem in root.iter():
            tag = elem.tag.split('}')[-1]
            if tag in CHART_TYPE_TAGS:
                if tag in ('barChart', 'bar3DChart'):
                    bar_dir = 'col'
                    for child in elem.iter():
                        c_tag = child.tag.split('}')[-1]
                        if c_tag == 'barDir':
                            bar_dir = (child.attrib.get('val') or child.attrib.get('barDir') or 'col').lower()
                            break
                    return 'ColumnChart' if bar_dir in ('col', 'column') else 'BarChart'
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
                    num_refs = []
                    cat_refs = []
                    for node in root.iter():
                        t_tag = node.tag.split("}")[-1]
                        if t_tag in ["numRef", "strRef"]:
                            f_node = node.find(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}f")
                            if f_node is not None and f_node.text:
                                ref_str = f_node.text.strip()
                                if t_tag == "numRef":
                                    num_refs.append(ref_str)
                                else:
                                    cat_refs.append(ref_str)
                    charts.append({
                        'type': chart_type,
                        'title': chart_title,
                        'seriesCount': series_count,
                        'sheet': sheet_ref,
                        'num_refs': num_refs,
                        'cat_refs': cat_refs,
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
    wb_orig = safe_load_workbook(original_path, data_only=False)
    wb_cached = safe_load_workbook(original_path, data_only=True)
    wb_recalc = safe_load_workbook(recalculated_path, data_only=True)

    defined_name_set = set()
    try:
        if hasattr(wb_orig, 'defined_names') and wb_orig.defined_names:
            for dn in wb_orig.defined_names.definedName:
                if dn.name:
                    defined_name_set.add(dn.name)
    except Exception:
        pass

    rubric = spec.get("rubric", {})
    criteria = rubric if isinstance(rubric, list) else rubric.get("criteria", [])
    test_cases = spec.get("testCases", [])

    criterion_weights = {}
    total_rubric_weight = sum(float(c.get("weight", 0)) for c in criteria)

    # ── SPRINT 4/6: Preserve original teacher rubric scale without silent 100-point inflation ──
    orig_max_score = None
    if isinstance(rubric, dict) and rubric.get("original_max_score") is not None:
        orig_max_score = float(rubric["original_max_score"])
    preserve_scale = bool(isinstance(rubric, dict) and rubric.get("preserve_original_marks", orig_max_score is not None))

    if not preserve_scale:
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
    detection_errors = []
    needs_review = False

    default_sheet_name = wb_orig.sheetnames[0]

    # Universal Workbook Object Registry (Sprint 2)
    registry = None
    if WorkbookObjectRegistry:
        try:
            registry = WorkbookObjectRegistry(original_path, recalculated_path)
        except Exception as reg_err:
            calculation_warnings.append(f"Registry notice: {str(reg_err)}")

    # Global inspections for Pivots, Macros, and Power Query
    detected_pivots = registry.pivot_tables if registry else inspect_openxml_pivots(original_path, detection_errors)
    if not detected_pivots:
        try:
            detected_pivots = detect_cell_based_pivots(wb_cached or wb_orig)
            if registry:
                registry.pivot_tables = detected_pivots
        except Exception as e:
            detection_errors.append(f"Fallback cell pivot detection error: {type(e).__name__}: {e}")
    raw_macro_info = inspect_openxml_macros_and_queries(original_path)
    macro_info = merge_macro_info(raw_macro_info, registry.macro_info if registry else None)
    detected_charts = [dict(c) for c in registry.charts] if (registry and registry.charts) else []

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
            if ch_type == 'BarChart':
                bar_type = getattr(ch, 'type', 'col')
                if bar_type in ('col', 'column'):
                    ch_type = 'ColumnChart'
                else:
                    ch_type = 'BarChart'
            ch_title = ""
            if hasattr(ch, 'title'):
                if isinstance(ch.title, str):
                    ch_title = ch.title
                elif hasattr(ch.title, 'text'):
                    ch_title = str(ch.title.text or '')
                else:
                    ch_title = str(ch.title or '')
            series_count = len(getattr(ch, 'series', []) or [])
            num_refs = []
            cat_refs = []
            for s in getattr(ch, 'series', []) or []:
                if hasattr(s, 'values') and s.values:
                    num_refs.append(str(getattr(s.values, 'loc', '') or getattr(s.values, 'title', '') or str(s.values)))
                if hasattr(s, 'categories') and s.categories:
                    cat_refs.append(str(getattr(s.categories, 'loc', '') or getattr(s.categories, 'title', '') or str(s.categories)))

            existing = any(c.get("title") == ch_title.strip() and c.get("sheet") == s_name for c in detected_charts)
            if not existing:
                detected_charts.append({
                    "sheet": s_name,
                    "type": ch_type,
                    "title": ch_title.strip(),
                    "seriesCount": series_count,
                    "series_count": series_count,
                    "num_refs": num_refs,
                    "cat_refs": cat_refs
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

    for idx, tc in enumerate(test_cases):
        reason = ""
        crit_name = tc.get("criterion", criteria[0].get("name") if criteria else "Standard Evaluation")
        if crit_name not in criterion_passes:
            criterion_passes[crit_name] = {"total": 0, "passed": 0}
        criterion_passes[crit_name]["total"] += 1

        tc_id = tc.get("id") or f"{crit_name}_{idx}"
        tc_weight = float(tc.get("weight", 1.0))
        tc_is_critical = bool(tc.get("is_critical", tc.get("is_prerequisite", False)))
        tc_score_cap = tc.get("score_cap")
        tc_depends_on = tc.get("depends_on", [])
        tc_dep_mode = tc.get("dependency_mode", "skip_penalty")

        tc_type = tc.get("type", "value_check")

        # Check if test case is explicitly marked unsupported or requires manual review
        if tc.get("status") in ["UNSUPPORTED_FEATURE", "MANUAL_REVIEW_REQUIRED"]:
            needs_review = True
            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "passed": None,
                "status": tc.get("status"),
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": f"Assertion explicitly flagged as '{tc.get('status')}' — deferred for manual review."
            })
            continue

        # Use intelligent 4-tier fuzzy sheet name resolver
        target_sheet_req = tc.get("sheet")
        strict_sheet = bool(tc.get("strict_sheet", tc.get("require_sheet", False)))
        sheet_name = resolve_sheet(wb_orig, target_sheet_req, anchor_hint=tc.get("anchor"), fallback_to_default=not strict_sheet)
        if not sheet_name or sheet_name not in wb_orig.sheetnames:
            # Global structural checks (Pivots, Macros) inspect the OpenXML package and do not require a specific sheet
            if tc_type in ["pivot_check", "pivot_table_check", "macro_check", "vba_check"] and wb_orig.sheetnames:
                sheet_name = wb_orig.sheetnames[0]
            else:
                assertion_results.append({
                    "id": tc.get("id", f"{crit_name}_{len(assertion_results)}"),
                    "criterion": crit_name,
                    "type": tc_type,
                    "weight": tc_weight,
                    "passed": False,
                    "status": "VERIFIED_FAIL",
                    "reason": f"Sheet '{target_sheet_req or 'default'}' not found in workbook.",
                    "target": target_sheet_req
                })
                continue

        if not sheet_name or sheet_name not in wb_orig.sheetnames:
            continue

        sheet_orig = wb_orig[sheet_name]
        sheet_cached = wb_cached[sheet_name] if (wb_cached and sheet_name in wb_cached.sheetnames) else None
        sheet_recalc = wb_recalc[sheet_name] if sheet_name in wb_recalc.sheetnames else (wb_recalc.active or sheet_orig)
        table_bounds = detect_table_bounds(sheet_orig)
        col_map = table_bounds.get("col_map", {})

        # -------------------------------------------------------------
        # TYPE 1: Value Check (Explicit Cell or Anchor-Based)
        # -------------------------------------------------------------
        if tc_type in ["value_check", "calculated_value"]:
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
                # If Power Query assignment, verify via genuine Power Query M-code / connections / query tables
                is_pq_crit = any(k in crit_name.lower() for k in ["power query", "query merge", "column expansion", "close & load"])
                has_actual_pq = bool(
                    (registry is not None and (registry.macro_info.get("has_power_query") or len(registry.power_queries) > 0))
                    or macro_info.get("has_power_query")
                )
                if is_pq_crit and has_actual_pq:
                    # Require actual matching Power Query M-code or loaded query table
                    matched_pq = False
                    if registry and registry.power_queries:
                        for pq in registry.power_queries:
                            q_name = str(pq.get("name", "")).lower()
                            q_code = str(pq.get("m_code", "")).lower()
                            if any(k in q_name or k in q_code for k in ["merge", "expand", "clean", "type", "transform", "table"]):
                                matched_pq = True
                                break
                    elif has_actual_pq:
                        matched_pq = True

                    if matched_pq:
                        criterion_passes[crit_name]["passed"] += 1
                        assertion_results.append({
                            "id": tc_id,
                            "criterion": crit_name,
                            "type": tc_type,
                            "passed": True,
                            "status": "VERIFIED_PASS",
                            "weight": tc_weight,
                            "is_critical": tc_is_critical,
                            "score_cap": tc_score_cap,
                            "depends_on": tc_depends_on,
                            "dependency_mode": tc_dep_mode,
                            "reason": f"Verified Power Query transformation pipeline for '{crit_name}'."
                        })
                        continue

                assertion_results.append({
                    "criterion": crit_name,
                    "type": tc_type,
                    "passed": False,
                    "reason": "Neither explicit cell nor anchor specified in test case, and no matching Power Query pipeline verified."
                })
                continue

            cell_val = sheet_recalc[target_cell_coord].value
            cached_val = sheet_cached[target_cell_coord].value if sheet_cached else None
            formula_expr = sheet_orig[target_cell_coord].value
            actual_num = parse_number(cell_val)
            expected_num = parse_number(tc.get("expectedValue"))
            abs_tol = float(tc.get("absTolerance", tc.get("tolerance", 0.05)))
            rel_tol = float(tc.get("relTolerance", 0.001))

            # Stale formula cache & divergence classification (Amendment 7 / Check 3)
            if cached_val is not None and cell_val is not None and isinstance(formula_expr, str) and formula_expr.startswith("="):
                num_c = parse_number(cached_val)
                num_r = parse_number(cell_val)
                if num_c is not None and num_r is not None and not check_dual_tolerance(num_c, num_r, abs_tol, rel_tol):
                    f_upper = formula_expr.upper()
                    is_volatile = any(vf in f_upper for vf in ["NOW(", "TODAY(", "RAND(", "RANDBETWEEN(", "OFFSET(", "INDIRECT("])
                    if is_volatile:
                        calculation_warnings.append(
                            f"CACHE_DIVERGENCE_VOLATILE in {sheet_name}!{target_cell_coord}: saved value '{num_c}' differs from recalculated value '{num_r}' due to volatile function ({formula_expr}). Normal behavior."
                        )
                    else:
                        calculation_warnings.append(
                            f"CACHE_DIVERGENCE_STATIC in {sheet_name}!{target_cell_coord}: student saved '{num_c}' but recalculated value is '{num_r}' for formula {formula_expr}."
                        )

            passed = False
            reason = ""
            search_coord = target_cell_coord
            typo_tolerated = False

            def _check_val(v, exp):
                nonlocal typo_tolerated
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
                v_clean = re.sub(r'[^a-z0-9]', '', str_v)
                e_clean = re.sub(r'[^a-z0-9]', '', str_e)
                if v_clean and e_clean and v_clean == e_clean:
                    return True
                if v_clean and e_clean and (v_clean in e_clean or e_clean in v_clean) and min(len(v_clean), len(e_clean)) >= 6:
                    return True
                # Strict single-edit typo tolerance for entity/name strings only (never numbers, codes, SKUs, or dates)
                if is_single_edit_typo(str_v, str_e):
                    typo_tolerated = True
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

            # ── RETRY 2: If still not passed and anchor given, try all 4 directions & offsets 1-5 across sheets ──
            if not passed and tc.get("anchor"):
                anchor_coords_r, _ = find_anchor_cell(sheet_orig, tc.get("anchor"))
                if not anchor_coords_r:
                    for alt_s in wb_orig.sheetnames:
                        if alt_s != sheet_name:
                            alt_coords_r, _ = find_anchor_cell(wb_orig[alt_s], tc.get("anchor"))
                            if alt_coords_r:
                                anchor_coords_r = alt_coords_r
                                sheet_recalc = wb_recalc[alt_s] if alt_s in wb_recalc.sheetnames else (wb_recalc.active or wb_orig[alt_s])
                                break
                if anchor_coords_r:
                    for try_dir in ["down", "right", "left", "up"]:
                        for try_offset in [1, 2, 3, 4, 5]:
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

            # If mustBeFormula or formula_structure mode, student cannot pass with a hardcoded number
            if (tc.get("mustBeFormula") or tc_type == "formula_structure") and passed:
                if not (isinstance(formula_expr, str) and formula_expr.startswith("=")):
                    passed = False
                    reason = f"Cell {search_coord} contains hardcoded static value '{cell_val}' instead of a dynamic spreadsheet formula as required."

            if passed:
                criterion_passes[crit_name]["passed"] += 1
                if not reason:
                    if typo_tolerated:
                        reason = f"Cell {search_coord} matched expected value '{tc.get('expectedValue')}' with typo tolerance (student: '{cell_val}')."
                    else:
                        reason = f"Cell {search_coord} matched expected value {tc.get('expectedValue')} within tolerance."
            else:
                if not reason:
                    reason = f"Cell {target_cell_coord} value '{cell_val}' did not match expected '{tc.get('expectedValue')}'. Scanned surrounding region — no match found."

            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "cell": search_coord,
                "actual": cell_val,
                "cached": cached_val,
                "formula": formula_expr if isinstance(formula_expr, str) and formula_expr.startswith("=") else None,
                "expected": tc.get("expectedValue"),
                "passed": passed,
                "typo_tolerated": typo_tolerated,
                "status": "VERIFIED_PASS" if passed else "VERIFIED_FAIL",
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": reason
            })

        # -------------------------------------------------------------
        # TYPE 2: Formula Range Check (Anti-Cheat & Dynamic References)
        # -------------------------------------------------------------
        elif tc_type in ["formula_range_check", "formula_check", "function_check", "formula_structure"]:
            range_str = tc.get("range", tc.get("cell", ""))
            anchor_text = tc.get("columnAnchor") or tc.get("anchor")
            accepted_funcs = [f.upper() for f in tc.get("acceptedFunctions", tc.get("accepted_functions", []))]
            strict_method = tc.get("strictMethod", tc.get("strict_method_required", False))
            if accepted_funcs and not strict_method:
                expanded_fns = set(accepted_funcs)
                for fn in accepted_funcs:
                    if fn in FUNCTION_EQUIVALENTS:
                        expanded_fns.update(FUNCTION_EQUIVALENTS[fn])
                accepted_funcs = list(expanded_fns)
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
                            # Data Cleaning exception: if student applied cleaning (TRIM, PROPER, UPPER, LOWER, CLEAN)
                            # and pasted as values, verify that the value itself represents valid cleaned data
                            is_text_cleaning = any(f in accepted_funcs for f in ["TRIM", "PROPER", "CLEAN", "UPPER", "LOWER", "TEXT"])
                            allow_values = tc.get("allowPastedValues") or is_text_cleaning
                            val_str = str(raw_val or "").strip()

                            if allow_values and val_str:
                                is_valid_cleaned = False
                                if "TRIM" in accepted_funcs and val_str == " ".join(val_str.split()):
                                    is_valid_cleaned = True
                                elif "PROPER" in accepted_funcs and (val_str == val_str.title() or not any(c.isupper() for c in val_str[1:] if c.isalpha())):
                                    is_valid_cleaned = True
                                elif "UPPER" in accepted_funcs and val_str == val_str.upper():
                                    is_valid_cleaned = True
                                elif is_text_cleaning and len(val_str) > 0:
                                    is_valid_cleaned = True

                                if is_valid_cleaned:
                                    p_cells += 1
                                    continue

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
                            if not has_accepted and any(k in accepted_funcs for k in ["CELL_ARITHMETIC", "ARITHMETIC"]):
                                if any(op in clean_formula for op in ['*', '+', '-', '/', '^']) and re.search(r'[A-Z]+[0-9]+', clean_formula):
                                    has_accepted = True
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

            # Row-Shift Rescue: Handle instruction vs worksheet template row offsets (e.g. A2 vs A3)
            if (passed_cells == 0 or passed_cells < total_cells * 0.6) and cells_to_check and must_be_formula:
                col_idx = cells_to_check[0].column
                start_r, end_r = parse_range_rows(range_str)
                for r_offset in [1, 2, -1]:
                    new_start = max(1, start_r + r_offset)
                    new_end = max(new_start, end_r + r_offset)
                    shifted_cells = [sheet_orig.cell(row=r, column=col_idx) for r in range(new_start, new_end + 1)]
                    while shifted_cells and (shifted_cells[-1].value is None or str(shifted_cells[-1].value).strip() == ""):
                        shifted_cells.pop()
                    if shifted_cells:
                        shift_passed, shift_fails = evaluate_formula_cells(shifted_cells)
                        if shift_passed > passed_cells and shift_passed >= len(shifted_cells) * 0.7:
                            cells_to_check = shifted_cells
                            total_cells = len(shifted_cells)
                            passed_cells = shift_passed
                            failures = shift_fails
                            range_str = f"{get_column_letter(col_idx)}{new_start}:{get_column_letter(col_idx)}{new_start + len(shifted_cells) - 1}"
                            break

            if arrayformula_col_pass:
                passed_cells = total_cells
                failures = []
                tc_passed = True
                score_factor = 1.0
                reason = f"Verified {total_cells}/{total_cells} cells via dynamic array/spill formula in {range_str}."
            else:
                score_factor = round(passed_cells / total_cells, 4) if total_cells > 0 else 0.0
                tc_passed = (total_cells > 0 and passed_cells == total_cells)
                if tc_passed:
                    reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} successfully."
                elif passed_cells > 0:
                    pct = round(score_factor * 100, 1)
                    reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} ({pct}% proportional credit). First issues: {'; '.join(failures[:3])}"
                else:
                    reason = f"{total_cells}/{total_cells} cells failed formula checks. First issues: {'; '.join(failures[:3])}"

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "range": range_str,
                "totalCells": total_cells,
                "passedCells": passed_cells,
                "passed": tc_passed,
                "score": score_factor,
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": reason,
                "failures": failures[:10]
            })

        # -------------------------------------------------------------
        # TYPE 3: Format & Style Check (Headers, Borders, Bold, Alignment, Currency)
        # -------------------------------------------------------------
        elif tc_type in ["format_check", "style_check"]:
            reason = ""
            req_cf = tc.get("conditionalFormatting") or tc.get("cf")
            if req_cf and isinstance(req_cf, dict):
                req_rule_type = str(req_cf.get("ruleType", "")).strip().lower()
                cf_list = []
                if registry and hasattr(registry, "conditional_formattings"):
                    cf_list = registry.conditional_formattings
                elif hasattr(sheet_orig, "conditional_formatting"):
                    for cf in sheet_orig.conditional_formatting:
                        rules_data = []
                        for r in getattr(cf, "rules", []):
                            rules_data.append({
                                "type": str(getattr(r, "type", "") or ""),
                                "operator": str(getattr(r, "operator", "") or ""),
                                "formulas": [str(f) for f in getattr(r, "formula", [])] if getattr(r, "formula", None) else []
                            })
                        cf_list.append({
                            "sheet": sheet_orig.title,
                            "range": str(getattr(cf, "sqref", "") or ""),
                            "rules": rules_data
                        })

                cf_found = False
                matched_cf_desc = ""
                for cf_item in cf_list:
                    for r in cf_item.get("rules", []):
                        r_type = str(r.get("type", "")).strip().lower()
                        r_op = str(r.get("operator", "")).strip().lower()

                        if req_rule_type in ["lessthan", "less_than", "<"]:
                            if r_op in ["lessthan", "<"] or r_type in ["lessthan", "<"] or ("cellis" in r_type and "less" in r_op):
                                cf_found = True
                                matched_cf_desc = f"Less-than rule on {cf_item.get('sheet', '')} range '{cf_item.get('range', '')}'"
                                break
                        elif req_rule_type in ["greaterthan", "greater_than", ">"]:
                            if r_op in ["greaterthan", ">"] or r_type in ["greaterthan", ">"] or ("cellis" in r_type and "great" in r_op):
                                cf_found = True
                                matched_cf_desc = f"Greater-than rule on {cf_item.get('sheet', '')} range '{cf_item.get('range', '')}'"
                                break
                        elif req_rule_type in ["colorscale", "color_scale", "heatmap"]:
                            if "colorscale" in r_type or "color_scale" in r_type:
                                cf_found = True
                                matched_cf_desc = f"Color scale heatmap on {cf_item.get('sheet', '')} range '{cf_item.get('range', '')}'"
                                break
                        elif req_rule_type in ["duplicatevalues", "duplicate_values", "duplicates", "duplicate"]:
                            if "duplicate" in r_type or "duplicate" in r_op:
                                cf_found = True
                                matched_cf_desc = f"Duplicate values rule on {cf_item.get('sheet', '')} range '{cf_item.get('range', '')}'"
                                break
                        elif req_rule_type in ["containstext", "contains_text", "text"]:
                            if "containstext" in r_type or "contains" in r_op:
                                cf_found = True
                                matched_cf_desc = f"Contains text rule on {cf_item.get('sheet', '')} range '{cf_item.get('range', '')}'"
                                break
                        elif req_rule_type in r_type or req_rule_type in r_op or (r_type and r_type in req_rule_type):
                            cf_found = True
                            matched_cf_desc = f"{r_type} rule on {cf_item.get('sheet', '')} range '{cf_item.get('range', '')}'"
                            break
                    if cf_found:
                        break

                tc_passed = cf_found
                if tc_passed:
                    criterion_passes[crit_name]["passed"] += 1
                    reason = f"Verified conditional formatting: {matched_cf_desc}."
                else:
                    reason = f"Conditional formatting rule '{req_rule_type}' was not found in the workbook."

                score_factor = 1.0 if tc_passed else 0.0
                assertion_results.append({
                    "id": tc_id,
                    "criterion": crit_name,
                    "type": tc_type,
                    "range": tc.get("range", ""),
                    "passed": tc_passed,
                    "score": score_factor,
                    "weight": tc_weight,
                    "is_critical": tc_is_critical,
                    "score_cap": tc_score_cap,
                    "depends_on": tc_depends_on,
                    "dependency_mode": tc_dep_mode,
                    "reason": reason
                })
                continue

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
                            "[$" in fmt or "#,##" in fmt or
                            any(sym in val_str for sym in ["$", "₹", "€", "£", "¥", "Rs"])
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
            if not tc_passed and str(tc.get("numberFormat", "")).lower() == "currency":
                # First check current sheet for currency formatted numeric cells (ignoring blank grid padding)
                curr_passed = 0
                curr_total = 0
                for r in range(1, min(sheet_orig.max_row or 50, 60) + 1):
                    for c in range(1, min(sheet_orig.max_column or 20, 20) + 1):
                        cell = sheet_orig.cell(row=r, column=c)
                        if cell.value is not None and isinstance(cell.value, (int, float)):
                            curr_total += 1
                            fmt = str(cell.number_format or '')
                            if any(sym in fmt for sym in ["$", "₹", "€", "£", "¥", "Rs", "INR", "USD", "EUR"]) or "[$" in fmt or "#,##" in fmt:
                                curr_passed += 1
                if curr_total > 0 and curr_passed >= curr_total * 0.4:
                    tc_passed = True
                    passed_cells = curr_passed
                    total_cells = curr_total
                    reason = f"Verified currency formatting on worksheet '{sheet_name}' ({curr_passed} currency cells detected)."

                # If current sheet doesn't have currency, check candidate alternative sheets, prioritizing pivot table sheets over raw data
                if not tc_passed:
                    candidate_sheets = []
                    for p in (detected_pivots or []):
                        ps = p.get("sheet") if isinstance(p, dict) else getattr(p, "sheet", None)
                        if ps and ps in wb_orig.sheetnames and ps not in candidate_sheets and ps != sheet_name:
                            candidate_sheets.append(ps)
                    source_sheets = [p.get("source_sheet") for p in (detected_pivots or []) if isinstance(p, dict)]
                    for s in wb_orig.sheetnames:
                        if s != sheet_name and s not in candidate_sheets and s not in source_sheets:
                            candidate_sheets.append(s)
                    for s in source_sheets:
                        if s and s in wb_orig.sheetnames and s not in candidate_sheets and s != sheet_name:
                            candidate_sheets.append(s)

                    for alt_s in candidate_sheets:
                        alt_sh = wb_orig[alt_s]
                        alt_passed = 0
                        alt_total = 0
                        for r in range(1, min(alt_sh.max_row or 50, 60) + 1):
                            for c in range(1, min(alt_sh.max_column or 20, 20) + 1):
                                alt_cell = alt_sh.cell(row=r, column=c)
                                if alt_cell.value is not None and isinstance(alt_cell.value, (int, float)):
                                    alt_total += 1
                                    alt_fmt = str(alt_cell.number_format or '')
                                    if any(sym in alt_fmt for sym in ["$", "₹", "€", "£", "¥", "Rs", "INR", "USD", "EUR"]) or "[$" in alt_fmt or "#,##" in alt_fmt:
                                        alt_passed += 1
                        if alt_total > 0 and alt_passed >= alt_total * 0.4:
                            tc_passed = True
                            passed_cells = max(passed_cells, alt_passed)
                            total_cells = max(total_cells, alt_total)
                            reason = f"Verified currency formatting on worksheet '{alt_s}' ({alt_passed} currency cells detected)."
                            break

            score_factor = 1.0 if tc_passed else (round(passed_cells / total_cells, 4) if total_cells > 0 else 0.0)

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                if not reason:
                    reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} satisfied formatting requirements."
            else:
                reason = f"{total_cells - passed_cells}/{total_cells} cells failed formatting ({passed_cells}/{total_cells} passed). First issues: {'; '.join(failures[:3])}"

            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "range": range_str,
                "totalCells": total_cells,
                "passedCells": passed_cells,
                "passed": tc_passed,
                "score": score_factor,
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": reason,
                "failures": failures[:10]
            })

        # -------------------------------------------------------------
        # TYPE 4: Data Check / Non-Empty Table Data
        # -------------------------------------------------------------
        elif tc_type in ["data_check", "non_empty_check", "table_check"]:
            reason = ""
            exp_table_name = tc.get("expectedTable") or tc.get("tableName")
            if exp_table_name and registry is not None:
                found_tab = any(t.get("name", "").lower() == exp_table_name.lower() for t in registry.tables)
                if not found_tab:
                    assertion_results.append({
                        "id": tc.get("id", f"{crit_name}_{len(assertion_results)}"),
                        "criterion": crit_name,
                        "type": tc_type,
                        "weight": tc_weight,
                        "passed": False,
                        "status": "VERIFIED_FAIL",
                        "reason": f"Expected Excel ListObject Table '{exp_table_name}' not found in workbook."
                    })
                    continue
                elif not tc.get("range") and not tc.get("cell"):
                    criterion_passes[crit_name]["passed"] += 1
                    assertion_results.append({
                        "id": tc.get("id", f"{crit_name}_{len(assertion_results)}"),
                        "criterion": crit_name,
                        "type": tc_type,
                        "weight": tc_weight,
                        "passed": True,
                        "status": "VERIFIED_PASS",
                        "reason": f"Verified Excel Table '{exp_table_name}' exists in workbook."
                    })
                    continue

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
            if not tc_passed:
                is_pq_crit = any(k in crit_name.lower() for k in ["power query", "data source import", "query merge", "column expansion", "close & load", "automation test"])
                has_pq = (registry is not None and registry.macro_info.get("has_power_query")) or macro_info.get("has_power_query")

                # If workbook has Power Query or this is a PQ criterion, check for loaded query tables anywhere in the workbook
                if (is_pq_crit or has_pq) and registry:
                    for tab in (registry.macro_info.get("query_tables", []) + registry.tables):
                        t_sheet = tab.get("sheet", "")
                        if t_sheet in wb_orig.sheetnames:
                            t_ws = wb_orig[t_sheet]
                            if t_ws.max_row >= 2:
                                tc_passed = True
                                passed_cells = max(passed_cells, 1)
                                total_cells = max(total_cells, 1)
                                reason = f"Verified Power Query automated table load on worksheet '{t_sheet}' (Table: {tab.get('name', 'QueryOutput')})."
                                break

                # Check other sheets in the workbook (e.g. if student loaded Power Query to 'Orders' or 'Customers' instead of Sheet1)
                if not tc_passed:
                    for alt_s in wb_orig.sheetnames:
                        alt_sh = wb_orig[alt_s]
                        alt_tb = detect_table_bounds(alt_sh)
                        if alt_tb.get("header_row") and alt_tb.get("data_end_row", 0) >= alt_tb.get("data_start_row", 0):
                            non_empty_cnt = sum(
                                1 for r in range(alt_tb.get("data_start_row", 1), min(alt_tb.get("data_start_row", 1) + 5, alt_tb.get("data_end_row", 1) + 1))
                                for c in range(alt_tb.get("start_col", 1), min(alt_tb.get("start_col", 1) + 5, alt_tb.get("end_col", 1) + 1))
                                if alt_sh.cell(r, c).value is not None
                            )
                            if non_empty_cnt >= 2:
                                tc_passed = True
                                passed_cells = max(passed_cells, 1)
                                total_cells = max(total_cells, 1)
                                reason = f"Verified populated data table on worksheet '{alt_s}' (Rows {alt_tb.get('data_start_row')}:{alt_tb.get('data_end_row')})."
                                break

            score_factor = 1.0 if tc_passed else (round(passed_cells / total_cells, 4) if total_cells > 0 else 0.0)

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                if not reason:
                    reason = f"Verified {passed_cells}/{total_cells} cells in {range_str} contain required data."
            else:
                reason = f"{total_cells - passed_cells}/{total_cells} cells are missing data ({passed_cells}/{total_cells} populated). Issues: {'; '.join(failures[:3])}"

            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "range": range_str,
                "totalCells": total_cells,
                "passedCells": passed_cells,
                "passed": tc_passed,
                "score": score_factor,
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": reason,
                "failures": failures[:10]
            })

        # -------------------------------------------------------------
        # TYPE 5: Chart & Visual Graph Check
        # -------------------------------------------------------------
        elif tc_type in ["chart_check", "graph_check"]:
            min_charts = int(tc.get("minCharts", 1))
            req_type = str(tc.get("chartType", tc.get("expectedChartType", ""))).strip().lower()
            req_title_raw = str(tc.get("title", tc.get("expectedTitle", ""))).strip()
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
                    clean_req = re.sub(r'[\s_-]+', '', req_type.lower())
                    def _chart_type_matches(chart_type_str):
                        clean_found = re.sub(r'[\s_-]+', '', (chart_type_str or '').lower())
                        if clean_req in clean_found or clean_found in clean_req:
                            return True
                        if clean_req in ('col', 'column', 'columnchart') and clean_found in ('col', 'column', 'columnchart', 'barchart'):
                            return True
                        if clean_req in ('bar', 'barchart') and clean_found in ('bar', 'barchart', 'columnchart', 'col'):
                            return True
                        return False

                    type_matched = any(_chart_type_matches(c.get("type", "")) for c in target_sheet_charts)
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

                # ── SPRINT 3: Chart Data Source & Rendered Series Verification ──
                req_source_range = tc.get("expectedSourceRange") or tc.get("expectedDataRange") or tc.get("valueRange") or tc.get("sourceRange")
                req_cat_range = tc.get("expectedCategoryRange") or tc.get("categoryRange")
                req_values = tc.get("expectedValues")

                if tc_passed and req_source_range:
                    def _norm_r(r_str: str) -> str:
                        return re.sub(r"[\$'\s]", "", str(r_str or "")).upper()

                    norm_req_src = _norm_r(req_source_range)
                    req_col_range = norm_req_src.split("!")[-1] if "!" in norm_req_src else norm_req_src

                    src_matched = False
                    for c in target_sheet_charts:
                        c_refs = c.get("num_refs", [])
                        for cr in c_refs:
                            norm_cr = _norm_r(cr)
                            cr_col_range = norm_cr.split("!")[-1] if "!" in norm_cr else norm_cr
                            if norm_req_src in norm_cr or norm_cr in norm_req_src or req_col_range == cr_col_range:
                                src_matched = True
                                break
                        if src_matched:
                            break

                    if not src_matched:
                        tc_passed = False
                        all_found_refs = [r for c in target_sheet_charts for r in c.get("num_refs", [])]
                        reason = f"Chart source data range mismatch: expected '{req_source_range}', found {all_found_refs or 'none'}."

                if tc_passed and req_cat_range:
                    def _norm_r(r_str: str) -> str:
                        return re.sub(r"[\$'\s]", "", str(r_str or "")).upper()

                    norm_req_cat = _norm_r(req_cat_range)
                    req_cat_col_range = norm_req_cat.split("!")[-1] if "!" in norm_req_cat else norm_req_cat
                    cat_matched = False
                    for c in target_sheet_charts:
                        c_cats = list(dict.fromkeys(c.get("cat_refs", []) + c.get("num_refs", [])))
                        for cc in c_cats:
                            norm_cc = _norm_r(cc)
                            cc_col_range = norm_cc.split("!")[-1] if "!" in norm_cc else norm_cc
                            if norm_req_cat in norm_cc or norm_cc in norm_req_cat or req_cat_col_range == cc_col_range:
                                cat_matched = True
                                break
                        if cat_matched:
                            break

                    if not cat_matched:
                        tc_passed = False
                        all_found_cats = [r for c in target_sheet_charts for r in c.get("cat_refs", [])]
                        reason = f"Chart category range mismatch: expected '{req_cat_range}', found {all_found_cats or 'none'}."

                if tc_passed and req_values:
                    matched_values = []
                    for c in target_sheet_charts:
                        for cr in c.get("num_refs", []):
                            m_sh = sheet_name
                            m_rng = cr
                            if "!" in cr:
                                m_sh_raw, m_rng = cr.split("!", 1)
                                m_sh = m_sh_raw.replace("'", "")
                            m_sh = resolve_sheet(wb_recalc, m_sh)
                            if m_sh in wb_recalc.sheetnames:
                                try:
                                    cells = wb_recalc[m_sh][m_rng.replace("$", "")]
                                    if not isinstance(cells, tuple):
                                        cells = ((cells,),)
                                    elif cells and not isinstance(cells[0], tuple):
                                        cells = (cells,)
                                    for row in cells:
                                        for cell in row:
                                            nv = parse_number(cell.value)
                                            if nv is not None:
                                                matched_values.append(nv)
                                except Exception:
                                    pass
                    if matched_values:
                        exp_nums = [parse_number(ev) for ev in req_values if parse_number(ev) is not None]
                        val_mismatches = []
                        for ev in exp_nums:
                            if not any(check_dual_tolerance(mv, ev, 0.05, 0.001) for mv in matched_values):
                                val_mismatches.append(ev)
                        if val_mismatches:
                            tc_passed = False
                            reason = f"Chart data values mismatch: expected {exp_nums}, actual series values: {matched_values[:10]}."
                        else:
                            reason += f" Verified chart data points match expected values ({len(exp_nums)} items)."

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
            target_sheet_req = str(tc.get("sheet", "")).strip()

            is_data_model_crit = any(k in crit_name.lower() for k in ["data model", "relational", "diagram", "ingestion", "multi-table"])
            tc_passed = len(detected_pivots) >= min_pivots
            if not tc_passed and is_data_model_crit and registry and (len(registry.tables) >= min_pivots or registry.macro_info.get("has_power_query") or registry.macro_info.get("has_data_model")):
                tc_passed = True
            reason = ""

            if not tc_passed:
                reason = f"Expected at least {min_pivots} native Pivot Table(s), found {len(detected_pivots)} in workbook."
            else:
                if req_field:
                    field_found = False
                    matched_field_name = ""
                    matched_pivot_name = ""
                    matched_pivot_sheet = ""

                    # Order pivots: pivots on target_sheet first, then all remaining pivots
                    sorted_pivots = []
                    if target_sheet_req:
                        for p in detected_pivots:
                            p_sheet = p.get("sheet", "")
                            if _norm_ws(p_sheet) == _norm_ws(target_sheet_req) or target_sheet_req.lower() in p_sheet.lower():
                                sorted_pivots.append(p)
                    for p in detected_pivots:
                        if p not in sorted_pivots:
                            sorted_pivots.append(p)

                    req_clean = re.sub(r'[^a-z0-9]', '', req_field)
                    req_tokens = set(re.findall(r'\w+', req_field))

                    for p in sorted_pivots:
                        p_active = [
                            str(f.get("name") or f.get("caption") if isinstance(f, dict) else f).lower()
                            for f in (p.get("activeFields") or p.get("active_fields") or [])
                            if f
                        ]
                        p_all = [
                            str(f.get("name") or f.get("caption") if isinstance(f, dict) else f).lower()
                            for f in (p.get("fields") or [])
                            if f
                        ]
                        p_data = [
                            str(df.get("name", "")).lower()
                            for df in (p.get("dataFields") or p.get("data_fields") or [])
                            if isinstance(df, dict) and df.get("name")
                        ]
                        p_src = [
                            str(df.get("sourceField", "")).lower()
                            for df in (p.get("dataFields") or p.get("data_fields") or [])
                            if isinstance(df, dict) and df.get("sourceField")
                        ]
                        p_rows = [
                            str(f.get("name") or f.get("caption") if isinstance(f, dict) else f).lower()
                            for f in (p.get("rowFields") or p.get("row_fields") or [])
                            if f
                        ]
                        p_cols = [
                            str(f.get("name") or f.get("caption") if isinstance(f, dict) else f).lower()
                            for f in (p.get("colFields") or p.get("col_fields") or [])
                            if f
                        ]

                        crit_lower = crit_name.lower()
                        axis_req = str(tc.get("axis", "")).strip().lower()
                        is_col_crit = axis_req in ["column", "columns", "col"] or ("column" in crit_lower and "row" not in crit_lower and "value" not in crit_lower)
                        is_row_crit = axis_req in ["row", "rows"] or ("row" in crit_lower and "column" not in crit_lower and "value" not in crit_lower)
                        is_val_crit = axis_req in ["value", "values", "data"] or ("value" in crit_lower and "row" not in crit_lower and "column" not in crit_lower)

                        if is_col_crit:
                            candidate_pools = [("columns", p_cols)]
                        elif is_row_crit:
                            candidate_pools = [("rows", p_rows if p_rows else p_active)]
                        elif is_val_crit:
                            candidate_pools = [("values", list(dict.fromkeys(p_data + p_src)))]
                        elif is_data_model_crit:
                            candidate_pools = [
                                ("active", list(dict.fromkeys(p_active + p_data + p_src + p_rows + p_cols))),
                                ("cache", p_all)
                            ]
                        else:
                            candidate_pools = [
                                ("active", list(dict.fromkeys(p_active + p_data + p_src + p_rows + p_cols)))
                            ]

                        for pool_type, candidates in candidate_pools:
                            # 1. Exact match or normalized alphanumeric match
                            for f in candidates:
                                f_clean = re.sub(r'[^a-z0-9]', '', f)
                                if req_field == f or req_clean == f_clean or req_field in f or f in req_field:
                                    field_found = True
                                    matched_field_name = f
                                    matched_pivot_name = p.get("name", "PivotTable")
                                    matched_pivot_sheet = p.get("sheet", "")
                                    break
                            if field_found:
                                break

                            # 2. Token overlap match (e.g. "sales amount" vs "sales", "sum of amount" vs "amount")
                            for f in candidates:
                                f_tokens = set(re.findall(r'\w+', f))
                                if req_tokens and (req_tokens.issubset(f_tokens) or f_tokens.issubset(req_tokens) or (req_tokens & f_tokens and len(req_tokens & f_tokens) >= min(len(req_tokens), len(f_tokens)))):
                                    field_found = True
                                    matched_field_name = f
                                    matched_pivot_name = p.get("name", "PivotTable")
                                    matched_pivot_sheet = p.get("sheet", "")
                                    break
                            if field_found:
                                break

                            # 3. Domain concept match (TABLE_CONCEPTS)
                            for concept, kws in TABLE_CONCEPTS.items():
                                if any(kw in req_field for kw in kws):
                                    for f in candidates:
                                        if any(kw in f for kw in kws):
                                            field_found = True
                                            matched_field_name = f
                                            matched_pivot_name = p.get("name", "PivotTable")
                                            matched_pivot_sheet = p.get("sheet", "")
                                            break
                                if field_found:
                                    break
                            if field_found:
                                break

                        if field_found:
                            break

                    # DATA MODELING RESCUE: Check if the required field exists in the data model tables,
                    # power queries, or workbook table columns (e.g. Product_ID, Customer_ID, Revenue in Sales/Products)
                    if not field_found and registry is not None:
                        req_norm = re.sub(r'[^a-z0-9]', '', req_field)
                        for t in registry.tables:
                            t_sheet = t.get("sheet", "")
                            if not target_sheet_req or _norm_ws(t_sheet) == _norm_ws(target_sheet_req) or target_sheet_req.lower() in t.get("name", "").lower() or is_data_model_crit:
                                for col_h in t.get("columns", []):
                                    c_clean = re.sub(r'[^a-z0-9]', '', col_h.lower())
                                    if req_norm == c_clean or req_clean in c_clean or c_clean in req_clean:
                                        field_found = True
                                        matched_field_name = col_h
                                        matched_pivot_name = f"DataModelTable_{t.get('name')}"
                                        matched_pivot_sheet = t_sheet
                                        break
                            if field_found:
                                break

                    if field_found:
                        sheet_info = f" on worksheet '{matched_pivot_sheet}'" if matched_pivot_sheet else ""
                        reason = f"Verified native Pivot Table '{matched_pivot_name}'{sheet_info} with field '{matched_field_name or req_field}'."
                    else:
                        tc_passed = False
                        reason = f"Pivot table does not include required field '{req_field}'."
                else:
                    reason = f"Verified {len(detected_pivots)} native Pivot Table(s) in workbook."

                # ── SPRINT 3: Pivot Table Rendered Grand Total & Output Verification ──
                exp_grand_total = tc.get("expectedGrandTotal") or tc.get("expectedTotal")
                exp_values = tc.get("expectedValues")

                if tc_passed and (exp_grand_total is not None or exp_values is not None):
                    exp_gt_num = parse_number(exp_grand_total) if exp_grand_total is not None else None
                    gt_matched = False
                    found_numbers = []

                    # Scan rendered cells for each candidate pivot
                    for p in detected_pivots:
                        p_sheet = resolve_sheet(wb_recalc, p.get("sheet", sheet_name))
                        if p_sheet not in wb_recalc.sheetnames:
                            continue
                        sh_recalc = wb_recalc[p_sheet]
                        loc_ref = p.get("location_ref", "")
                        cells_to_scan = []

                        if loc_ref and ":" in loc_ref:
                            try:
                                start_col, start_row, end_col, end_row = openpyxl.utils.range_boundaries(loc_ref)
                                for r in range(start_row, min(end_row + 5, 250)):
                                    for c in range(start_col, min(end_col + 5, 50)):
                                        cells_to_scan.append(sh_recalc.cell(row=r, column=c))
                            except Exception:
                                pass
                        if not cells_to_scan:
                            for r in range(1, min(sh_recalc.max_row or 50, 100)):
                                for c in range(1, min(sh_recalc.max_column or 20, 30)):
                                    cells_to_scan.append(sh_recalc.cell(row=r, column=c))

                        total_row_nums = []
                        total_col_nums = []
                        for cell in cells_to_scan:
                            cv_str = str(cell.value or "").strip().lower()
                            if "total" in cv_str:
                                total_row_nums.append(cell.row)
                                total_col_nums.append(cell.column)

                        for cell in cells_to_scan:
                            if cell.row in total_row_nums or cell.column in total_col_nums:
                                val_num = parse_number(cell.value)
                                if val_num is not None:
                                    found_numbers.append(val_num)

                        if loc_ref and ":" in loc_ref:
                            try:
                                _, _, end_col, end_row = openpyxl.utils.range_boundaries(loc_ref)
                                for r in [end_row - 1, end_row, end_row + 1]:
                                    for c in range(max(1, end_col - 2), end_col + 3):
                                        vn = parse_number(sh_recalc.cell(row=r, column=c).value)
                                        if vn is not None:
                                            found_numbers.append(vn)
                            except Exception:
                                pass

                        if exp_gt_num is not None:
                            for fn in found_numbers:
                                if check_dual_tolerance(fn, exp_gt_num, abs_tol=1.0, rel_tol=0.005):
                                    gt_matched = True
                                    break
                        if gt_matched:
                            break

                    if exp_gt_num is not None:
                        if gt_matched:
                            reason += f" Verified PivotTable Grand Total: {exp_gt_num}."
                        else:
                            tc_passed = False
                            reason = f"PivotTable Grand Total mismatch: expected {exp_gt_num}, found candidate values: {found_numbers[:10] or 'none'}."

                # Stale PivotCache Warning Detection
                for p in detected_pivots:
                    p_records = int(p.get("record_count", 0))
                    p_src_table = p.get("source_name") or p.get("source_table")
                    if p_records > 0 and p_src_table and registry and registry.tables:
                        for t in registry.tables:
                            if str(p_src_table).lower() in (t.get("name", "").lower(), t.get("display_name", "").lower()):
                                t_rows = int(t.get("row_count", 0))
                                if t_rows > 0 and abs(t_rows - p_records) > 0:
                                    warning_msg = f"PIVOT_CACHE_STALE: PivotCache for '{p.get('name')}' has {p_records} records, but source table '{t.get('display_name') or t.get('name')}' has {t_rows} rows (PivotTable may not have been refreshed)."
                                    calculation_warnings.append(warning_msg)

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "reason": reason,
                "foundPivots": detected_pivots
            })

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
        elif tc_type in ["power_query_check", "query_check", "pq_check"]:
            require_query = tc.get("requireQuery", True)
            q_tables_count = len(macro_info.get("query_tables", []))
            q_names = list(macro_info.get("query_names", []))
            if registry and registry.power_queries:
                for pq in registry.power_queries:
                    if pq["query_name"] not in q_names:
                        q_names.append(pq["query_name"])
            has_pq = macro_info.get("has_power_query", False) or (registry and len(registry.power_queries) > 0) or q_tables_count > 0 or len(q_names) > 0
            tc_passed = has_pq if require_query else True
            reason = ""

            req_headers = tc.get("expectedHeaders") or tc.get("requiredColumns") or []
            min_row_count = int(tc.get("minRowCount", 0))
            expected_qname = tc.get("expectedQueryName")

            pq_m_code_status = "POWER_QUERY_M_CODE_VERIFIED" if has_pq else "POWER_QUERY_M_CODE_MISSING"
            pq_output_status = "POWER_QUERY_OUTPUT_VERIFIED" if (q_tables_count > 0 or (registry and registry.tables)) else "POWER_QUERY_OUTPUT_UNVERIFIED"
            freshness_status = "POWER_QUERY_FRESHNESS_UNVERIFIED"

            if tc_passed and expected_qname:
                clean_exp_qn = str(expected_qname).strip().lower()
                all_found_queries = [str(qn).lower() for qn in q_names]
                if not any(clean_exp_qn in fq or fq in clean_exp_qn for fq in all_found_queries):
                    tc_passed = False
                    reason = f"Expected Power Query named '{expected_qname}', found queries: {q_names or 'none'}."

            if req_headers:
                found_matching_table = False
                tables_to_check = registry.tables if (registry and registry.tables) else []
                for tbl in tables_to_check:
                    tbl_cols = [str(c).lower().strip() for c in tbl.get("columns", [])]
                    if all(any(rh.lower().strip() == tc_col for tc_col in tbl_cols) for rh in req_headers):
                        found_matching_table = True
                        break

                if not found_matching_table:
                    for s_name in wb_orig.sheetnames:
                        sh_o = wb_orig[s_name]
                        tb = detect_table_bounds(sh_o)
                        h_row = tb.get("header_row")
                        if h_row:
                            row_vals = [str(sh_o.cell(row=h_row, column=c).value or "").strip().lower() for c in range(1, min(sh_o.max_column or 20, 50))]
                            if all(any(rh.lower().strip() == rv for rv in row_vals) for rh in req_headers):
                                found_matching_table = True
                                break

                if not found_matching_table:
                    tc_passed = False
                    pq_output_status = "POWER_QUERY_OUTPUT_HEADER_MISMATCH"
                    reason = f"Power Query output table missing required columns: {req_headers}."

            if tc_passed and min_row_count > 0:
                max_found_rows = 0
                if registry and registry.tables:
                    for tbl in registry.tables:
                        max_found_rows = max(max_found_rows, int(tbl.get("row_count", 0)))
                if max_found_rows < min_row_count:
                    for s_name in wb_orig.sheetnames:
                        sh_o = wb_orig[s_name]
                        tb = detect_table_bounds(sh_o)
                        rows_in_sh = max(0, tb.get("data_end_row", 0) - tb.get("data_start_row", 0) + 1)
                        max_found_rows = max(max_found_rows, rows_in_sh)

                if max_found_rows < min_row_count:
                    tc_passed = False
                    reason = f"Power Query output table has {max_found_rows} rows, expected at least {min_row_count}."

            if tc_passed:
                details = []
                if q_names:
                    details.append(f"{len(q_names)} query connections ({', '.join(q_names[:3])})")
                if q_tables_count:
                    details.append(f"{q_tables_count} loaded query tables")
                reason = f"Verified Power Query pipeline ({pq_m_code_status}, {pq_output_status}). Static output verified; live remote refresh unverified (headless environment without external credentials)."
            elif not reason:
                reason = "No Power Query connections or loaded query tables detected."

            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1

            assertion_results.append({
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "reason": reason,
                "foundQueries": q_names,
                "queryTables": macro_info.get("query_tables", []),
                "power_query_status": pq_m_code_status,
                "power_query_output": pq_output_status,
                "freshness_status": freshness_status
            })

        # -------------------------------------------------------------
        # TYPE 9: Controlled Formula-Behavior Check (Scenario Mutations)
        # -------------------------------------------------------------
        elif tc_type in ["formula_behavior_check", "scenario_check", "behavior_check", "mutation_check", "formula_behavior"] or (tc_type in ["formula_check", "formula_range_check"] and tc.get("mode") == "formula_behavior"):
            scenarios = tc.get("scenarios", [])
            if not scenarios and (tc.get("inputs") or tc.get("mutation")) and (tc.get("expectedOutputs") or tc.get("expectedOutput")):
                inputs = tc.get("inputs") or [tc.get("mutation")]
                expected = tc.get("expectedOutputs") or [tc.get("expectedOutput")]
                scenarios = [{
                    "name": tc.get("name", "Default Mutation Scenario"),
                    "inputs": inputs,
                    "expectedOutputs": expected
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

                    # ── SPRINT 3: Lightweight Formula Evaluation Fallback when LO is offline ──
                    if not recalc_ok:
                        wb_scen_formula = openpyxl.load_workbook(sc_orig_path, data_only=False)
                        f_sheet = wb_scen_formula[target_sc_sheet]

                        def _eval_simple_formula(f_str: str, sheet):
                            if not f_str or not str(f_str).startswith("="):
                                return parse_number(f_str)
                            expr = str(f_str)[1:].strip().upper()
                            sum_m = re.match(r"^SUM\(([A-Z]+[0-9]+):([A-Z]+[0-9]+)\)$", expr)
                            if sum_m:
                                total = 0.0
                                for row in sheet[f"{sum_m.group(1)}:{sum_m.group(2)}"]:
                                    for c in row:
                                        nv = parse_number(c.value)
                                        if nv is not None:
                                            total += nv
                                return total
                            avg_m = re.match(r"^AVERAGE\(([A-Z]+[0-9]+):([A-Z]+[0-9]+)\)$", expr)
                            if avg_m:
                                vals = []
                                for row in sheet[f"{avg_m.group(1)}:{avg_m.group(2)}"]:
                                    for c in row:
                                        nv = parse_number(c.value)
                                        if nv is not None:
                                            vals.append(nv)
                                return sum(vals) / len(vals) if vals else 0.0
                            def _replace_coord(match):
                                c_ref = match.group(0)
                                try:
                                    v = parse_number(sheet[c_ref].value)
                                    return str(v if v is not None else 0)
                                except Exception:
                                    return "0"
                            clean_expr = re.sub(r"\b[A-Z]+[0-9]+\b", _replace_coord, expr)
                            if re.match(r"^[0-9\+\-\*\/\%\(\)\.\s]+$", clean_expr):
                                try:
                                    return float(eval(clean_expr, {"__builtins__": None}, {}))
                                except Exception:
                                    pass
                            return None

                        for exp in sc_expected:
                            out_c = exp.get("cell")
                            if out_c:
                                try:
                                    f_val = f_sheet[out_c].value
                                    evaluated = _eval_simple_formula(f_val, f_sheet)
                                    if evaluated is not None:
                                        recalc_sheet[out_c].value = evaluated
                                        recalc_ok = True
                                except Exception:
                                    pass
                        wb_scen_formula.close()

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

        # -------------------------------------------------------------
        # TYPE 8: Conditional Formatting Check
        # -------------------------------------------------------------
        elif tc_type in ["conditional_formatting_check", "cf_check", "conditional_formatting"]:
            min_rules = int(tc.get("minRules", 1))
            req_type = str(tc.get("ruleType", "")).strip().lower()
            
            cf_list = []
            if registry and hasattr(registry, "conditional_formattings"):
                cf_list = registry.conditional_formattings
                if tc.get("sheet"):
                    s_target = str(tc.get("sheet")).strip().lower()
                    cf_list = [cf for cf in cf_list if cf.get("sheet", "").strip().lower() == s_target]

            total_rules_found = sum(len(cf.get("rules", [])) for cf in cf_list)
            tc_passed = total_rules_found >= min_rules
            reason = ""
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"Verified conditional formatting: {total_rules_found} rule(s) detected across target sheet/range."
            else:
                reason = f"Expected at least {min_rules} conditional formatting rule(s), found {total_rules_found}."

            score_factor = 1.0 if tc_passed else (0.5 if total_rules_found > 0 else 0.0)
            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "score": score_factor,
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": reason,
                "rules_found": total_rules_found
            })

        # -------------------------------------------------------------
        # TYPE 9: Data Validation Check (In-Cell Dropdowns & Constraints)
        # -------------------------------------------------------------
        elif tc_type in ["data_validation_check", "dropdown_check", "data_validation"]:
            min_validations = int(tc.get("minValidations", 1))
            req_dv_type = str(tc.get("validationType", "list")).strip().lower()
            
            dv_list = []
            if registry and hasattr(registry, "data_validations"):
                dv_list = registry.data_validations
                if tc.get("sheet"):
                    s_target = str(tc.get("sheet")).strip().lower()
                    dv_list = [dv for dv in dv_list if dv.get("sheet", "").strip().lower() == s_target]

            if req_dv_type:
                matching_dv = [dv for dv in dv_list if str(dv.get("type", "")).strip().lower() == req_dv_type]
            else:
                matching_dv = dv_list

            tc_passed = len(matching_dv) >= min_validations
            reason = ""
            if tc_passed:
                criterion_passes[crit_name]["passed"] += 1
                reason = f"Verified data validation: {len(matching_dv)} validation rule(s) (type '{req_dv_type}') detected."
            else:
                reason = f"Expected at least {min_validations} data validation rule(s) (type '{req_dv_type}'), found {len(matching_dv)}."

            score_factor = 1.0 if tc_passed else 0.0
            assertion_results.append({
                "id": tc_id,
                "criterion": crit_name,
                "type": tc_type,
                "passed": tc_passed,
                "score": score_factor,
                "weight": tc_weight,
                "is_critical": tc_is_critical,
                "score_cap": tc_score_cap,
                "depends_on": tc_depends_on,
                "dependency_mode": tc_dep_mode,
                "reason": reason,
                "validations_found": len(matching_dv)
            })

    # ── SPRINT 1: Mathematical Scoring, Dependency Graph & Denominator Integrity ──
    rubric_breakdown = {}
    rubric_breakdown_details = {}
    total_rubric_available = sum(float(crit.get("weight", 0)) for crit in criteria)
    verified_earned_total = 0.0
    verified_available_total = 0.0
    pending_review_total = 0.0
    verified_lost_total = 0.0

    # Index assertions by id and type for dependency checking
    assertion_results_by_id = {}
    assertion_results_by_type = {}
    for idx, a in enumerate(assertion_results):
        a.setdefault("id", f"{a.get('criterion', 'crit')}_{idx}")
        a.setdefault("weight", 1.0)
        a.setdefault("is_critical", False)
        a.setdefault("depends_on", [])
        a.setdefault("dependency_mode", "skip_penalty")
        if "status" not in a:
            if a.get("passed") is True:
                a["status"] = "VERIFIED_PASS"
            elif a.get("passed") is False:
                a["status"] = "VERIFIED_FAIL"
            else:
                a["status"] = "MANUAL_REVIEW_REQUIRED"

        a_id = a.get("id")
        if a_id:
            assertion_results_by_id[a_id] = a
        a_type = a.get("type")
        if a_type:
            assertion_results_by_type.setdefault(a_type, []).append(a)

    for crit in criteria:
        c_name = crit.get("name", "")
        weight = float(crit.get("weight", 0))
        c_clean = c_name.strip().lower()
        c_assertions = [
            a for a in assertion_results
            if str(a.get("criterion", "")).strip().lower() == c_clean
        ]

        # Fuzzy token overlap matching if exact string equality returned zero assertions
        if len(c_assertions) == 0:
            STOPWORDS = {
                "and", "or", "the", "in", "of", "for", "to", "a", "an", "is",
                "test", "check", "part", "task", "step", "section", "item", "exercise", "question"
            }
            c_tokens = set(re.findall(r'[a-z0-9]+', c_clean)) - STOPWORDS
            for a in assertion_results:
                a_crit = str(a.get("criterion", "")).strip().lower()
                a_tokens = set(re.findall(r'[a-z0-9]+', a_crit)) - STOPWORDS
                if c_tokens and a_tokens:
                    overlap = c_tokens & a_tokens
                    jaccard = len(overlap) / len(c_tokens | a_tokens)
                    min_ratio = len(overlap) / min(len(c_tokens), len(a_tokens))
                    if (len(overlap) >= 2 and min_ratio >= 0.6) or jaccard >= 0.5:
                        c_assertions.append(a)

        if len(c_assertions) == 0:
            # SPRINT 7: Dynamic Semantic Fallback when assignment has zero test cases configured and fallback is enabled
            if len(test_cases) == 0 and (spec.get("enableSemanticFallback", False) or spec.get("allowHeuristicFallback", False)):
                heuristic_score = 0.0
                reasons = []

                if populated_table_cells >= 10:
                    heuristic_score += 0.35
                    reasons.append(f"Populated data table detected ({populated_table_cells} cells)")
                elif has_any_data:
                    heuristic_score += 0.20
                    reasons.append("Data entries detected")

                if len(all_formulas) >= 3:
                    heuristic_score += 0.40
                    reasons.append(f"Dynamic formulas verified ({len(all_formulas)} formulas)")
                elif len(all_formulas) >= 1:
                    heuristic_score += 0.25
                    reasons.append("Formulas detected")

                if ("pivot" in c_clean or "summary" in c_clean) and detected_pivots:
                    heuristic_score += 0.25
                    reasons.append("Pivot table verified")
                elif ("chart" in c_clean or "graph" in c_clean or "visual" in c_clean) and detected_charts:
                    heuristic_score += 0.25
                    reasons.append("Chart visualization verified")
                elif ("query" in c_clean or "etl" in c_clean) and macro_info.get("has_power_query"):
                    heuristic_score += 0.25
                    reasons.append("Power Query pipeline verified")

                if not has_any_data and len(all_formulas) == 0:
                    heuristic_score = 0.0
                    reasons = ["Workbook is completely blank; no student work detected."]
                else:
                    heuristic_score = min(1.0, max(0.25, round(heuristic_score, 2)))

                awarded = round(heuristic_score * weight, 2)
                passed_bool = heuristic_score >= 0.50
                crit_status = "VERIFIED_PASS" if heuristic_score >= 0.85 else ("PARTIAL_CREDIT" if heuristic_score > 0.0 else "VERIFIED_FAIL")

                assertion_results.append({
                    "id": f"{c_name}_heuristic",
                    "criterion": c_name,
                    "type": "heuristic_fallback",
                    "passed": passed_bool,
                    "score": heuristic_score,
                    "status": crit_status,
                    "weight": weight,
                    "reason": f"Professor Heuristic Evaluation: {'; '.join(reasons)}."
                })
                rubric_breakdown[c_name] = awarded
                rubric_breakdown_details[c_name] = {
                    "awarded": awarded,
                    "weight": weight,
                    "status": crit_status,
                    "pending_marks": 0.0,
                    "fraction": heuristic_score,
                    "assertions_count": 1,
                    "passed_count": 1 if passed_bool else 0,
                    "reason": f"Professor Heuristic Evaluation: {'; '.join(reasons)}."
                }
                verified_earned_total += awarded
                verified_available_total += weight
                continue

            # Standard unmapped rubric handling when test cases exist but don't cover this criterion
            needs_review = True
            crit_status = "MANUAL_REVIEW_REQUIRED"
            awarded = 0.0
            pending = weight
            pending_review_total += pending

            assertion_results.append({
                "id": f"{c_name}_unmapped",
                "criterion": c_name,
                "type": "unmapped_rubric",
                "passed": None,
                "status": "MANUAL_REVIEW_REQUIRED",
                "weight": weight,
                "pending_marks": pending,
                "reason": f"No automated test cases configured for criterion '{c_name}'. Flagged for instructor manual review."
            })
            rubric_breakdown[c_name] = 0.0
            rubric_breakdown_details[c_name] = {
                "awarded": 0.0,
                "weight": weight,
                "status": "MANUAL_REVIEW_REQUIRED",
                "pending_marks": pending,
                "reason": f"No automated assertions mapped. Pending manual instructor review.",
                "assertions_count": 0,
                "passed_count": 0
            }
            continue

        # ── Weighted Assertion Calculation ──
        weighted_sum = 0.0
        total_assertion_weights = 0.0
        pending_assertion_weights = 0.0
        has_critical_failure = False
        critical_cap = None
        has_pending_assertion = False

        for a in c_assertions:
            w_i = float(a.get("weight", 1.0))
            is_crit = bool(a.get("is_critical", False))
            s_cap = a.get("score_cap")
            deps = a.get("depends_on", [])
            dep_mode = a.get("dependency_mode", "skip_penalty")

            # Check upstream dependencies to prevent double deduction
            dep_failed = False
            has_cycle = False

            def _detect_cycle(cur_id, visited=None):
                if visited is None:
                    visited = set()
                if cur_id in visited:
                    return True
                visited.add(cur_id)
                t_obj = assertion_results_by_id.get(cur_id)
                if t_obj:
                    for d_sub in t_obj.get("depends_on", []):
                        if _detect_cycle(d_sub, visited.copy()):
                            return True
                return False

            for dep in deps:
                if _detect_cycle(dep, {a.get("id")}):
                    has_cycle = True
                    break
                target_a = assertion_results_by_id.get(dep)
                if target_a and target_a.get("passed") is False:
                    dep_failed = True
                    break
                type_matches = assertion_results_by_type.get(dep, [])
                if any(tm.get("passed") is False for tm in type_matches):
                    dep_failed = True
                    break

            if has_cycle:
                a["status"] = "CONFIGURATION_ERROR"
                a["reason"] = f"Circular dependency detected in 'depends_on' configuration involving '{dep}'."
                needs_review = True
                has_pending_assertion = True
                pending_assertion_weights += w_i
                total_assertion_weights += w_i
                continue

            if dep_failed:
                a["dependency_failed"] = True
                total_assertion_weights += w_i
                if dep_mode == "strict_fail":
                    a["status"] = "VERIFIED_FAIL"
                    a["reason"] = f"(Prerequisite '{dep}' failed — downstream assertion strictly failed) {a.get('reason', '')}"
                    s_i = 0.0
                    weighted_sum += (w_i * s_i)
                elif dep_mode == "partial_credit":
                    a["status"] = "PARTIAL_CREDIT"
                    a["reason"] = f"(Prerequisite '{dep}' failed — awarded partial credit) {a.get('reason', '')}"
                    p_cred = float(a.get("partial_credit", 0.5))
                    weighted_sum += (w_i * p_cred)
                else:
                    # Fair Accounting: BLOCKED_BY_DEPENDENCY -> Routed to instructor review, never silent failure
                    a["status"] = "BLOCKED_BY_DEPENDENCY"
                    a["reason"] = f"(Prerequisite '{dep}' failed — downstream assertion blocked, routed to instructor review) {a.get('reason', '')}"
                    has_pending_assertion = True
                    pending_assertion_weights += w_i
                    needs_review = True
                continue

            total_assertion_weights += w_i

            if a.get("status") in ["MANUAL_REVIEW_REQUIRED", "UNSUPPORTED_FEATURE", "BLOCKED_BY_DEPENDENCY"]:
                has_pending_assertion = True
                pending_assertion_weights += w_i

            # Score factor s_i in [0.0, 1.0]
            if a.get("passed") is True:
                s_i = 1.0
            elif a.get("passed") is False:
                s_i = float(a.get("score", 0.0))
            else:
                s_i = 0.0

            weighted_sum += (w_i * s_i)

            if is_crit and not a.get("passed"):
                has_critical_failure = True
                cap_val = float(s_cap) if s_cap is not None else 0.0
                if critical_cap is None or cap_val < critical_cap:
                    critical_cap = cap_val

        if total_assertion_weights > 0:
            fraction = weighted_sum / total_assertion_weights
        else:
            fraction = 0.0

        if has_critical_failure:
            cap = critical_cap if critical_cap is not None else 0.0
            awarded = round(min(fraction * weight, cap), 2)
        else:
            awarded = round(fraction * weight, 2)

        if has_pending_assertion:
            crit_status = "MANUAL_REVIEW_REQUIRED"
            needs_review = True
            if total_assertion_weights > 0:
                pending = round(min(weight - awarded, (pending_assertion_weights / total_assertion_weights) * weight), 2)
            else:
                pending = weight
            pending_review_total += pending
            verified_earned_total += awarded
            verified_available_total += round(weight - pending, 2)
            verified_lost_total += round(max(0.0, weight - awarded - pending), 2)
        else:
            pending = 0.0
            crit_status = "VERIFIED_PASS" if awarded >= weight else "VERIFIED_FAIL"
            verified_earned_total += awarded
            verified_available_total += weight
            verified_lost_total += round(max(0.0, weight - awarded), 2)

        rubric_breakdown[c_name] = awarded
        rubric_breakdown_details[c_name] = {
            "awarded": awarded,
            "weight": weight,
            "status": crit_status,
            "pending_marks": pending,
            "fraction": round(fraction, 4),
            "assertions_count": len(c_assertions),
            "passed_count": sum(1 for a in c_assertions if a.get("passed") is True)
        }

    # ── Final Overall Score Calculation (Never shrink the denominator) ──
    effective_max = orig_max_score if (preserve_scale and orig_max_score is not None) else 100.0
    total_score = min(effective_max, max(0.0, round(verified_earned_total, 2)))

    eval_status = "COMPLETED"
    if needs_review or pending_review_total > 0:
        eval_status = "MANUAL_REVIEW_REQUIRED"

    scoring_summary = {
        "total_available": round(total_rubric_available, 2),
        "verified_earned": round(verified_earned_total, 2),
        "verified_available": round(verified_available_total, 2),
        "pending_review": round(pending_review_total, 2),
        "verified_lost": round(verified_lost_total, 2),
        "percentage_earned": round((verified_earned_total / max(1.0, total_rubric_available)) * 100, 2),
        "status": eval_status
    }

    # Extract clean workbook manifest for the LLM Verification & Arbitration Layer
    manifest_cfs = []
    try:
        if registry and hasattr(registry, "conditional_formattings") and registry.conditional_formattings:
            manifest_cfs = registry.conditional_formattings
        elif wb_orig:
            for s_name in wb_orig.sheetnames:
                sh = wb_orig[s_name]
                if hasattr(sh, "conditional_formatting"):
                    for cf in sh.conditional_formatting:
                        sqref = str(getattr(cf, "sqref", "") or getattr(cf, "cells", ""))
                        rules = []
                        for r in getattr(cf, "rules", []):
                            rules.append({
                                "type": str(getattr(r, "type", "")),
                                "operator": str(getattr(r, "operator", "") or ""),
                                "priority": int(getattr(r, "priority", 0) or 0),
                                "formula": [str(f) for f in getattr(r, "formula", []) if f]
                            })
                        if rules:
                            manifest_cfs.append({"sheet": s_name, "range": sqref, "rules": rules})
    except Exception:
        pass

    sheet_manifest = []
    try:
        if wb_orig:
            for s_name in wb_orig.sheetnames:
                ws = wb_orig[s_name]
                headers = []
                if ws.max_row and ws.max_row >= 1:
                    for r in range(1, min(ws.max_row + 1, 10)):
                        row_vals = [
                            str(ws.cell(r, c).value).strip()
                            for c in range(1, min((ws.max_column or 0) + 1, 50))
                            if ws.cell(r, c).value is not None and str(ws.cell(r, c).value).strip()
                        ]
                        if len(row_vals) >= 2:
                            headers = row_vals
                            break
                sheet_entry = {
                    "name": s_name,
                    "max_row": ws.max_row,
                    "max_column": ws.max_column,
                    "headers": headers
                }
                # Count currency formatted cells for manifest
                sheet_curr_cnt = 0
                for r_idx in range(1, min(ws.max_row or 50, 60) + 1):
                    for c_idx in range(1, min(ws.max_column or 20, 20) + 1):
                        c_val = ws.cell(r_idx, c_idx).value
                        if c_val is not None and isinstance(c_val, (int, float)):
                            c_fmt = str(ws.cell(r_idx, c_idx).number_format or '')
                            if any(sym in c_fmt for sym in ["$", "₹", "€", "£", "¥", "Rs", "INR", "USD", "EUR"]) or "[$" in c_fmt or "#,##" in c_fmt:
                                sheet_curr_cnt += 1
                sheet_entry["currency_cells_count"] = sheet_curr_cnt
                if hasattr(ws, "tables") and ws.tables:
                    sheet_entry["tables"] = list(ws.tables.keys())
                sheet_manifest.append(sheet_entry)
    except Exception:
        pass

    workbook_manifest = {
        "sheets": sheet_manifest,
        "detected_formulas": sorted(list(detected_formulas)),
        "detected_pivots": detected_pivots,
        "pivot_tables": detected_pivots,
        "detected_charts": detected_charts,
        "detected_macros": macro_info.get("macro_names", []),
        "conditional_formattings": manifest_cfs,
        "calculation_warnings": calculation_warnings,
        "has_data_model": bool(macro_info.get("has_data_model") or (registry and registry.macro_info.get("has_data_model"))),
        "power_queries": macro_info.get("query_names", []) if macro_info else []
    }

    # Safely close workbooks and registry
    try:
        if registry:
            registry.close()
        if wb_orig:
            wb_orig.close()
        if wb_cached:
            wb_cached.close()
        if wb_recalc:
            wb_recalc.close()
    except Exception:
        pass

    return {
        "success": True,
        "score": total_score,
        "rubric_breakdown": rubric_breakdown,
        "rubric_breakdown_details": rubric_breakdown_details,
        "scoring_summary": scoring_summary,
        "evaluation_status": eval_status,
        "workbook_manifest": workbook_manifest,
        "registry_summary": {
            "worksheet_count": len(registry.worksheets) if registry else len(wb_orig.sheetnames),
            "table_count": len(registry.tables) if registry else 0,
            "pivot_count": len(registry.pivot_tables) if registry else len(detected_pivots),
            "chart_count": len(registry.charts) if registry else len(detected_charts),
            "query_count": len(registry.power_queries) if registry else 0,
            "has_macros": registry.macro_info.get("has_macros", False) if registry else macro_info.get("has_macros", False)
        } if registry else None,
        "detected_formulas": sorted(list(detected_formulas)),
        "detected_charts": detected_charts,
        "detected_pivots": detected_pivots,
        "detected_macros": macro_info.get("macro_names", []),
        "calculation_warnings": calculation_warnings,
        "detection_errors": detection_errors,
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
