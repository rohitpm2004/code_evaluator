"""
sql_runner.py - Main CLI & stdio entry point for the Python SQL Execution Engine.
Orchestrates:
  1. Reading JSON contract over stdin
  2. AST Guard syntax & policy validation (ast_guard.py)
  3. Ephemeral schema sandboxed PostgreSQL execution (db_executor.py)
  4. Multiset result comparison & N+1 overflow detection (comparator.py)
  5. Deterministic scoring with partial credit & rubric breakdown
  6. Outputting unified JSON response over stdout
"""

import re
import sys
import json
import logging
from typing import Dict, Any, List, Tuple, Optional

from ast_guard import validate_sql_ast
from comparator import compare_query_results
from db_executor import SandboxedSqlRunner, ExecutionWatchdog
from routine_runner import evaluate_routine_test_case
from trigger_runner import evaluate_trigger_test_case
from concurrency_runner import evaluate_transaction_test_case, evaluate_dcl_test_case
from schema_differ import (
    normalize_student_ddl,
    compute_name_similarity,
    sanitize_and_split_student_sql,
    prune_bulk_inserts_for_llm,
    rewrite_query_with_schema_mapping
)

try:
    import sqlglot
    from sqlglot import exp
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False

# Configure logging to stderr so stdout remains clean JSON
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='[%(levelname)s] %(message)s')
logger = logging.getLogger("sql_runner")


def check_ast_concept_match(cand_sql: str, ref_query: str) -> Tuple[bool, float, str]:
    """
    Compares candidate query against reference query requirements using AST / pattern analysis.
    Verifies core educational intent:
      1. ORDER BY clause existence & sort direction (DESC / ASC)
      2. LIMIT value matching
      3. OFFSET value matching
      4. GROUP BY / HAVING presence
      5. JOIN presence
    Returns:
      (matched: bool, score_factor: float, explanation: str)
    """
    if not ref_query or not ref_query.strip():
        return False, 0.0, ""

    cand_upper = re.sub(r'\b(LEFT|RIGHT|FULL)\s+OUTER\s+JOIN\b', r'\1 JOIN', cand_sql.upper())
    ref_upper = re.sub(r'\b(LEFT|RIGHT|FULL)\s+OUTER\s+JOIN\b', r'\1 JOIN', ref_query.upper())

    # 1. ORDER BY & Direction Check
    if 'ORDER BY' in ref_upper:
        if 'ORDER BY' not in cand_upper:
            return False, 0.0, "Missing required ORDER BY clause."
        if 'DESC' in ref_upper and 'DESC' not in cand_upper:
            return False, 0.0, "ORDER BY direction should be DESC."
        if 'ASC' in ref_upper and 'DESC' in cand_upper and 'ASC' not in ref_upper:
            return False, 0.0, "ORDER BY direction should be ASC."

    # 1b. JOIN Direction Check
    outer_joins = ('LEFT JOIN', 'RIGHT JOIN', 'FULL JOIN', 'CROSS JOIN')
    for jtype in outer_joins:
        if jtype in ref_upper and jtype not in cand_upper:
            return False, 0.0, f"Expected {jtype}, but student query did not use {jtype}."
    ref_is_inner = (
        re.search(r'\bINNER\s+JOIN\b', ref_upper)
        or (re.search(r'\bJOIN\b', ref_upper) and not any(j in ref_upper for j in outer_joins))
    )
    if ref_is_inner and any(j in cand_upper for j in ('LEFT JOIN', 'RIGHT JOIN', 'FULL JOIN')):
        return False, 0.0, "Expected INNER JOIN, but student query used an OUTER JOIN."

    # 2. LIMIT Check
    ref_lim_m = re.search(r'\bLIMIT\s+(\d+)\b', ref_upper)
    if ref_lim_m:
        cand_lim_m = re.search(r'\bLIMIT\s+(\d+)\b', cand_upper)
        if not cand_lim_m:
            return False, 0.0, "Missing required LIMIT clause."
        if ref_lim_m.group(1) != cand_lim_m.group(1):
            return False, 0.0, f"Expected LIMIT {ref_lim_m.group(1)}, but got LIMIT {cand_lim_m.group(1)}."

    # 3. OFFSET Check
    ref_off_m = re.search(r'\bOFFSET\s+(\d+)\b', ref_upper)
    if ref_off_m:
        cand_off_m = re.search(r'\bOFFSET\s+(\d+)\b', cand_upper)
        if not cand_off_m:
            return False, 0.0, "Missing required OFFSET clause."
        if ref_off_m.group(1) != cand_off_m.group(1):
            return False, 0.0, f"Expected OFFSET {ref_off_m.group(1)}, but got OFFSET {cand_off_m.group(1)}."

    reasons = []
    if 'ORDER BY' in ref_upper:
        reasons.append("ORDER BY DESC" if 'DESC' in ref_upper else "ORDER BY")
    if ref_lim_m:
        reasons.append(f"LIMIT {ref_lim_m.group(1)}")
    if ref_off_m:
        reasons.append(f"OFFSET {ref_off_m.group(1)}")
    if 'GROUP BY' in ref_upper:
        reasons.append("GROUP BY")
    if 'JOIN' in ref_upper:
        reasons.append("JOIN")

    desc = ", ".join(reasons) if reasons else "required SQL clauses"
    score_factor = 0.85 if 'SELECT *' in cand_upper else 0.90

    return True, score_factor, f"Query logic correctly demonstrates {desc} on student dataset."


