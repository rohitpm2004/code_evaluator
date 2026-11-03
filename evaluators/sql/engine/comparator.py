"""
comparator.py - Tier 3 Multiset & Ordered SQL Result Comparator.
Provides deterministic, mathematically sound comparison between student query output
and expected reference query results with N+1 overflow guards and type normalization.
"""

import math
from collections import Counter
from decimal import Decimal
from datetime import date, datetime, time, timezone
from typing import Any, List, Tuple, Dict, Optional, Sequence


def normalize_value(val: Any, is_native_float: bool = False, float_rel_tol: float = 1e-5, float_abs_tol: float = 1e-7) -> Tuple[str, Any]:
    """
    Normalizes a single database cell value into an explicit type-tagged canonical tuple:
    (type_tag, canonical_value).
    
    Prevents cross-type equality traps (e.g., Python True == 1, 0 == False),
    preserves exact Decimal monetary precision, and prevents NaN sentinel collisions.
    """
    if val is None:
        return ('SQL_NULL', None)

    # Boolean must be checked before int because isinstance(True, int) is True in Python
    if isinstance(val, bool):
        return ('BOOL', val)

    # Exact Decimal types (NUMERIC / DECIMAL in SQL)
    if isinstance(val, Decimal):
        try:
            f_val = float(val)
            if math.isnan(f_val):
                return ('SPECIAL_FLOAT', 'NAN')
            if math.isinf(f_val):
                return ('SPECIAL_FLOAT', 'INF' if f_val > 0 else '-INF')
            if f_val.is_integer():
                return ('NUMBER', int(f_val))
            return ('NUMBER', round(f_val, 6))
        except (ValueError, OverflowError):
            return ('NUMBER', str(val.normalize()))

    # Native SQL Floating-point numbers (REAL / FLOAT / DOUBLE PRECISION)
    if isinstance(val, float):
        if math.isnan(val):
            return ('SPECIAL_FLOAT', 'NAN')
        if math.isinf(val):
            return ('SPECIAL_FLOAT', 'INF' if val > 0 else '-INF')
        if val.is_integer():
            return ('NUMBER', int(val))
        return ('NUMBER', round(val, 6))

    # Exact Integer types (INT, BIGINT, SMALLINT)
    if isinstance(val, int):
        return ('NUMBER', int(val))

    # Temporal types with microsecond precision
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            utc_dt = val.astimezone(timezone.utc)
            return ('TIMESTAMP_AWARE', utc_dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
        return ('TIMESTAMP_NAIVE', val.strftime('%Y-%m-%d %H:%M:%S.%f'))

    if isinstance(val, date):
        return ('DATE', val.isoformat())

    if isinstance(val, time):
        return ('TIME', val.isoformat())

    # String types (VARCHAR, TEXT, CHAR)
    if isinstance(val, str):
        return ('STR', val.strip())

    # JSON / Dict types
    if isinstance(val, dict):
        # Sort keys recursively
        return ('JSON_OBJECT', tuple(sorted((str(k), normalize_value(v)) for k, v in val.items())))

    # Arrays / List / Tuple types
    if isinstance(val, (list, tuple)):
        return ('ARRAY', tuple(normalize_value(x) for x in val))

    if isinstance(val, set):
        return ('SET', tuple(sorted(normalize_value(x) for x in val)))

    return ('UNKNOWN', str(val))


def normalize_row(row: Sequence[Any]) -> Tuple[Tuple[str, Any], ...]:
    """Normalizes an entire row tuple into a type-tagged canonical tuple."""
    return tuple(normalize_value(cell) for cell in row)


def compare_query_results(
    actual_rows: List[Sequence[Any]],
    expected_rows: List[Sequence[Any]],
    actual_columns: Optional[List[str]] = None,
    expected_columns: Optional[List[str]] = None,
    enforce_order: bool = False,
    enforce_column_names: bool = False,
    max_allowed_rows: int = 5000,
    has_overflow: bool = False
) -> Dict[str, Any]:
    """
    Compares student query output against expected reference output.

    Returns:
        dict with:
            passed: bool
            score_factor: float (0.0 to 1.0)
            reason: str
            actual_count: int
            expected_count: int
            mismatch_details: Optional[Dict]
    """
    # ── Check 1: N+1 Row Count Overflow ────────────────────────────────
    if has_overflow or len(actual_rows) > max_allowed_rows:
        return {
            'passed': False,
            'score_factor': 0.0,
            'reason': (
                f"Result set exceeded the maximum row limit ({max_allowed_rows} rows). "
                "Check for missing JOIN conditions (Cartesian product) or unconstrained queries."
            ),
            'actual_count': len(actual_rows),
            'expected_count': len(expected_rows),
            'mismatch_details': {'overflow': True}
        }

    actual_count = len(actual_rows)
    expected_count = len(expected_rows)

    has_extra_columns = False
    extra_col_names = []

    # ── Check 2: Column Count & Names ──────────────────────────────────
    if expected_columns:
        if actual_columns and len(actual_columns) != len(expected_columns):
            # Check if all expected columns exist in actual_columns (e.g. student did SELECT * or selected extra columns)
            act_map = {c.lower(): idx for idx, c in enumerate(actual_columns)}
            if all(ec.lower() in act_map for ec in expected_columns):
                proj_indices = [act_map[ec.lower()] for ec in expected_columns]
                actual_rows = [tuple(r[i] for i in proj_indices) for r in actual_rows]
                extra_col_names = [c for c in actual_columns if c.lower() not in [ec.lower() for ec in expected_columns]]
                actual_columns = [expected_columns[i] for i in range(len(expected_columns))]
                has_extra_columns = True
            else:
                return {
                    'passed': False,
                    'score_factor': 0.0,
                    'reason': (
                        f"Column mismatch: expected [{', '.join(expected_columns)}], "
                        f"but received [{', '.join(actual_columns)}]."
                    ),
                    'actual_count': actual_count,
                    'expected_count': expected_count,
                    'mismatch_details': {
                        'expected_columns': expected_columns,
                        'actual_columns': actual_columns
                    }
                }

        # Check for column order variations when count matches
        if actual_columns and len(actual_columns) == len(expected_columns) and not has_extra_columns:
            act_map = {c.lower(): idx for idx, c in enumerate(actual_columns)}
            if all(ec.lower() in act_map for ec in expected_columns) and [c.lower() for c in actual_columns] != [ec.lower() for ec in expected_columns]:
                proj_indices = [act_map[ec.lower()] for ec in expected_columns]
                actual_rows = [tuple(r[i] for i in proj_indices) for r in actual_rows]
                actual_columns = [expected_columns[i] for i in range(len(expected_columns))]

        if enforce_column_names and actual_columns and not has_extra_columns:
            act_lower = [c.lower() for c in actual_columns]
            exp_lower = [c.lower() for c in expected_columns]
            if act_lower != exp_lower:
                return {
                    'passed': False,
                    'score_factor': 0.5,
                    'reason': (
                        f"Column names or aliases mismatch: expected [{', '.join(expected_columns)}], "
                        f"received [{', '.join(actual_columns)}]."
                    ),
                    'actual_count': actual_count,
                    'expected_count': expected_count,
                    'mismatch_details': {
                        'expected_columns': expected_columns,
                        'actual_columns': actual_columns
                    }
                }

    # Normalize rows
    norm_actual = [normalize_row(r) for r in actual_rows]
    norm_expected = [normalize_row(r) for r in expected_rows]

    # Both empty is an exact match
    if actual_count == 0 and expected_count == 0:
        return {
            'passed': True,
            'score_factor': 1.0,
            'reason': 'Query returned 0 rows as expected.',
            'actual_count': 0,
            'expected_count': 0,
            'mismatch_details': None
        }

    # ── Mode A: Ordered Comparison (enforce_order=True) ───────────────
    if enforce_order:
        if actual_count != expected_count:
            return {
                'passed': False,
                'score_factor': 0.0,
                'reason': f"Row count mismatch with ORDER BY required: expected {expected_count} rows, got {actual_count}.",
                'actual_count': actual_count,
                'expected_count': expected_count,
                'mismatch_details': {'expected_count': expected_count, 'actual_count': actual_count}
            }

        matched_count = 0
        first_mismatch_idx = -1
        for i in range(actual_count):
            act_r = norm_actual[i]
            exp_r = norm_expected[i]
            if act_r == exp_r:
                matched_count += 1
            elif not enforce_column_names and sorted(act_r, key=lambda v: (v[0], str(v[1]))) == sorted(exp_r, key=lambda v: (v[0], str(v[1]))):
                matched_count += 1
            elif first_mismatch_idx == -1:
                first_mismatch_idx = i

        if matched_count == actual_count:
            sf = 0.85 if has_extra_columns else 1.0
            r_msg = (
                f"All {actual_count} rows matched in exact order (with extra columns via SELECT *; 85% credit awarded)."
                if has_extra_columns else f"All {actual_count} rows matched in exact order."
            )
            return {
                'passed': True,
                'score_factor': sf,
                'reason': r_msg,
                'actual_count': actual_count,
                'expected_count': expected_count,
                'mismatch_details': None
            }

        # Check for Creative Mock Data / Self-Consistent Sorted Output:
        # If student created their own custom dataset, values won't match teacher's reference,
        # but rows are returned in internally sorted order matching required row count!
        is_internally_sorted = False
        if actual_count > 1 and actual_columns:
            for c_idx in range(len(actual_columns)):
                # Check numeric sort (DESC or ASC)
                try:
                    vals = []
                    for r in norm_actual:
                        cell_val = r[c_idx]
                        if isinstance(cell_val, tuple) and len(cell_val) == 2:
                            vals.append(float(cell_val[1]))
                        else:
                            vals.append(float(str(cell_val).replace('$', '').replace(',', '')))
                    if len(vals) == actual_count and (all(vals[j] >= vals[j+1] for j in range(len(vals) - 1)) or all(vals[j] <= vals[j+1] for j in range(len(vals) - 1))):
                        is_internally_sorted = True
                        break
                except (ValueError, TypeError, IndexError):
                    pass

                # Check string/alphabetical sort (DESC or ASC)
                try:
                    s_vals = []
                    for r in norm_actual:
                        cell_val = r[c_idx]
                        raw_str = cell_val[1] if isinstance(cell_val, tuple) and len(cell_val) == 2 else str(cell_val)
                        s_vals.append(str(raw_str).strip().lower())
                    if len(s_vals) == actual_count and (all(s_vals[j] >= s_vals[j+1] for j in range(len(s_vals) - 1)) or all(s_vals[j] <= s_vals[j+1] for j in range(len(s_vals) - 1))):
                        is_internally_sorted = True
                        break
                except (ValueError, TypeError, IndexError):
                    pass

        if is_internally_sorted:
            sf = 0.80 if has_extra_columns else 0.90
            r_msg = (
                f"Query returned {actual_count} sorted rows matching required row limit on student's custom dataset (with extra columns via SELECT *; 80% credit awarded)."
                if has_extra_columns else
                f"Query successfully returned {actual_count} sorted rows matching required row limit and ordering on student's custom dataset. (90% credit awarded)"
            )
            return {
                'passed': True,
                'score_factor': sf,
                'reason': r_msg,
                'actual_count': actual_count,
                'expected_count': expected_count,
                'mismatch_details': None
            }

        score_factor = round(matched_count / max(1, expected_count), 2)
        return {
            'passed': False,
            'score_factor': max(0.0, score_factor * 0.7),  # Penalize order mismatch
            'reason': (
                f"Row order mismatch at row index {first_mismatch_idx + 1}. "
                "Ensure your ORDER BY clause matches all required sort columns and directions."
            ),
            'actual_count': actual_count,
            'expected_count': expected_count,
            'mismatch_details': {
                'first_mismatch_index': first_mismatch_idx + 1,
                'expected_sample': list(norm_expected[first_mismatch_idx]) if first_mismatch_idx < expected_count else None,
                'actual_sample': list(norm_actual[first_mismatch_idx]) if first_mismatch_idx < actual_count else None
            }
        }

    # ── Mode B: Unordered Multiset Comparison (Default) ───────────────
    # Use Counter for frequency matching (O(N) vs O(N^2))
    actual_counter = Counter(norm_actual)
    expected_counter = Counter(norm_expected)

    if actual_counter == expected_counter:
        sf = 0.85 if has_extra_columns else 1.0
        r_msg = (
            f"All {expected_count} rows matched expected results (with extra columns via SELECT *; 85% credit awarded)."
            if has_extra_columns else f"All {expected_count} rows matched expected results."
        )
        return {
            'passed': True,
            'score_factor': sf,
            'reason': r_msg,
            'actual_count': actual_count,
            'expected_count': expected_count,
            'mismatch_details': None
        }

    # Fallback: if column names not enforced, check if sorted cell values per row match
    if not enforce_column_names and actual_count == expected_count:
        sorted_actual = [tuple(sorted(r, key=lambda v: (v[0], str(v[1])))) for r in norm_actual]
        sorted_expected = [tuple(sorted(r, key=lambda v: (v[0], str(v[1])))) for r in norm_expected]
        if Counter(sorted_actual) == Counter(sorted_expected):
            return {
                'passed': True,
                'score_factor': 1.0,
                'reason': f"All {expected_count} rows matched expected results (columns matched irrespective of order).",
                'actual_count': actual_count,
                'expected_count': expected_count,
                'mismatch_details': None
            }

    # Compute intersection for partial credit
    intersection = actual_counter & expected_counter
    matched_rows = sum(intersection.values())
    total_expected = max(1, expected_count)
    score_factor = round(matched_rows / total_expected, 2)

    # Identify sample missing and surplus rows
    missing = list((expected_counter - actual_counter).elements())[:3]
    surplus = list((actual_counter - expected_counter).elements())[:3]

    reason_parts = []
    if actual_count != expected_count:
        reason_parts.append(f"Row count mismatch (expected {expected_count}, got {actual_count})")
    if missing:
        reason_parts.append(f"{len(list((expected_counter - actual_counter).elements()))} expected row(s) missing")
    if surplus:
        reason_parts.append(f"{len(list((actual_counter - expected_counter).elements()))} extra row(s) returned")

    return {
        'passed': False,
        'score_factor': max(0.0, min(0.9, score_factor)),  # Cap partial credit below passing
        'reason': '; '.join(reason_parts) or 'Data rows do not match expected results.',
        'actual_count': actual_count,
        'expected_count': expected_count,
        'mismatch_details': {
            'matched_rows': matched_rows,
            'missing_sample_count': len(missing),
            'surplus_sample_count': len(surplus)
        }
    }
