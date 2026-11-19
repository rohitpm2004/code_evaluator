"""
concurrency_runner.py - Multi-Session Concurrency, Transaction (TCL) & DCL/RLS Engine (Phase 4).
Supports:
  1. Multi-session interleaved transaction testing (Session A / Session B).
  2. Transaction isolation level verification (READ COMMITTED, REPEATABLE READ, SERIALIZABLE).
  3. Rollback & Savepoint verification (verifying uncommitted state discarded).
  4. Database Roles & Permission testing (GRANT, REVOKE).
  5. Row-Level Security (RLS) tenant isolation verification.
"""

import logging
from typing import Dict, Any, List, Optional
from db_executor import connect_postgres, get_db_connection_params
from comparator import compare_query_results

logger = logging.getLogger("concurrency_runner")


def evaluate_transaction_test_case(
    runner,
    ref_runner=None,
    tc: Optional[Dict[str, Any]] = None,
    schema_name: str = ""
) -> Dict[str, Any]:
    """
    Executes multi-session concurrency steps across Session A and Session B,
    verifying transaction isolation and final database state.
    """
    tc = tc or {}
    steps = tc.get('steps') or []
    verify_q = tc.get('finalVerifyQuery') or tc.get('verifyQuery') or tc.get('referenceQuery') or ''
    exp_rows = tc.get('expectedRows')
    exp_cols = tc.get('expectedColumns')

    if not steps:
        # Single-session rollback verification fallback
        rollback_sql = tc.get('transactionSql') or tc.get('statement') or ''
        if rollback_sql:
            steps = [{'session': 'A', 'sql': rollback_sql}]

    if not steps and not verify_q:
        return {
            'passed': False,
            'score_factor': 0.0,
            'reason': "Transaction evaluation error: no steps or verificationQuery configured.",
            'details': []
        }

    conn_params = get_db_connection_params()
    sessions = {}
    details = []
    overall_passed = True
    fail_reason = ""

    try:
        # Open dedicated connections for Session A and Session B
        sessions['A'] = connect_postgres(conn_params, autocommit=False)
        sessions['B'] = connect_postgres(conn_params, autocommit=False)

        # Set search_path on both sessions
        for s_name, conn in sessions.items():
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema_name}", pg_catalog;')
                conn.commit()

        # Execute interleaved steps
        for step_idx, step in enumerate(steps, start=1):
            sess_key = step.get('session', 'A').upper()
            sql = step.get('sql', '').strip()
            expect_error = step.get('expectError', False) or step.get('expectException', False)
            target_conn = sessions.get(sess_key, sessions['A'])

            step_passed = True
            err_msg = None

            try:
                with target_conn.cursor() as cur:
                    cur.execute(sql)
                    if sql.upper().startswith('COMMIT') or sql.upper().startswith('ROLLBACK'):
                        target_conn.commit() if sql.upper().startswith('COMMIT') else target_conn.rollback()
                if expect_error:
                    step_passed = False
                    fail_reason = f"Step {step_idx} ({sess_key}) succeeded unexpectedly; expected error/conflict."
            except Exception as step_err:
                err_msg = str(step_err)
                if expect_error:
                    step_passed = True
                    try:
                        target_conn.rollback()
                    except Exception:
                        pass
                else:
                    step_passed = False
                    fail_reason = f"Step {step_idx} ({sess_key}) failed: {err_msg}"

            details.append({
                'step': step_idx,
                'session': sess_key,
                'sql': sql,
                'passed': step_passed,
                'error': err_msg
            })

            if not step_passed:
                overall_passed = False
                break

        # Final state verification query
        if overall_passed and verify_q:
            v_rows, v_cols, _, _ = runner.execute_query(verify_q, max_rows=100, is_student=False)
            if exp_rows is not None:
                comp = compare_query_results(v_rows, exp_rows, v_cols, exp_cols or v_cols)
                overall_passed = comp['passed']
                if not overall_passed:
                    fail_reason = f"Post-transaction state verification failed: {comp['reason']}"
            elif ref_runner:
                ref_v_rows, ref_v_cols, _, _ = ref_runner.execute_query(verify_q, max_rows=100, is_student=False)
                comp = compare_query_results(v_rows, ref_v_rows, v_cols, ref_v_cols)
                overall_passed = comp['passed']
                if not overall_passed:
                    fail_reason = f"Post-transaction state differed from reference: {comp['reason']}"

    except Exception as e:
        overall_passed = False
        fail_reason = f"Multi-session execution error: {e}"
    finally:
        for conn in sessions.values():
            try:
                conn.close()
            except Exception:
                pass

    return {
        'passed': overall_passed,
        'score_factor': 1.0 if overall_passed else 0.0,
        'reason': fail_reason or "All transaction and multi-session concurrency steps completed with correct isolation.",
        'details': details
    }