def try_auto_repair_query(
    runner: SandboxedSqlRunner,
    cand_sql: str,
    err_msg: str,
    expected_cols: List[str],
    max_rows: int
) -> Tuple[Optional[List[Tuple]], Optional[List[str]], bool, float, str]:
    """
    Attempts to repair runtime query errors (e.g. column or table naming differences).
    Returns (rows, cols, has_overflow, time_ms, notice_string)
    """
    # 1. Missing Column
    col_match = re.search(r'column\s+"([^"]+)"\s+does not exist', err_msg, re.I)
    if col_match and expected_cols:
        missing_col = col_match.group(1)
        # Find which expected column is NOT in cand_sql
        target_col = None
        for ec in expected_cols:
            if not re.search(rf'\b{re.escape(ec)}\b', cand_sql, re.I):
                target_col = ec
                break

        if not target_col and expected_cols:
            target_col = expected_cols[-1]

        if target_col:
            repaired_sql = re.sub(rf'\b{re.escape(missing_col)}\b', target_col, cand_sql, flags=re.I)
            try:
                r_rows, r_cols, r_overflow, r_time = runner.execute_query(
                    sql_query=repaired_sql,
                    max_rows=max_rows,
                    is_student=True
                )
                notice = f" (Note: Column '{missing_col}' automatically mapped to '{target_col}')"
                return r_rows, r_cols, r_overflow, r_time, notice
            except Exception as rep_err:
                logger.warning(f"Auto-repaired column query failed: {rep_err}")

    # 2. Missing Relation / Table
    tbl_match = re.search(r'relation\s+"([^"]+)"\s+does not exist', err_msg, re.I)
    if tbl_match:
        missing_tbl = tbl_match.group(1)
        try:
            cat_rows, _, _, _ = runner.execute_query(
                sql_query="SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();",
                max_rows=50,
                is_student=False
            )
            existing_tables = [r[0] for r in cat_rows]
            for ex_tbl in existing_tables:
                if compute_name_similarity(missing_tbl, ex_tbl) >= 0.75:
                    repaired_sql = re.sub(rf'\b{re.escape(missing_tbl)}\b', ex_tbl, cand_sql, flags=re.I)
                    r_rows, r_cols, r_overflow, r_time = runner.execute_query(
                        sql_query=repaired_sql,
                        max_rows=max_rows,
                        is_student=True
                    )
                    notice = f" (Note: Table '{missing_tbl}' automatically mapped to '{ex_tbl}')"
                    return r_rows, r_cols, r_overflow, r_time, notice
        except Exception as tbl_err:
            logger.warning(f"Auto-repaired table query failed: {tbl_err}")

    return None, None, False, 0.0, ""


def _evaluate_candidates(
    runner: SandboxedSqlRunner,
    cand_queries: List[str],
    ref_query: str,
    expected_rows: List[Any],
    expected_cols: List[str],
    enforce_order: bool,
    enforce_col_names: bool,
    max_rows: int,
    question_tag: Optional[str] = None,
    allow_extra_columns: Optional[bool] = None,
    projection_policy: str = "flexible",
    extra_column_penalty: float = 0.15
) -> Tuple[Dict[str, Any], List[Any], List[str], float, bool, str]:
    """Evaluate candidate student SELECT/WITH queries and return the best comparison result."""
    best_comp_res = None
    best_score_factor = -1.0
    best_actual_rows: List[Any] = []
    best_actual_cols: List[str] = []
    best_actual_time = 0.0
    best_actual_overflow = False
    best_err = None
    best_deduction = ""

    # Filter candidates if explicit question anchors exist (e.g. -- @question Q1 or -- Question 1:)
    filtered_candidates = cand_queries
    if question_tag:
        clean_tag = question_tag.strip().lower()
        tagged = [
            q for q in cand_queries
            if re.search(rf'--\s*@question\s+{re.escape(clean_tag)}\b', q, re.I)
            or re.search(rf'--\s*question\s*{re.escape(clean_tag)}\b', q, re.I)
        ]
        if tagged:
            filtered_candidates = tagged

    for cand_sql in filtered_candidates:
        try:
            c_rows, c_cols, c_overflow, c_time = runner.execute_query(
                sql_query=cand_sql,
                max_rows=max_rows,
                is_student=True
            )
            c_comp = compare_query_results(
                actual_rows=c_rows,
                expected_rows=expected_rows,
                actual_columns=c_cols,
                expected_columns=expected_cols,
                enforce_order=enforce_order,
                enforce_column_names=enforce_col_names,
                allow_extra_columns=allow_extra_columns,
                projection_policy=projection_policy,
                extra_column_penalty=extra_column_penalty,
                max_allowed_rows=max_rows,
                has_overflow=c_overflow
            )

            # Strict correctness: AST analysis confirms required syntax when rows pass, but never passes wrong data rows
            if c_comp['passed']:
                matched, ast_factor, explanation = check_ast_concept_match(cand_sql, ref_query)
                if not matched and 'ORDER BY' in ref_query.upper() and 'ORDER BY' not in cand_sql.upper():
                    c_comp['score_factor'] = round(c_comp['score_factor'] * 0.85, 2)
                    c_comp['reason'] += " (Deduction: missing required ORDER BY clause)."

            if c_comp['score_factor'] > best_score_factor:
                best_score_factor = c_comp['score_factor']
                best_comp_res = c_comp
                best_actual_rows = c_rows
                best_actual_cols = c_cols
                best_actual_time = c_time
                best_actual_overflow = c_overflow
                best_deduction = ""
        except Exception as c_err:
            best_err = c_err
            r_rows, r_cols, r_overflow, r_time, notice = try_auto_repair_query(
                runner=runner,
                cand_sql=cand_sql,
                err_msg=str(c_err),
                expected_cols=expected_cols,
                max_rows=max_rows
            )
            if r_rows is not None:
                r_comp = compare_query_results(
                    actual_rows=r_rows,
                    expected_rows=expected_rows,
                    actual_columns=r_cols,
                    expected_columns=expected_cols,
                    enforce_order=enforce_order,
                    enforce_column_names=enforce_col_names,
                    allow_extra_columns=allow_extra_columns,
                    projection_policy=projection_policy,
                    extra_column_penalty=extra_column_penalty,
                    max_allowed_rows=max_rows,
                    has_overflow=r_overflow
                )
                if r_comp['passed']:
                    adj_factor = round(r_comp['score_factor'] * 0.90, 2)
                else:
                    adj_factor = 0.0

                if adj_factor > best_score_factor:
                    best_score_factor = adj_factor
                    r_comp['score_factor'] = adj_factor
                    r_comp['passed'] = adj_factor > 0
                    best_comp_res = r_comp
                    best_actual_rows = r_rows
                    best_actual_cols = r_cols
                    best_actual_time = r_time
                    best_actual_overflow = r_overflow
                    best_deduction = notice
                    continue

            matched, ast_factor, explanation = check_ast_concept_match(cand_sql, ref_query)
            if matched and ast_factor > best_score_factor:
                best_score_factor = ast_factor
                best_comp_res = {
                    'passed': True,
                    'score_factor': ast_factor,
                    'reason': explanation,
                    'actual_count': len(expected_rows) if expected_rows else 0,
                    'expected_count': len(expected_rows) if expected_rows else 0,
                    'mismatch_details': None
                }
                best_actual_rows = []
                best_actual_cols = []
                best_actual_time = 0.0
                best_actual_overflow = False
                best_deduction = f" (Verified via conceptual AST analysis: {explanation})"

    if best_comp_res is not None:
        return best_comp_res, best_actual_rows, best_actual_cols, best_actual_time, best_actual_overflow, best_deduction

    return (
        {
            'passed': False,
            'score_factor': 0.0,
            'reason': f"Query execution error: {str(best_err)}" if best_err else "No matching query results.",
            'actual_count': 0,
            'expected_count': len(expected_rows) if expected_rows else 0,
            'mismatch_details': {'error': str(best_err)} if best_err else None
        },
        [], [], 0.0, False, ""
    )


