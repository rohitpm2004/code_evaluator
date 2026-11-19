"""
comparator.py - Tier 3 Multiset & Ordered SQL Result Comparator.
Provides deterministic, mathematically sound comparison between student query output
and expected reference query results with N+1 overflow guards and type normalization.
"""

import math
import uuid
from collections import Counter
from decimal import Decimal
from datetime import date, datetime, time, timedelta, timezone
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

    # Temporal types with microsecond precision (unify aware/naive as UTC)
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            val = val.astimezone(timezone.utc).replace(tzinfo=None)
        return ('TIMESTAMP', val.strftime('%Y-%m-%d %H:%M:%S.%f'))

    if isinstance(val, date):
        return ('DATE', val.isoformat())

    if isinstance(val, time):
        return ('TIME', val.isoformat())

    if isinstance(val, timedelta):
        return ('INTERVAL', round(val.total_seconds(), 6))

    if isinstance(val, uuid.UUID):
        return ('UUID', str(val).lower())

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


def _rows_equal_with_tol(r1: Tuple[Tuple[str, Any], ...], r2: Tuple[Tuple[str, Any], ...], float_tol: float = 1e-2) -> bool:
    """Compare two normalized rows, allowing small numeric rounding differences (e.g. AVG/ROUND)."""
    if r1 == r2:
        return True
    if len(r1) != len(r2):
        return False
    for (t1, v1), (t2, v2) in zip(r1, r2):
        if (t1, v1) == (t2, v2):
            continue
        if t1 == 'NUMBER' and t2 == 'NUMBER' and isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            if abs(float(v1) - float(v2)) <= max(float_tol, 1e-4 * max(abs(float(v1)), abs(float(v2)))):
                continue
        return False
    return True


def compare_query_results(
    actual_rows: List[Sequence[Any]],
    expected_rows: List[Sequence[Any]],
    actual_columns: Optional[List[str]] = None,
    expected_columns: Optional[List[str]] = None,
    enforce_order: bool = False,
    enforce_column_names: bool = False,
    allow_extra_columns: Optional[bool] = None,
    projection_policy: str = "flexible",
    extra_column_penalty: float = 0.15,
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

        if has_extra_columns:
            is_strict = allow_extra_columns is False or (projection_policy and projection_policy.lower() == 'strict')
            if is_strict:
                return {
                    'passed': False,
                    'score_factor': 0.0,
                    'reason': (
                        f"Strict projection required: query returned extra column(s) [{', '.join(extra_col_names)}]. "
                        f"Expected exactly [{', '.join(expected_columns)}]."
                    ),
                    'actual_count': actual_count,
                    'expected_count': expected_count,
                    'mismatch_details': {
                        'expected_columns': expected_columns,
                        'actual_columns': actual_columns,
                        'extra_columns': extra_col_names,
                        'policy': 'strict'
                    }
                }

        if enforce_column_names and actual_columns and not has_extra_columns:
            act_lower = [c.lower() for c in actual_columns]
            exp_lower = [c.lower() for c in expected_columns]
            if act_lower != exp_lower:
                return {
                    'passed': False,
                    'score_factor': 0.0,
                    'reason': (
                        f"Column names mismatch: expected [{', '.join(expected_columns)}], "
                        f"received [{', '.join(actual_columns)}]. Exact column names are required."
                    ),
                    'actual_count': actual_count,
                    'expected_count': expected_count,
                    'mismatch_details': {
                        'expected_columns': expected_columns,
                        'actual_columns': actual_columns
                    }
                }

    # Determine extra columns score factor & message
    if has_extra_columns:
        if allow_extra_columns is True or (projection_policy and projection_policy.lower() == 'lenient'):
            extra_col_sf = 1.0
            extra_msg_suffix = " (extra columns permitted under lenient projection policy)."
        else:
            extra_col_sf = max(0.0, round(1.0 - extra_column_penalty, 2))
            pct = int(round(extra_col_sf * 100))
            extra_msg_suffix = f" (with extra columns via SELECT *; {pct}% credit awarded)."
    else:
        extra_col_sf = 1.0
        extra_msg_suffix = "."

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
            sf = extra_col_sf if has_extra_columns else 1.0
            r_msg = (
                f"All {actual_count} rows matched in exact order{extra_msg_suffix}"
            )
            return {
                'passed': True,
                'score_factor': sf,
                'reason': r_msg,
                'actual_count': actual_count,
                'expected_count': expected_count,
                'mismatch_details': None
            }

        # Check tie-group equivalence: identical multiset and identical values on all monotonic sort columns
        if Counter(norm_actual) == Counter(norm_expected) and actual_count > 0:
            ncols = len(norm_expected[0])
            mono_cols = []
            for c in range(ncols):
                col_vals = [r[c][1] for r in norm_expected]
                try:
                    is_asc = all(col_vals[j] <= col_vals[j + 1] for j in range(actual_count - 1))
                    is_desc = all(col_vals[j] >= col_vals[j + 1] for j in range(actual_count - 1))
                    if (is_asc or is_desc) and len(set(col_vals)) > 1:
                        mono_cols.append(c)
                except TypeError:
                    pass
            if not mono_cols or any(all(norm_actual[i][c] == norm_expected[i][c] for i in range(actual_count)) for c in mono_cols):
                sf = extra_col_sf if has_extra_columns else 1.0
                return {
                    'passed': True,
                    'score_factor': sf,
                    'reason': f"All {actual_count} rows matched in valid sorted order (tie-order equivalent){extra_msg_suffix}",
                    'actual_count': actual_count,
                    'expected_count': expected_count,
                    'mismatch_details': None
                }

        # Check tolerant numeric match in exact order (e.g., ROUND/AVG precision differences)
        if all(_rows_equal_with_tol(norm_actual[i], norm_expected[i]) for i in range(actual_count)):
            sf = extra_col_sf if has_extra_columns else 1.0
            return {
                'passed': True,
                'score_factor': sf,
                'reason': f"All {actual_count} rows matched in exact order (within numeric rounding tolerance){extra_msg_suffix}",
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
        sf = extra_col_sf if has_extra_columns else 1.0
        r_msg = (
            f"All {expected_count} rows matched expected results{extra_msg_suffix}"
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

    # Check tolerant numeric multiset match when counts match
    if actual_count == expected_count and actual_count > 0:
        unmatched_exp = list(norm_expected)
        tol_matched = True
        for act_r in norm_actual:
            idx = next((i for i, exp_r in enumerate(unmatched_exp) if _rows_equal_with_tol(act_r, exp_r)), -1)
            if idx == -1:
                tol_matched = False
                break
            unmatched_exp.pop(idx)
        if tol_matched:
            sf = extra_col_sf if has_extra_columns else 1.0
            return {
                'passed': True,
                'score_factor': sf,
                'reason': f"All {actual_count} rows matched (within numeric rounding tolerance){extra_msg_suffix}",
                'actual_count': actual_count,
                'expected_count': expected_count,
                'mismatch_details': None
            }

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
            'surplus_sample_count': len(surplus),
            'missing_sample': [[c[1] for c in r] for r in missing],
            'surplus_sample': [[c[1] for c in r] for r in surplus],
        }
    }
