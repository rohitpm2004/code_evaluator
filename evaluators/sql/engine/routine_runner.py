"""
routine_runner.py - Stored Functions & Procedures Evaluation Harness (Phase 2).
Automates multi-permutation execution and assertion of PostgreSQL functions and procedures:
  1. Scalar and table-valued functions (RETURNS TABLE / SETOF).
  2. Stored procedures with IN, OUT, and INOUT arguments (CALL ...).
  3. Edge-case and boundary parameter matrices.
  4. Exception handling validation (expectException: True).
  5. State verification after procedure execution.
"""

import logging
import re
from typing import Dict, Any, List, Optional, Tuple, Sequence
from comparator import compare_query_results, normalize_value

logger = logging.getLogger("routine_runner")


def _format_sql_literal(val: Any) -> str:
    """Safely formats Python values into SQL literal arguments."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        # Escape single quotes
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(val, (list, tuple)):
        # Format as ARRAY[val1, val2]
        elements = ", ".join(_format_sql_literal(x) for x in val)
        return f"ARRAY[{elements}]"
    if isinstance(val, dict):
        import json
        escaped = json.dumps(val).replace("'", "''")
        return f"'{escaped}'::jsonb"
    return f"'{str(val).replace("'", "''")}'"


def evaluate_routine_test_case(
    runner,
    ref_runner=None,
    tc: Optional[Dict[str, Any]] = None,
    schema_name: str = ""
) -> Dict[str, Any]:
    """
    Executes and evaluates stored function or procedure test cases.

    Returns:
        dict: {
            'passed': bool,
            'score_factor': float (0.0 to 1.0),
            'reason': str,
            'total_invocations': int,
            'passed_invocations': int,
            'details': List[Dict]
        }
    """
    tc = tc or {}
    routine_name = tc.get('routineName') or tc.get('functionName') or tc.get('procedureName') or ''
    routine_type = tc.get('routineType', 'function').lower()
    invocations = tc.get('invocations') or tc.get('testCases') or []

    if not routine_name and not invocations:
        # Fallback: check if referenceQuery or query contains function call
        q = tc.get('referenceQuery') or tc.get('query') or ''
        m = re.search(r'\b(?:SELECT\s+(?:\*\s+FROM\s+)?|CALL\s+)([a-zA-Z0-9_]+)\s*\(', q, re.I)
        if m:
            routine_name = m.group(1)
        if 'CALL' in q.upper():
            routine_type = 'procedure'

    if not routine_name:
        return {
            'passed': False,
            'score_factor': 0.0,
            'reason': "Routine evaluation error: routineName not specified in test case manifest.",
            'total_invocations': 0,
            'passed_invocations': 0,
            'details': []
        }

    # Inspect routine signature from information_schema if possible
    is_table_func = False
    try:
        check_q = f"""
            SELECT routine_type, data_type 
            FROM information_schema.routines 
            WHERE routine_name = '{routine_name.lower()}' 
              AND routine_schema = current_schema();
        """
        rows, _, _, _ = runner.execute_query(check_q, max_rows=10, is_student=False)
        if rows:
            r_type, d_type = rows[0]
            if str(r_type).upper() == 'PROCEDURE':
                routine_type = 'procedure'
            if str(d_type).lower() in ('record', 'table'):
                is_table_func = True
    except Exception as inspect_err:
        logger.debug(f"Routine inspection notice: {inspect_err}")

    # Synthesize single default invocation if none provided
    if not invocations:
        call_stmt = tc.get('call') or tc.get('referenceQuery') or tc.get('query')
        if call_stmt:
            invocations = [{'call': call_stmt, 'expectedReturn': tc.get('expectedReturn')}]
        else:
            invocations = [{'args': [], 'expectedReturn': tc.get('expectedReturn')}]

    passed_count = 0
    total_count = len(invocations)
    invocation_details = []

    for idx, inv in enumerate(invocations, start=1):
        inv_desc = inv.get('description', f"Invocation {idx}")
        args = inv.get('args', [])
        expect_exception = inv.get('expectException', False)
        custom_call = inv.get('call')

        if custom_call:
            call_sql = custom_call
        else:
            arg_str = ", ".join(_format_sql_literal(a) for a in args)
            if routine_type == 'procedure':
                call_sql = f"CALL {routine_name}({arg_str});"
            elif is_table_func or inv.get('returnsTable'):
                call_sql = f"SELECT * FROM {routine_name}({arg_str});"
            else:
                call_sql = f"SELECT {routine_name}({arg_str}) AS result;"

        actual_result = None
        actual_error = None
        inv_passed = False
        fail_reason = ""

        try:
            if routine_type == 'procedure':
                runner.execute_script(call_sql)
                # Verify state after procedure call if verificationQuery is provided
                verify_q = inv.get('verifyQuery') or tc.get('verifyQuery')
                if verify_q:
                    v_rows, v_cols, _, _ = runner.execute_query(verify_q, max_rows=100, is_student=False)
                    exp_verify = inv.get('expectedVerifyRows')
                    if exp_verify is not None:
                        comp = compare_query_results(v_rows, exp_verify, v_cols, inv.get('expectedColumns') or v_cols)
                        inv_passed = comp['passed']
                        if not inv_passed:
                            fail_reason = f"State verification failed: {comp['reason']}"
                    elif ref_runner:
                        # Dual-runner verification
                        ref_runner.execute_script(call_sql)
                        ref_v_rows, ref_v_cols, _, _ = ref_runner.execute_query(verify_q, max_rows=100, is_student=False)
                        comp = compare_query_results(v_rows, ref_v_rows, v_cols, ref_v_cols)
                        inv_passed = comp['passed']
                        if not inv_passed:
                            fail_reason = f"Dual-runner state mismatch: {comp['reason']}"
                    else:
                        inv_passed = True
                else:
                    inv_passed = True
            else:
                # Function execution
                f_rows, f_cols, _, _ = runner.execute_query(call_sql, max_rows=100, is_student=True)
                actual_result = f_rows

                if expect_exception:
                    inv_passed = False
                    fail_reason = "Expected exception was not raised by function call."
                else:
                    exp_ret = inv.get('expectedReturn')
                    if exp_ret is not None:
                        # Normalize and compare
                        if is_table_func or (isinstance(exp_ret, list) and exp_ret and isinstance(exp_ret[0], (list, tuple))):
                            comp = compare_query_results(f_rows, exp_ret, f_cols, inv.get('expectedColumns') or f_cols)
                            inv_passed = comp['passed']
                            if not inv_passed:
                                fail_reason = comp['reason']
                        else:
                            # Scalar return comparison
                            scalar_actual = f_rows[0][0] if f_rows and f_rows[0] else None
                            norm_act = normalize_value(scalar_actual)
                            norm_exp = normalize_value(exp_ret)
                            if norm_act == norm_exp:
                                inv_passed = True
                            elif norm_act[0] == 'NUMBER' and norm_exp[0] == 'NUMBER':
                                try:
                                    inv_passed = abs(float(norm_act[1]) - float(norm_exp[1])) <= 1e-4
                                except Exception:
                                    inv_passed = False
                            if not inv_passed:
                                fail_reason = f"Expected return {exp_ret}, received {scalar_actual}."
                    elif ref_runner:
                        # Dual-runner ground truth execution
                        ref_rows, ref_cols, _, _ = ref_runner.execute_query(call_sql, max_rows=100, is_student=False)
                        comp = compare_query_results(f_rows, ref_rows, f_cols, ref_cols)
                        inv_passed = comp['passed']
                        if not inv_passed:
                            fail_reason = f"Output did not match reference routine: {comp['reason']}"
                    else:
                        inv_passed = len(f_rows) > 0
        except Exception as exec_err:
            actual_error = str(exec_err)
            if expect_exception:
                inv_passed = True
            else:
                inv_passed = False
                fail_reason = f"Runtime execution error: {actual_error}"

        if inv_passed:
            passed_count += 1

        invocation_details.append({
            'index': idx,
            'description': inv_desc,
            'call': call_sql,
            'passed': inv_passed,
            'reason': fail_reason or "Output matched expected specification.",
            'error': actual_error
        })

    score_factor = round(passed_count / max(1, total_count), 2)
    overall_passed = (passed_count == total_count)

    summary_reason = (
        f"All {total_count} routine test cases passed successfully for {routine_type} '{routine_name}'."
        if overall_passed else
        f"{passed_count}/{total_count} routine test cases passed for {routine_type} '{routine_name}'. "
        + (invocation_details[0]['reason'] if invocation_details and not invocation_details[0]['passed'] else "")
    )

    return {
        'passed': overall_passed,
        'score_factor': score_factor,
        'reason': summary_reason,
        'total_invocations': total_count,
        'passed_invocations': passed_count,
        'details': invocation_details
    }