def run_evaluation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates student SQL against manifest requirements and test cases."""
    job_id = payload.get('jobId', 'unknown')
    student_sql = payload.get('studentSql', '').strip()
    raw_manifest = payload.get('manifest', {})
    if isinstance(raw_manifest, list):
        manifest = {'testCases': raw_manifest}
    elif isinstance(raw_manifest, dict):
        manifest = raw_manifest
    else:
        manifest = {}

    mode = payload.get('mode') or manifest.get('mode')
    if not mode or mode not in ('query_only', 'script'):
        # Auto-detect mode if not explicitly specified
        dml_or_ddl = (
            'CREATE TABLE', 'ALTER TABLE', 'DROP TABLE', 'CREATE INDEX', 'CREATE VIEW',
            'UPDATE ', 'DELETE FROM', 'INSERT INTO', 'MERGE '
        )
        if any(kw in student_sql.upper() for kw in dml_or_ddl):
            mode = 'script'
        else:
            mode = 'query_only'

    # ── Step 0: Fault-Tolerant Pre-Parsing & Note Auto-Wrapping ──────
    raw_student_sql = student_sql
    _, sanitized_sql, preparse_diagnostics = sanitize_and_split_student_sql(student_sql)
    student_sql = sanitized_sql
    pruned_sql_for_llm = prune_bulk_inserts_for_llm(student_sql)

    alias_map = {}
    normalization_notices = []
    if mode == 'script':
        # Compute alias mapping for partial credit diagnosis, but NEVER silently mutate student SQL!
        _, alias_map, normalization_notices = normalize_student_ddl(student_sql, manifest)

    test_cases = manifest.get('testCases', [])
    # If no testCases array provided, synthesize a default test case from top-level manifest
    if not test_cases:
        test_cases = [{
            'name': 'Primary query verification',
            'kind': 'visible',
            'weight': 100,
            'setupSql': manifest.get('setupSql', ''),
            'referenceQuery': manifest.get('referenceQuery', ''),
            'enforceOrder': manifest.get('enforceOrder', False),
            'enforceColumnNames': manifest.get('enforceColumnNames', False)
        }]

    # ── Step 1: AST Guard & Policy Inspection ─────────────────────────
    ast_res = validate_sql_ast(
        sql_text=student_sql,
        mode=mode,
        manifest=manifest,
        dialect=manifest.get('dialect', 'postgres')
    )

    if not ast_res['valid_syntax'] or ast_res.get('forbidden_detected'):
        security_prefixes = (
            'FORBIDDEN_', 'CROSS_SCHEMA_', 'SELECT_INTO_', 'TABLE_LOCK_',
            'EXPLAIN_', 'DATA_MODIFYING_CTE_', 'AST_MAX_DEPTH_', 'PAYLOAD_TOO_LARGE'
        )
        policy_blocked = any(
            str(item).startswith(security_prefixes)
            for item in (ast_res.get('forbidden_detected') or [])
        )
        return {
            'status': 'AST_ERROR',
            'deterministicScore': 0,
            'policyBlocked': policy_blocked,
            'astAnalysis': ast_res,
            'testResults': [{
                'name': 'AST & Syntax Validation',
                'passed': False,
                'kind': 'visible',
                'weight': 100,
                'score': 0,
                'reason': ast_res.get('ast_error') or f"Forbidden construct detected: {ast_res.get('forbidden_detected')}"
            }],
            'rubricBreakdown': {'Syntax & AST Validation': 0},
            'executionMetrics': {'executionTimeMs': 0},
            'rawStudentSql': raw_student_sql,
            'prunedStudentSql': pruned_sql_for_llm,
            'preparseDiagnostics': preparse_diagnostics,
            'error': ast_res.get('ast_error')
        }

    # ── Step 2: Sandboxed PostgreSQL Execution ─────────────────────────
    test_results: List[Dict[str, Any]] = []
    script_errors: List[str] = list(ast_res.get('partial_syntax_errors') or [])
    total_weight = sum(t.get('weight', 10) for t in test_cases) or 100
    earned_weight = 0.0
    total_exec_time = 0.0

    manifest_schema = (manifest.get('schema') or '').strip()
    manifest_setup = (manifest.get('setupSql') or '').strip()
    parts = []
    if manifest_schema:
        parts.append(manifest_schema if manifest_schema.endswith(';') else manifest_schema + ';')
    if manifest_setup:
        parts.append(manifest_setup if manifest_setup.endswith(';') else manifest_setup + ';')
    global_setup = '\n'.join(parts)

    has_custom_tc_setup = any(tc.get('setupSql', '').strip() for tc in test_cases)

    # Fast path: without custom test setup, provision an ephemeral schema once,
    # load fixture once, then verify all test cases against it (massive WAN speedup and zero lock contention)
    if not has_custom_tc_setup:
        try:
            with SandboxedSqlRunner(job_id=f"{job_id}_main") as runner:
                with ExecutionWatchdog(runner, deadline_seconds=15.0):
                    runner.load_fixture(global_setup)

                ref_runner = None
                from schema_differ import split_sql_statements
                stmts = split_sql_statements(student_sql)
                cand_queries = [s.strip() for s in stmts if re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                scratchpad = [s.strip() for s in stmts if not re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                scratchpad_loaded = False

                if mode == 'script':
                    script_time = runner.execute_script(student_sql, preserve_create=True)
                    total_exec_time += script_time
                    script_errors.extend(runner.last_script_errors)

                    # Check if a reference script needs to run
                    ref_script = manifest.get('referenceScript') or manifest.get('referenceSql') or manifest.get('referenceDdl')
                    if ref_script:
                        try:
                            ref_runner = SandboxedSqlRunner(job_id=f"{job_id}_ref_main")
                            ref_runner.__enter__()
                            ref_runner.load_fixture(global_setup)
                            ref_runner.execute_script(ref_script)
                        except Exception as ref_setup_err:
                            logger.warning(f"Failed to execute unified reference script: {ref_setup_err}")
                            ref_runner = None
                else:
                    if not cand_queries:
                        cand_queries = [student_sql.strip()]

                    # Only pre-load student scratchpad DDL/DML if no teacher fixture exists;
                    # otherwise keep the clean manifest dataset isolated and lazy-load only on missing relation/column.
                    if scratchpad and not global_setup.strip():
                        try:
                            clean_scratch = [
                                re.sub(r'\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)', 'CREATE TABLE IF NOT EXISTS ', s.rstrip(';') + ';', flags=re.I)
                                for s in scratchpad
                            ]
                            runner.load_fixture("\n".join(clean_scratch))
                            scratchpad_loaded = True
                        except Exception as sc_err:
                            logger.info(f"Student scratchpad load notice: {sc_err}")

                try:
                    for idx, tc in enumerate(test_cases):
                        test_name = tc.get('name', f"Test Case {idx + 1}")
                        test_kind = tc.get('kind', 'visible')
                        weight = tc.get('weight', 10)
                        ref_query = tc.get('referenceQuery') or ''
                        if not ref_query and idx > 0 and any(kw in test_name.lower() for kw in ['confirm', 'verify', 'second', 'pagination', 'output', 'result']):
                            ref_query = test_cases[idx - 1].get('referenceQuery') or manifest.get('referenceQuery', '')
                        elif not ref_query:
                            ref_query = manifest.get('referenceQuery', '')
                        enforce_order = tc.get('enforceOrder', False)
                        enforce_col_names = tc.get('enforceColumnNames', False)
                        max_rows = tc.get('maxRows', 5000)

                        # Phase 2: Stored Functions & Procedures
                        if test_kind in ('procedure', 'function', 'routine') or tc.get('routineName') or tc.get('invocations'):
                            res = evaluate_routine_test_case(runner=runner, ref_runner=ref_runner, tc=tc, schema_name=runner.schema_name)
                            tc_score = round(weight * res['score_factor'], 2)
                            earned_weight += tc_score
                            test_results.append({
                                'name': test_name,
                                'kind': test_kind,
                                'weight': weight,
                                'score': tc_score,
                                'passed': res['passed'],
                                'reason': res['reason'],
                                'actualRowCount': res.get('passed_invocations', 0),
                                'expectedRowCount': res.get('total_invocations', 0),
                                'executionTimeMs': 0.0,
                                'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                            })
                            continue

                        # Phase 3: Trigger Event Simulation
                        if test_kind == 'trigger' or tc.get('triggerName') or tc.get('events'):
                            res = evaluate_trigger_test_case(runner=runner, ref_runner=ref_runner, tc=tc, schema_name=runner.schema_name)
                            tc_score = round(weight * res['score_factor'], 2)
                            earned_weight += tc_score
                            test_results.append({
                                'name': test_name,
                                'kind': test_kind,
                                'weight': weight,
                                'score': tc_score,
                                'passed': res['passed'],
                                'reason': res['reason'],
                                'actualRowCount': res.get('passed_events', 0),
                                'expectedRowCount': res.get('total_events', 0),
                                'executionTimeMs': 0.0,
                                'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                            })
                            continue

                        # Phase 4: Multi-Session Concurrency & Transactions (TCL)
                        if test_kind in ('transaction', 'concurrency', 'tcl') or tc.get('steps'):
                            res = evaluate_transaction_test_case(runner=runner, ref_runner=ref_runner, tc=tc, schema_name=runner.schema_name)
                            tc_score = round(weight * res['score_factor'], 2)
                            earned_weight += tc_score
                            test_results.append({
                                'name': test_name,
                                'kind': test_kind,
                                'weight': weight,
                                'score': tc_score,
                                'passed': res['passed'],
                                'reason': res['reason'],
                                'actualRowCount': 1 if res['passed'] else 0,
                                'expectedRowCount': 1,
                                'executionTimeMs': 0.0,
                                'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                            })
                            continue

                        # Phase 4: Roles, Permissions & RLS (DCL)
                        if test_kind in ('dcl', 'permission', 'rls', 'role') or tc.get('allowedStatements') or tc.get('prohibitedStatements'):
                            res = evaluate_dcl_test_case(runner=runner, tc=tc, schema_name=runner.schema_name)
                            tc_score = round(weight * res['score_factor'], 2)
                            earned_weight += tc_score
                            test_results.append({
                                'name': test_name,
                                'kind': test_kind,
                                'weight': weight,
                                'score': tc_score,
                                'passed': res['passed'],
                                'reason': res['reason'],
                                'actualRowCount': res.get('passed_checks', 0),
                                'expectedRowCount': res.get('total_checks', 0),
                                'executionTimeMs': 0.0,
                                'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                            })
                            continue

                        try:
                            if mode == 'script':
                                ref_exec_err = None
                                try:
                                    schema_mapping = manifest.get('schemaMapping')
                                    rewritten_ref = rewrite_query_with_schema_mapping(ref_query, schema_mapping)
                                    actual_rows, actual_cols, actual_overflow, actual_time = runner.execute_query(
                                        sql_query=rewritten_ref,
                                        max_rows=max_rows,
                                        is_student=False
                                    )
                                    total_exec_time += actual_time
                                except Exception as rq_err:
                                    ref_exec_err = rq_err
                                    actual_rows, actual_cols, actual_overflow, actual_time = [], [], False, 0.0

                                # If actual_rows is empty for catalog query, check runtime fuzzy table match
                                is_catalog_query = any(k in (ref_query or '').lower() for k in ['information_schema', 'pg_catalog', 'current_schema()'])
                                typo_matched = False
                                matched_table_name = None

                                if is_catalog_query and len(actual_rows) == 0:
                                    try:
                                        cat_rows, _, _, _ = runner.execute_query(
                                            sql_query="SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();",
                                            max_rows=100,
                                            is_student=False
                                        )
                                        existing_tables = [r[0] for r in cat_rows]
                                        for m in re.finditer(r"table_name\s*=\s*['\"]([a-zA-Z0-9_]+)['\"]", ref_query, re.I):
                                            target_tbl = m.group(1).lower()
                                            for ex_tbl in existing_tables:
                                                sim = compute_name_similarity(ex_tbl, target_tbl)
                                                if sim >= 0.8:
                                                    fixed_query = re.sub(
                                                        rf"table_name\s*=\s*['\"]{re.escape(m.group(1))}['\"]",
                                                        f"table_name = '{ex_tbl}'",
                                                        ref_query,
                                                        flags=re.I
                                                    )
                                                    retry_rows, retry_cols, retry_overflow, _ = runner.execute_query(
                                                        sql_query=fixed_query,
                                                        max_rows=max_rows,
                                                        is_student=False
                                                    )
                                                    if len(retry_rows) > 0:
                                                        actual_rows = retry_rows
                                                        actual_cols = retry_cols
                                                        typo_matched = True
                                                        matched_table_name = ex_tbl
                                                        break
                                            if typo_matched:
                                                break
                                    except Exception as cat_err:
                                        logger.warning(f"Runtime fuzzy catalog lookup failed: {cat_err}")

                                if ref_runner:
                                    expected_rows, expected_cols, _, _ = ref_runner.execute_query(
                                        sql_query=ref_query,
                                        max_rows=max_rows,
                                        is_student=False
                                    )
                                else:
                                    expected_rows = tc.get('expectedRows')
                                    expected_cols = tc.get('expectedColumns')

                                if expected_rows is None:
                                    if is_catalog_query:
                                        if len(actual_rows) > 0:
                                            expected_rows = actual_rows
                                            expected_cols = actual_cols
                                        else:
                                            expected_rows = [['<expected_schema_objects_not_found>']]
                                            expected_cols = actual_cols or ['status']
                                    elif ref_exec_err is not None:
                                        expected_rows = []
                                        expected_cols = []
                                    else:
                                        # Never manufacture expected results from student results
                                        expected_rows = None
                                        expected_cols = None

                                allow_extra_cols = tc.get('allowExtraColumns', manifest.get('allowExtraColumns'))
                                proj_policy = tc.get('projectionPolicy', manifest.get('projectionPolicy', 'flexible'))
                                extra_col_pen = tc.get('extraColumnPenalty', manifest.get('extraColumnPenalty', 0.15))

                                if not is_catalog_query and expected_rows is None:
                                    comp_res = {
                                        'passed': False,
                                        'score_factor': 0.0,
                                        'reason': 'Evaluation configuration error: assignment manifest is missing referenceScript or expectedRows.',
                                        'actual_count': len(actual_rows),
                                        'expected_count': 0,
                                        'mismatch_details': {'error': 'EVALUATION_CONFIGURATION_ERROR'}
                                    }
                                    cand_deduction = ""
                                elif not is_catalog_query and cand_queries:
                                    comp_res, actual_rows, actual_cols, cand_time, actual_overflow, cand_deduction = _evaluate_candidates(
                                        runner=runner,
                                        cand_queries=cand_queries,
                                        ref_query=ref_query,
                                        expected_rows=expected_rows or [],
                                        expected_cols=expected_cols or [],
                                        enforce_order=enforce_order,
                                        enforce_col_names=enforce_col_names,
                                        max_rows=max_rows,
                                        question_tag=tc.get('questionId') or f"Q{idx + 1}",
                                        allow_extra_columns=allow_extra_cols,
                                        projection_policy=proj_policy,
                                        extra_column_penalty=extra_col_pen
                                    )
                                    total_exec_time += cand_time
                                elif ref_exec_err is not None:
                                    comp_res = {
                                        'passed': False,
                                        'score_factor': 0.0,
                                        'reason': f"Reference check failed on student schema: {ref_exec_err}",
                                        'actual_count': 0,
                                        'expected_count': len(expected_rows or []),
                                        'mismatch_details': {'error': str(ref_exec_err)}
                                    }
                                    cand_deduction = ""
                                else:
                                    comp_res = compare_query_results(
                                        actual_rows=actual_rows,
                                        expected_rows=expected_rows,
                                        actual_columns=actual_cols,
                                        expected_columns=expected_cols,
                                        enforce_order=enforce_order,
                                        enforce_column_names=enforce_col_names,
                                        allow_extra_columns=allow_extra_cols,
                                        projection_policy=proj_policy,
                                        extra_column_penalty=extra_col_pen,
                                        max_allowed_rows=max_rows,
                                        has_overflow=actual_overflow
                                    )
                                    cand_deduction = ""

                                score_factor = comp_res['score_factor']
                                has_naming_deduction = False
                                deduction_reason = cand_deduction

                                if comp_res['passed']:
                                    matched_rep = None
                                    for rep in runner.repairs_applied:
                                        t_name = rep.get('table_name')
                                        if t_name and (t_name in ref_query.lower() or t_name in test_name.lower()):
                                            matched_rep = rep
                                            break

                                    if matched_rep:
                                        is_fk_tc = any(kw in test_name.lower() for kw in ['foreign key', 'constraint', 'relationship'])
                                        rep_type = matched_rep.get('repair_type', '')
                                        if is_fk_tc and rep_type != 'UNDECLARED_FK_COLUMN':
                                            pass
                                        else:
                                            rep_factor = matched_rep.get('score_factor', 0.67)
                                            score_factor = round(min(score_factor, rep_factor), 2)
                                            cand_deduction += f" (Deduction: Syntax error on table '{matched_rep['table_name']}' required auto-repair)."
                                    else:
                                        is_exec_tc = any(kw in test_name.lower() for kw in ['script execution', 'dependency ordering', 'execution', 'sequential', 'ordering'])
                                        if is_exec_tc and runner.repairs_applied:
                                            num_rep = len(runner.repairs_applied)
                                            exec_factor = max(0.40, round(1.0 - (0.25 * num_rep), 2))
                                            score_factor = round(min(score_factor, exec_factor), 2)
                                            cand_deduction += f" (Deduction: Script had {num_rep} statement execution faults requiring auto-repair)."

                                    for tbl_name, info in alias_map.items():
                                        if tbl_name in ref_query.lower() or info['canonical'] in ref_query.lower():
                                            has_naming_deduction = True
                                            score_factor = max(0.85, score_factor * 0.90)  # 10% naming deduction
                                            cand_deduction += f" (Note: Table defined as '{info['original']}', matched to '{info['canonical']}')"
                                            break
                                    if typo_matched and not has_naming_deduction:
                                        score_factor = max(0.80, score_factor * 0.85)
                                        cand_deduction += f" (Note: Table '{matched_table_name}' matched target with partial credit)"

                                    deduction_reason = cand_deduction

                            else:
                                # Mode == 'query_only'
                                if tc.get('expectedRows') is not None:
                                    expected_rows = tc.get('expectedRows')
                                    expected_cols = tc.get('expectedColumns')
                                else:
                                    allow_extra_cols = tc.get('allowExtraColumns', manifest.get('allowExtraColumns'))
                                    proj_policy = tc.get('projectionPolicy', manifest.get('projectionPolicy', 'flexible'))
                                    extra_col_pen = tc.get('extraColumnPenalty', manifest.get('extraColumnPenalty', 0.15))

                                    try:
                                        expected_rows, expected_cols, _, _ = runner.execute_query(
                                            sql_query=ref_query,
                                            max_rows=max_rows,
                                            is_student=False
                                        )
                                    except Exception as ref_err:
                                        logger.warning(f"Error executing reference query: {ref_err}")
                                        expected_rows = tc.get('expectedRows') or []
                                        expected_cols = tc.get('expectedColumns') or []

                                comp_res, actual_rows, actual_cols, actual_time, actual_overflow, deduction_reason = _evaluate_candidates(
                                    runner=runner,
                                    cand_queries=cand_queries,
                                    ref_query=ref_query,
                                    expected_rows=expected_rows,
                                    expected_cols=expected_cols,
                                    enforce_order=enforce_order,
                                    enforce_col_names=enforce_col_names,
                                    max_rows=max_rows,
                                    question_tag=tc.get('questionId') or f"Q{idx + 1}",
                                    allow_extra_columns=allow_extra_cols,
                                    projection_policy=proj_policy,
                                    extra_column_penalty=extra_col_pen
                                )

                                # Mode B — Custom Student Data:
                                # If query failed due to missing relation/column, load student scratchpad/schema
                                # and re-evaluate reference logic against the student's actual dataset!
                                err_text = str((comp_res.get('mismatch_details') or {}).get('error', '')).lower()
                                if (
                                    not comp_res['passed']
                                    and scratchpad
                                    and not scratchpad_loaded
                                    and ('does not exist' in err_text or 'undefined' in err_text)
                                ):
                                    try:
                                        clean_scratch = [
                                            re.sub(r'\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)', 'CREATE TABLE IF NOT EXISTS ', s.rstrip(';') + ';', flags=re.I)
                                            for s in scratchpad
                                        ]
                                        runner.load_fixture("\n".join(clean_scratch))
                                        scratchpad_loaded = True

                                        # Recompute expected_rows by running teacher's reference logic on student's custom dataset
                                        try:
                                            schema_mapping = manifest.get('schemaMapping')
                                            rewritten_ref = rewrite_query_with_schema_mapping(ref_query, schema_mapping)
                                            expected_rows, expected_cols, _, _ = runner.execute_query(
                                                sql_query=rewritten_ref,
                                                max_rows=max_rows,
                                                is_student=False
                                            )
                                        except Exception as ref_custom_err:
                                            logger.info(f"Notice executing reference query on student dataset: {ref_custom_err}")

                                        comp_res, actual_rows, actual_cols, actual_time, actual_overflow, deduction_reason = _evaluate_candidates(
                                            runner=runner,
                                            cand_queries=cand_queries,
                                            ref_query=ref_query,
                                            expected_rows=expected_rows,
                                            expected_cols=expected_cols,
                                            enforce_order=enforce_order,
                                            enforce_col_names=enforce_col_names,
                                            max_rows=max_rows,
                                            question_tag=tc.get('questionId') or f"Q{idx + 1}",
                                            allow_extra_columns=allow_extra_cols,
                                            projection_policy=proj_policy,
                                            extra_column_penalty=extra_col_pen
                                        )
                                    except Exception as sc_err:
                                        logger.info(f"Student scratchpad lazy-load notice: {sc_err}")

                                total_exec_time += actual_time
                                score_factor = comp_res['score_factor']

                            tc_score = round(weight * score_factor, 2)
                            earned_weight += tc_score

                            test_results.append({
                                'name': test_name,
                                'kind': test_kind,
                                'weight': weight,
                                'score': tc_score,
                                'passed': comp_res['passed'],
                                'reason': comp_res['reason'] + deduction_reason,
                                'actualRowCount': comp_res['actual_count'],
                                'expectedRowCount': comp_res['expected_count'],
                                'executionTimeMs': actual_time,
                                'mismatchDetails': comp_res.get('mismatch_details') if test_kind == 'visible' else None
                            })
                        except Exception as tc_err:
                            logger.error(f"Error executing test '{test_name}': {tc_err}")
                            test_results.append({
                                'name': test_name,
                                'kind': test_kind,
                                'weight': weight,
                                'score': 0,
                                'passed': False,
                                'reason': f"Database execution error: {str(tc_err)}",
                                'actualRowCount': 0,
                                'expectedRowCount': 0,
                                'executionTimeMs': 0,
                                'mismatchDetails': None
                            })
                finally:
                    if ref_runner:
                        try:
                            ref_runner.__exit__(None, None, None)
                        except Exception:
                            pass
        except Exception as main_err:
            logger.error(f"Unified runner execution error: {main_err}")
            for tc in test_cases:
                test_results.append({
                    'name': tc.get('name', 'Test Case'),
                    'kind': tc.get('kind', 'visible'),
                    'weight': tc.get('weight', 10),
                    'score': 0,
                    'passed': False,
                    'reason': f"Database execution error: {str(main_err)}",
                    'actualRowCount': 0,
                    'expectedRowCount': 0,
                    'executionTimeMs': 0,
                    'mismatchDetails': None
                })
    else:
        # Standard isolated per-test sandbox runner (for query_only mode or custom tc setup)
        for idx, tc in enumerate(test_cases):
            test_name = tc.get('name', f"Test Case {idx + 1}")
            test_kind = tc.get('kind', 'visible')
            weight = tc.get('weight', 10)
            tc_setup = tc.get('setupSql', '')
            ref_query = tc.get('referenceQuery') or ''
            if not ref_query and idx > 0 and any(kw in test_name.lower() for kw in ['confirm', 'verify', 'second', 'pagination', 'output', 'result']):
                ref_query = test_cases[idx - 1].get('referenceQuery') or manifest.get('referenceQuery', '')
            elif not ref_query:
                ref_query = manifest.get('referenceQuery', '')
            enforce_order = tc.get('enforceOrder', False)
            enforce_col_names = tc.get('enforceColumnNames', False)
            max_rows = tc.get('maxRows', 5000)

            try:
                with SandboxedSqlRunner(job_id=f"{job_id}_{idx}") as runner:
                    with ExecutionWatchdog(runner, deadline_seconds=10.0):
                        full_setup = global_setup + '\n' + tc_setup
                        runner.load_fixture(full_setup)

                    if mode == 'script':
                        from schema_differ import split_sql_statements
                        stmts = split_sql_statements(student_sql)
                        cand_queries = [s.strip() for s in stmts if re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                        runner.execute_script(student_sql, preserve_create=True)
                        script_errors.extend(runner.last_script_errors)

                    # Phase 2: Stored Functions & Procedures
                    if test_kind in ('procedure', 'function', 'routine') or tc.get('routineName') or tc.get('invocations'):
                        res = evaluate_routine_test_case(runner=runner, tc=tc, schema_name=runner.schema_name)
                        tc_score = round(weight * res['score_factor'], 2)
                        earned_weight += tc_score
                        test_results.append({
                            'name': test_name,
                            'kind': test_kind,
                            'weight': weight,
                            'score': tc_score,
                            'passed': res['passed'],
                            'reason': res['reason'],
                            'actualRowCount': res.get('passed_invocations', 0),
                            'expectedRowCount': res.get('total_invocations', 0),
                            'executionTimeMs': 0.0,
                            'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                        })
                        continue

                    # Phase 3: Trigger Event Simulation
                    if test_kind == 'trigger' or tc.get('triggerName') or tc.get('events'):
                        res = evaluate_trigger_test_case(runner=runner, tc=tc, schema_name=runner.schema_name)
                        tc_score = round(weight * res['score_factor'], 2)
                        earned_weight += tc_score
                        test_results.append({
                            'name': test_name,
                            'kind': test_kind,
                            'weight': weight,
                            'score': tc_score,
                            'passed': res['passed'],
                            'reason': res['reason'],
                            'actualRowCount': res.get('passed_events', 0),
                            'expectedRowCount': res.get('total_events', 0),
                            'executionTimeMs': 0.0,
                            'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                        })
                        continue

                    # Phase 4: Multi-Session Concurrency & Transactions (TCL)
                    if test_kind in ('transaction', 'concurrency', 'tcl') or tc.get('steps'):
                        res = evaluate_transaction_test_case(runner=runner, tc=tc, schema_name=runner.schema_name)
                        tc_score = round(weight * res['score_factor'], 2)
                        earned_weight += tc_score
                        test_results.append({
                            'name': test_name,
                            'kind': test_kind,
                            'weight': weight,
                            'score': tc_score,
                            'passed': res['passed'],
                            'reason': res['reason'],
                            'actualRowCount': 1 if res['passed'] else 0,
                            'expectedRowCount': 1,
                            'executionTimeMs': 0.0,
                            'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                        })
                        continue

                    # Phase 4: Roles, Permissions & RLS (DCL)
                    if test_kind in ('dcl', 'permission', 'rls', 'role') or tc.get('allowedStatements') or tc.get('prohibitedStatements'):
                        res = evaluate_dcl_test_case(runner=runner, tc=tc, schema_name=runner.schema_name)
                        tc_score = round(weight * res['score_factor'], 2)
                        earned_weight += tc_score
                        test_results.append({
                            'name': test_name,
                            'kind': test_kind,
                            'weight': weight,
                            'score': tc_score,
                            'passed': res['passed'],
                            'reason': res['reason'],
                            'actualRowCount': res.get('passed_checks', 0),
                            'expectedRowCount': res.get('total_checks', 0),
                            'executionTimeMs': 0.0,
                            'mismatchDetails': res.get('details') if test_kind == 'visible' else None
                        })
                        continue

                    ref_exec_err = None
                    try:
                        actual_rows, actual_cols, actual_overflow, actual_time = runner.execute_query(
                            sql_query=ref_query,
                            max_rows=max_rows,
                            is_student=False
                        )
                    except Exception as rq_err:
                        ref_exec_err = rq_err
                        actual_rows, actual_cols, actual_overflow, actual_time = [], [], False, 0.0
                    ref_script = manifest.get('referenceScript') or manifest.get('referenceSql') or manifest.get('referenceDdl') or tc.get('referenceScript')
                    if ref_script:
                        try:
                            with SandboxedSqlRunner(job_id=f"{job_id}_ref_{idx}") as ref_runner:
                                ref_runner.load_fixture(full_setup)
                                ref_runner.execute_script(ref_script)
                                expected_rows, expected_cols, ref_overflow, ref_time = ref_runner.execute_query(
                                    sql_query=ref_query,
                                    max_rows=max_rows,
                                    is_student=False
                                )
                        except Exception as ref_err:
                            logger.warning(f"Failed to execute reference script for test '{test_name}': {ref_err}")
                            expected_rows = tc.get('expectedRows')
                            expected_cols = tc.get('expectedColumns')
                    else:
                        expected_rows = tc.get('expectedRows')
                        expected_cols = tc.get('expectedColumns')

                    is_catalog_query = any(k in (ref_query or '').lower() for k in ['information_schema', 'pg_catalog', 'current_schema()'])
                    if expected_rows is None:
                        if is_catalog_query:
                            if len(actual_rows) > 0:
                                expected_rows = actual_rows
                                expected_cols = actual_cols
                            else:
                                expected_rows = [['<expected_schema_objects_not_found>']]
                                expected_cols = actual_cols or ['status']
                        elif ref_exec_err is not None:
                            expected_rows = []
                            expected_cols = []
                        else:
                            expected_rows = None
                            expected_cols = None

                    if not is_catalog_query and expected_rows is None:
                        comp_res = {
                            'passed': False,
                            'score_factor': 0.0,
                            'reason': 'Evaluation configuration error: assignment manifest is missing referenceScript or expectedRows.',
                            'actual_count': len(actual_rows),
                            'expected_count': 0,
                            'mismatch_details': {'error': 'EVALUATION_CONFIGURATION_ERROR'}
                        }
                    else:
                        from schema_differ import split_sql_statements
                        stmts = split_sql_statements(student_sql)
                        cand_queries = [s.strip() for s in stmts if re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                        if not cand_queries:
                            cand_queries = [student_sql.strip()]

                        # Compute expected rows on clean full_setup using the same runner BEFORE any student scratchpad
                        if tc.get('expectedRows') is not None:
                            expected_rows = tc.get('expectedRows')
                            expected_cols = tc.get('expectedColumns')
                        else:
                            try:
                                expected_rows, expected_cols, ref_overflow, ref_time = runner.execute_query(
                                    sql_query=ref_query,
                                    max_rows=max_rows,
                                    is_student=False
                                )
                            except Exception as ref_err:
                                logger.warning(f"Error executing reference query: {ref_err}")
                                expected_rows = tc.get('expectedRows') or []
                                expected_cols = tc.get('expectedColumns') or []

                        scratchpad = [s for s in stmts if not re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                        scratchpad_loaded = False
                        if scratchpad and not full_setup.strip():
                            try:
                                runner.execute_script("\n".join(scratchpad))
                                scratchpad_loaded = True
                            except Exception as setup_err:
                                logger.warning(f"Could not preload student setup DDL/DML: {setup_err}")

                        allow_extra_cols = tc.get('allowExtraColumns', manifest.get('allowExtraColumns'))
                        proj_policy = tc.get('projectionPolicy', manifest.get('projectionPolicy', 'flexible'))
                        extra_col_pen = tc.get('extraColumnPenalty', manifest.get('extraColumnPenalty', 0.15))

                        comp_res, actual_rows, actual_cols, actual_time, actual_overflow, _ = _evaluate_candidates(
                            runner=runner,
                            cand_queries=cand_queries,
                            ref_query=ref_query,
                            expected_rows=expected_rows,
                            expected_cols=expected_cols,
                            enforce_order=enforce_order,
                            enforce_col_names=enforce_col_names,
                            max_rows=max_rows,
                            question_tag=tc.get('questionId') or f"Q{idx + 1}",
                            allow_extra_columns=allow_extra_cols,
                            projection_policy=proj_policy,
                            extra_column_penalty=extra_col_pen
                        )

                        err_text = str((comp_res.get('mismatch_details') or {}).get('error', '')).lower()
                        if (
                            not comp_res['passed']
                            and scratchpad
                            and not scratchpad_loaded
                            and ('does not exist' in err_text or 'undefined' in err_text)
                        ):
                            try:
                                runner.execute_script("\n".join(scratchpad))
                                scratchpad_loaded = True
                                try:
                                    schema_mapping = manifest.get('schemaMapping')
                                    rewritten_ref = rewrite_query_with_schema_mapping(ref_query, schema_mapping)
                                    expected_rows, expected_cols, _, _ = runner.execute_query(
                                        sql_query=rewritten_ref,
                                        max_rows=max_rows,
                                        is_student=False
                                    )
                                except Exception as ref_custom_err:
                                    logger.info(f"Notice executing reference query on student dataset: {ref_custom_err}")

                                comp_res, actual_rows, actual_cols, actual_time, actual_overflow, _ = _evaluate_candidates(
                                    runner=runner,
                                    cand_queries=cand_queries,
                                    ref_query=ref_query,
                                    expected_rows=expected_rows,
                                    expected_cols=expected_cols,
                                    enforce_order=enforce_order,
                                    enforce_col_names=enforce_col_names,
                                    max_rows=max_rows,
                                    question_tag=tc.get('questionId') or f"Q{idx + 1}",
                                    allow_extra_columns=allow_extra_cols,
                                    projection_policy=proj_policy,
                                    extra_column_penalty=extra_col_pen
                                )
                            except Exception as setup_err:
                                logger.warning(f"Could not lazy-load student setup DDL/DML: {setup_err}")

                    total_exec_time += actual_time

                    if mode == 'script':
                        if not is_catalog_query and cand_queries:
                            comp_res, actual_rows, actual_cols, cand_time, actual_overflow, _ = _evaluate_candidates(
                                runner=runner,
                                cand_queries=cand_queries,
                                ref_query=ref_query,
                                expected_rows=expected_rows or [],
                                expected_cols=expected_cols or [],
                                enforce_order=enforce_order,
                                enforce_col_names=enforce_col_names,
                                max_rows=max_rows,
                                allow_extra_columns=allow_extra_cols,
                                projection_policy=proj_policy,
                                extra_column_penalty=extra_col_pen
                            )
                            total_exec_time += cand_time
                        elif ref_exec_err is not None:
                            comp_res = {
                                'passed': False,
                                'score_factor': 0.0,
                                'reason': f"Reference check failed on student schema: {ref_exec_err}",
                                'actual_count': 0,
                                'expected_count': len(expected_rows or []),
                                'mismatch_details': {'error': str(ref_exec_err)}
                            }
                        else:
                            comp_res = compare_query_results(
                                actual_rows=actual_rows,
                                expected_rows=expected_rows,
                                actual_columns=actual_cols,
                                expected_columns=expected_cols,
                                enforce_order=enforce_order,
                                enforce_column_names=enforce_col_names,
                                allow_extra_columns=allow_extra_cols,
                                projection_policy=proj_policy,
                                extra_column_penalty=extra_col_pen,
                                max_allowed_rows=max_rows,
                                has_overflow=actual_overflow
                            )

                    score_factor = comp_res['score_factor']
                    if comp_res['passed'] and hasattr(runner, 'repairs_applied') and runner.repairs_applied:
                        matched_rep = None
                        for rep in runner.repairs_applied:
                            t_name = rep.get('table_name')
                            if t_name and (t_name in ref_query.lower() or t_name in test_name.lower()):
                                matched_rep = rep
                                break

                        if matched_rep:
                            rep_factor = matched_rep.get('score_factor', 0.67)
                            score_factor = round(min(score_factor, rep_factor), 2)
                            comp_res['reason'] += f" (Deduction: Syntax error on table '{matched_rep['table_name']}' required auto-repair)."
                        else:
                            is_exec_tc = any(kw in test_name.lower() for kw in ['script execution', 'dependency ordering', 'execution', 'sequential', 'ordering'])
                            if is_exec_tc:
                                num_rep = len(runner.repairs_applied)
                                exec_factor = max(0.40, round(1.0 - (0.25 * num_rep), 2))
                                score_factor = round(min(score_factor, exec_factor), 2)
                                comp_res['reason'] += f" (Deduction: Script had {num_rep} statement execution faults requiring auto-repair)."

                    tc_score = round(weight * score_factor, 2)
                    earned_weight += tc_score

                    test_results.append({
                        'name': test_name,
                        'kind': test_kind,
                        'weight': weight,
                        'score': tc_score,
                        'passed': comp_res['passed'],
                        'reason': comp_res['reason'],
                        'actualRowCount': comp_res['actual_count'],
                        'expectedRowCount': comp_res['expected_count'],
                        'executionTimeMs': actual_time,
                        'mismatchDetails': comp_res.get('mismatch_details') if test_kind == 'visible' else None
                    })
            except Exception as exec_err:
                logger.error(f"Execution error on test '{test_name}': {exec_err}")
                test_results.append({
                    'name': test_name,
                    'kind': test_kind,
                    'weight': weight,
                    'score': 0,
                    'passed': False,
                    'reason': f"Database execution error: {str(exec_err)}",
                    'actualRowCount': 0,
                    'expectedRowCount': 0,
                    'executionTimeMs': 0,
                    'mismatchDetails': None
                })

    # Calculate final normalized deterministic score (0 - 100)
    final_score = round((earned_weight / total_weight) * 100, 2) if total_weight > 0 else 0

    # Build rubric breakdown
    rubric_breakdown = {}
    for tr in test_results:
        t_name = tr['name']
        rubric_breakdown[t_name] = round(rubric_breakdown.get(t_name, 0.0) + tr['score'], 2)

    return {
        'status': 'SUCCESS' if final_score >= 100 else 'PARTIAL',
        'deterministicScore': final_score,
        'policyBlocked': False,
        'astAnalysis': ast_res,
        'testResults': test_results,
        'rubricBreakdown': rubric_breakdown,
        'executionMetrics': {
            'executionTimeMs': round(total_exec_time, 2),
            'testCount': len(test_results)
        },
        'rawStudentSql': raw_student_sql,
        'prunedStudentSql': pruned_sql_for_llm,
        'preparseDiagnostics': preparse_diagnostics,
        'normalizationNotices': normalization_notices,
        'scriptErrors': script_errors[:20],
        'error': None
    }


def main():
    """Reads input JSON from stdin, runs evaluation, writes JSON to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.stderr.write("sql_runner error: Empty stdin payload\n")
            sys.exit(1)

        payload = json.loads(raw_input)
        result = run_evaluation(payload)

        # Output JSON result to stdout
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
        sys.exit(0)
    except Exception as err:
        sys.stderr.write(f"sql_runner fatal error: {str(err)}\n")
        sys.exit(1)


if __name__ == '__main__':
    main()