def evaluate_dcl_test_case(
    runner,
    tc: Optional[Dict[str, Any]] = None,
    schema_name: str = ""
) -> Dict[str, Any]:
    """
    Evaluates database roles, permissions (GRANT / REVOKE), and Row-Level Security (RLS)
    by impersonating designated roles and asserting access controls.
    """
    tc = tc or {}
    role_to_test = tc.get('role') or tc.get('roleName') or 'student_eval_role'
    allowed_stmts = tc.get('allowedStatements') or tc.get('allowedQueries') or []
    prohibited_stmts = tc.get('prohibitedStatements') or tc.get('deniedStatements') or []

    # If single query specified
    single_allowed = tc.get('allowedQuery')
    if single_allowed and single_allowed not in allowed_stmts:
        allowed_stmts.append(single_allowed)
    single_denied = tc.get('deniedQuery') or tc.get('prohibitedQuery')
    if single_denied and single_denied not in prohibited_stmts:
        prohibited_stmts.append(single_denied)

    details = []
    passed_checks = 0
    total_checks = len(allowed_stmts) + len(prohibited_stmts)

    if total_checks == 0:
        # Catalog check: verify table grants in information_schema.table_privileges
        target_table = tc.get('tableName') or ''
        try:
            priv_q = f"""
                SELECT grantee, privilege_type
                FROM information_schema.table_privileges
                WHERE table_schema = current_schema()
                  AND table_name = '{target_table.lower()}';
            """
            rows, cols, _, _ = runner.execute_query(priv_q, max_rows=50, is_student=False)
            exp_privs = tc.get('expectedPrivileges')
            if exp_privs is not None:
                comp = compare_query_results(rows, exp_privs, cols, cols)
                return {
                    'passed': comp['passed'],
                    'score_factor': comp['score_factor'],
                    'reason': comp['reason'],
                    'details': [{'grantee': r[0], 'privilege': r[1]} for r in rows]
                }
            return {
                'passed': len(rows) > 0,
                'score_factor': 1.0 if rows else 0.0,
                'reason': f"Detected {len(rows)} granted privileges on '{target_table}'." if rows else "No privileges granted.",
                'details': [{'grantee': r[0], 'privilege': r[1]} for r in rows]
            }
        except Exception as priv_err:
            return {
                'passed': False,
                'score_factor': 0.0,
                'reason': f"Error inspecting permissions: {priv_err}",
                'details': []
            }

    # Verify allowed statements succeed under role
    for stmt in allowed_stmts:
        check_passed = False
        err_text = None
        try:
            wrapped_sql = f"""
                SET search_path TO "{schema_name}", pg_catalog;
                SET ROLE "{role_to_test}";
                {stmt}
                RESET ROLE;
            """
            runner.execute_script(wrapped_sql)
            check_passed = True
            passed_checks += 1
        except Exception as e:
            err_text = str(e)
            check_passed = False

        details.append({
            'type': 'ALLOWED_CHECK',
            'statement': stmt,
            'passed': check_passed,
            'error': err_text,
            'reason': "Statement executed successfully under role." if check_passed else f"Permitted statement was denied: {err_text}"
        })

    # Verify prohibited statements fail with permission denied
    for stmt in prohibited_stmts:
        check_passed = False
        err_text = None
        try:
            wrapped_sql = f"""
                SET search_path TO "{schema_name}", pg_catalog;
                SET ROLE "{role_to_test}";
                {stmt}
                RESET ROLE;
            """
            runner.execute_script(wrapped_sql)
            # If no exception, permission check failed!
            check_passed = False
        except Exception as e:
            err_text = str(e)
            if "permission denied" in err_text.lower() or "must be owner" in err_text.lower():
                check_passed = True
                passed_checks += 1
            else:
                check_passed = False

        details.append({
            'type': 'PROHIBITED_CHECK',
            'statement': stmt,
            'passed': check_passed,
            'error': err_text,
            'reason': "Permission correctly denied." if check_passed else "Security violation: prohibited statement was allowed to execute!"
        })

    score_factor = round(passed_checks / max(1, total_checks), 2)
    overall_passed = (passed_checks == total_checks)

    return {
        'passed': overall_passed,
        'score_factor': score_factor,
        'reason': (
            f"All {total_checks} permission checks passed for role '{role_to_test}'."
            if overall_passed else
            f"{passed_checks}/{total_checks} permission checks passed for role '{role_to_test}'."
        ),
        'total_checks': total_checks,
        'passed_checks': passed_checks,
        'details': details
    }

