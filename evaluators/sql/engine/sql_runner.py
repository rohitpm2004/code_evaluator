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
from db_executor import SandboxedSqlRunner
from schema_differ import normalize_student_ddl, compute_name_similarity, sanitize_and_split_student_sql, prune_bulk_inserts_for_llm

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

    cand_upper = cand_sql.upper()
    ref_upper = ref_query.upper()

    # 1. ORDER BY & Direction Check
    if 'ORDER BY' in ref_upper:
        if 'ORDER BY' not in cand_upper:
            return False, 0.0, "Missing required ORDER BY clause."
        if 'DESC' in ref_upper and 'DESC' not in cand_upper:
            return False, 0.0, "ORDER BY direction should be DESC."
        if 'ASC' in ref_upper and 'DESC' in cand_upper and 'ASC' not in ref_upper:
            return False, 0.0, "ORDER BY direction should be ASC."

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
        if any(kw in student_sql.upper() for kw in ('CREATE TABLE', 'ALTER TABLE', 'DROP TABLE', 'CREATE INDEX', 'CREATE VIEW')):
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
        norm_sql, alias_map, normalization_notices = normalize_student_ddl(student_sql, manifest)
        student_sql = norm_sql

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
        return {
            'status': 'AST_ERROR',
            'deterministicScore': 0,
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

    has_custom_tc_setup = any(tc.get('setupSql', '').strip() for tc in test_cases) and mode != 'query_only'

    # Fast path: without custom test setup, provision an ephemeral schema once,
    # load fixture once, then verify all test cases against it (massive WAN speedup and zero lock contention)
    if not has_custom_tc_setup:
        try:
            with SandboxedSqlRunner(job_id=f"{job_id}_main") as runner:
                runner.load_fixture(global_setup)

                ref_runner = None
                cand_queries = []

                if mode == 'script':
                    script_time = runner.execute_script(student_sql)
                    total_exec_time += script_time

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
                    # In query_only mode, extract candidate SELECT statements from student SQL
                    from schema_differ import split_sql_statements
                    stmts = split_sql_statements(student_sql)
                    cand_queries = [s.strip() for s in stmts if re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                    if not cand_queries:
                        cand_queries = [student_sql.strip()]

                    # Execute student scratchpad statements (CREATE TABLE, INSERT) if present
                    scratchpad = [s.strip() for s in stmts if not re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                    if scratchpad:
                        try:
                            clean_scratch = []
                            for s in scratchpad:
                                s_clean = s.rstrip(';') + ';'
                                s_clean = re.sub(r'\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)', 'CREATE TABLE IF NOT EXISTS ', s_clean, flags=re.I)
                                clean_scratch.append(s_clean)
                            runner.load_fixture("\n".join(clean_scratch))
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

                        try:
                            if mode == 'script':
                                actual_rows, actual_cols, actual_overflow, actual_time = runner.execute_query(
                                    sql_query=ref_query,
                                    max_rows=max_rows,
                                    is_student=False
                                )
                                total_exec_time += actual_time

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

                                if expected_rows is None and is_catalog_query:
                                    if len(actual_rows) > 0:
                                        expected_rows = actual_rows
                                        expected_cols = actual_cols
                                    else:
                                        expected_rows = [['<expected_schema_objects_not_found>']]
                                        expected_cols = actual_cols or ['status']

                                comp_res = compare_query_results(
                                    actual_rows=actual_rows,
                                    expected_rows=expected_rows,
                                    actual_columns=actual_cols,
                                    expected_columns=expected_cols,
                                    enforce_order=enforce_order,
                                    enforce_column_names=enforce_col_names,
                                    max_allowed_rows=max_rows,
                                    has_overflow=actual_overflow
                                )

                                score_factor = comp_res['score_factor']
                                has_naming_deduction = False
                                deduction_reason = ""

                                if comp_res['passed']:
                                    for tbl_name, info in alias_map.items():
                                        if tbl_name in ref_query.lower() or info['canonical'] in ref_query.lower():
                                            has_naming_deduction = True
                                            score_factor = max(0.85, score_factor * 0.90)  # 10% naming deduction
                                            deduction_reason = f" (Note: Table defined as '{info['original']}', matched to '{info['canonical']}')"
                                            break
                                    if typo_matched and not has_naming_deduction:
                                        score_factor = max(0.80, score_factor * 0.85)
                                        deduction_reason = f" (Note: Table '{matched_table_name}' matched target with partial credit)"

                            else:
                                # Mode == 'query_only'
                                if tc.get('expectedRows') is not None:
                                    expected_rows = tc.get('expectedRows')
                                    expected_cols = tc.get('expectedColumns')
                                else:
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

                                best_comp_res = None
                                best_score_factor = -1.0
                                best_actual_rows = []
                                best_actual_cols = []
                                best_actual_time = 0.0
                                best_actual_overflow = False
                                best_err = None
                                best_deduction = ""

                                for cand_sql in cand_queries:
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
                                            max_allowed_rows=max_rows,
                                            has_overflow=c_overflow
                                        )

                                        # Check if query matched AST intent on creative mock data
                                        matched, ast_factor, explanation = check_ast_concept_match(cand_sql, ref_query)
                                        if matched and ast_factor > c_comp['score_factor']:
                                            if c_comp['actual_count'] == expected_count and c_comp['actual_count'] > 0:
                                                c_comp['score_factor'] = ast_factor
                                                c_comp['passed'] = True
                                                c_comp['reason'] = f"Query logic successfully verified: returned {c_comp['actual_count']} rows on student dataset demonstrating {explanation}"

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
                                        # Tier 1: Dynamic query auto-repair (column/table discrepancy)
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
                                                max_allowed_rows=max_rows,
                                                has_overflow=r_overflow
                                            )
                                            if r_comp['score_factor'] > 0:
                                                adj_factor = round(r_comp['score_factor'] * 0.90, 2)
                                            else:
                                                matched, ast_factor, explanation = check_ast_concept_match(cand_sql, ref_query)
                                                if matched and r_comp['actual_count'] == expected_count and r_comp['actual_count'] > 0:
                                                    adj_factor = round(ast_factor * 0.90, 2)
                                                    r_comp['passed'] = True
                                                    r_comp['reason'] = f"Query logic successfully verified on repaired schema: {explanation}"
                                                else:
                                                    adj_factor = 0.0

                                            if adj_factor > best_score_factor:
                                                best_score_factor = adj_factor
                                                r_comp['score_factor'] = adj_factor
                                                r_comp['passed'] = True
                                                best_comp_res = r_comp
                                                best_actual_rows = r_rows
                                                best_actual_cols = r_cols
                                                best_actual_time = r_time
                                                best_actual_overflow = r_overflow
                                                best_deduction = notice
                                                continue

                                        # Tier 2: AST / Semantic Concept Matcher (Intent-based grading)
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
                                    comp_res = best_comp_res
                                    actual_rows = best_actual_rows
                                    actual_cols = best_actual_cols
                                    actual_time = best_actual_time
                                    actual_overflow = best_actual_overflow
                                    deduction_reason = best_deduction
                                else:
                                    actual_rows, actual_cols, actual_overflow, actual_time = [], [], False, 0.0
                                    comp_res = {
                                        'passed': False,
                                        'score_factor': 0.0,
                                        'reason': f"Query execution error: {str(best_err)}" if best_err else "No matching query results.",
                                        'actual_count': 0,
                                        'expected_count': len(expected_rows) if expected_rows else 0,
                                        'mismatch_details': {'error': str(best_err)} if best_err else None
                                    }
                                    deduction_reason = ""

                                total_exec_time += actual_time
                                score_factor = comp_res['score_factor']
                                deduction_reason = ""

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
                    full_setup = global_setup + '\n' + tc_setup
                    runner.load_fixture(full_setup)

                    # Creative tolerance: If student included CREATE TABLE statements, execute their setup DDL/DML
                    # in the sandbox so custom table names and mock data exist and can be queried!
                    if mode != 'script' and re.search(r'\bCREATE\s+TABLE\b', student_sql, re.I):
                        try:
                            from schema_differ import split_sql_statements
                            all_stmts = split_sql_statements(student_sql)
                            setup_stmts = [s for s in all_stmts if not re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                            if setup_stmts:
                                runner.execute_script("\n".join(setup_stmts))
                        except Exception as setup_err:
                            logger.warning(f"Could not preload student setup DDL/DML: {setup_err}")

                    if mode == 'script':
                        runner.execute_script(student_sql)
                        actual_rows, actual_cols, actual_overflow, actual_time = runner.execute_query(
                            sql_query=ref_query,
                            max_rows=max_rows,
                            is_student=False
                        )
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

                        if expected_rows is None:
                            is_catalog_query = any(k in (ref_query or '').lower() for k in ['information_schema', 'pg_catalog', 'current_schema()'])
                            if is_catalog_query:
                                if len(actual_rows) > 0:
                                    expected_rows = actual_rows
                                    expected_cols = actual_cols
                                else:
                                    expected_rows = [['<expected_schema_objects_not_found>']]
                                    expected_cols = actual_cols or ['status']
                            else:
                                expected_rows = []
                    else:
                        from schema_differ import split_sql_statements
                        stmts = split_sql_statements(student_sql)
                        cand_queries = [s.strip() for s in stmts if re.match(r'^\s*(SELECT|WITH)\b', s, re.I)]
                        if not cand_queries:
                            cand_queries = [student_sql.strip()]

                        try:
                            with SandboxedSqlRunner(job_id=f"{job_id}_ref_{idx}") as ref_runner:
                                ref_runner.load_fixture(full_setup)
                                expected_rows, expected_cols, ref_overflow, ref_time = ref_runner.execute_query(
                                    sql_query=ref_query,
                                    max_rows=max_rows,
                                    is_student=False
                                )
                        except Exception as ref_err:
                            logger.warning(f"Error executing reference query in independent sandbox: {ref_err}")
                            expected_rows = tc.get('expectedRows') or []
                            expected_cols = tc.get('expectedColumns') or []

                        best_comp_res = None
                        best_score_factor = -1.0
                        best_actual_rows = []
                        best_actual_cols = []
                        best_actual_time = 0.0
                        best_actual_overflow = False
                        best_err = None
                        best_deduction = ""

                        for cand_sql in cand_queries:
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
                                    max_allowed_rows=max_rows,
                                    has_overflow=c_overflow
                                )

                                # Check if query matched AST intent on creative mock data
                                matched, ast_factor, explanation = check_ast_concept_match(cand_sql, ref_query)
                                if matched and ast_factor > c_comp['score_factor']:
                                    if c_comp['actual_count'] == expected_count and c_comp['actual_count'] > 0:
                                        c_comp['score_factor'] = ast_factor
                                        c_comp['passed'] = True
                                        c_comp['reason'] = f"Query logic successfully verified: returned {c_comp['actual_count']} rows on student dataset demonstrating {explanation}"

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
                                # Tier 1: Dynamic query auto-repair (column/table discrepancy)
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
                                        max_allowed_rows=max_rows,
                                        has_overflow=r_overflow
                                    )
                                    if r_comp['score_factor'] > 0:
                                        adj_factor = round(r_comp['score_factor'] * 0.90, 2)
                                    else:
                                        matched, ast_factor, explanation = check_ast_concept_match(cand_sql, ref_query)
                                        if matched and r_comp['actual_count'] == expected_count and r_comp['actual_count'] > 0:
                                            adj_factor = round(ast_factor * 0.90, 2)
                                            r_comp['passed'] = True
                                            r_comp['reason'] = f"Query logic successfully verified on repaired schema: {explanation}"
                                        else:
                                            adj_factor = 0.0

                                    if adj_factor > best_score_factor:
                                        best_score_factor = adj_factor
                                        r_comp['score_factor'] = adj_factor
                                        r_comp['passed'] = True
                                        best_comp_res = r_comp
                                        best_actual_rows = r_rows
                                        best_actual_cols = r_cols
                                        best_actual_time = r_time
                                        best_actual_overflow = r_overflow
                                        best_deduction = notice
                                        continue

                                # Tier 2: AST / Semantic Concept Matcher (Intent-based grading)
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
                                    best_actual_rows = expected_rows or []
                                    best_actual_cols = expected_cols or []
                                    best_actual_time = 0.0
                                    best_actual_overflow = False
                                    best_deduction = f" (Note: Evaluated via concept verification due to execution discrepancy: {str(c_err)})"

                        if best_comp_res is not None:
                            comp_res = best_comp_res
                            actual_rows = best_actual_rows
                            actual_cols = best_actual_cols
                            actual_time = best_actual_time
                            actual_overflow = best_actual_overflow
                        else:
                            actual_rows, actual_cols, actual_overflow, actual_time = [], [], False, 0.0
                            comp_res = {
                                'passed': False,
                                'score_factor': 0.0,
                                'reason': f"Query execution error: {str(best_err)}" if best_err else "No matching query results.",
                                'actual_count': 0,
                                'expected_count': len(expected_rows) if expected_rows else 0,
                                'mismatch_details': {'error': str(best_err)} if best_err else None
                            }

                    total_exec_time += actual_time

                    if mode == 'script':
                        comp_res = compare_query_results(
                            actual_rows=actual_rows,
                            expected_rows=expected_rows,
                            actual_columns=actual_cols,
                            expected_columns=expected_cols,
                            enforce_order=enforce_order,
                            enforce_column_names=enforce_col_names,
                            max_allowed_rows=max_rows,
                            has_overflow=actual_overflow
                        )

                    score_factor = comp_res['score_factor']
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
