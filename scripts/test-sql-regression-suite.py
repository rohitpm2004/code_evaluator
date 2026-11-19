#!/usr/bin/env python3
"""
test-sql-regression-suite.py - SQL 2.0 Ground-Truth Regression & Enterprise Engine Suite.

Comprehensive regression harness verifying:
1. Negative Tests (Security & False-Positive Prevention):
   - AST guard blocks unauthorized DDL/DML, privilege escalation, and injection attempts.
   - AST guard comment immunity (comments/strings containing 'copy' or 'version' pass).
   - Real student golden negative fixtures (ast syntax error, injection attempt, missing clauses).
2. Positive Tests (Pedagogical Flexibility & Tolerances):
   - Multiset row equivalence (unordered rows match 100%).
   - Ordered strict matching (out-of-order fails when enforceOrder=True).
   - Numeric & Float tolerances (Decimal, float, int normalized with 1e-4 tolerance).
   - Flexible column alias matching and column reordering.
   - Projection tolerance (SELECT * extra columns awarded calibrated partial credit ~85%).
   - Proportional partial credit for row subsets.
3. Enterprise Runners:
   - Trigger Runner (trigger_runner.py): mutation simulation, side-effect verifyQuery, exception aborts.
   - Routine Runner (routine_runner.py): function/procedure calls, literal formatting, exception handling.
   - Concurrency & DCL Runner (concurrency_runner.py): role permissions, catalog privileges, isolation steps.
4. Schema Differ & Creative Dataset Engine:
   - Terminal prompt and student note stripping.
   - Stray trailing character recovery.
   - Creative mock dataset (Mode B) AST schema remapping.
"""

import sys
import os
import json
from decimal import Decimal
from typing import Dict, Any, List, Tuple

# Add evaluators/sql/engine to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
engine_dir = os.path.abspath(os.path.join(current_dir, '..', 'evaluators', 'sql', 'engine'))
test_dir = os.path.abspath(os.path.join(current_dir, '..', 'test'))

if engine_dir not in sys.path:
    sys.path.insert(0, engine_dir)

from ast_guard import validate_sql_ast
from comparator import compare_query_results, normalize_value, normalize_row
from sql_runner import check_ast_concept_match
from schema_differ import (
    sanitize_and_split_student_sql,
    prune_bulk_inserts_for_llm,
    rewrite_query_with_schema_mapping,
)
from trigger_runner import evaluate_trigger_test_case
from routine_runner import evaluate_routine_test_case, _format_sql_literal
from concurrency_runner import evaluate_dcl_test_case, evaluate_transaction_test_case

# Track test statistics
total_tests = 0
passed_tests = 0
failed_tests = 0
failed_test_names = []


def record_result(name: str, passed: bool, details: str = ""):
    global total_tests, passed_tests, failed_tests, failed_test_names
    total_tests += 1
    if passed:
        passed_tests += 1
        print(f"  [PASS] {name}" + (f" -> {details}" if details else ""))
    else:
        failed_tests += 1
        failed_test_names.append(name)
        print(f"  [FAIL] {name}" + (f" -> {details}" if details else ""))


class MockRunner:
    """Deterministic Mock Database Runner for Enterprise Trigger, Routine, and DCL tests."""
    def __init__(self):
        self.executed_scripts: List[str] = []
        self.query_responses: Dict[str, Tuple[List[Tuple], List[str]]] = {}
        self.catalog_data: Dict[str, List[Tuple]] = {}
        self.should_raise_on_script: Dict[str, str] = {}
        self.should_raise_on_query: Dict[str, str] = {}

    def execute_script(self, stmt: str):
        self.executed_scripts.append(stmt.strip())
        for trigger_snippet, err_msg in self.should_raise_on_script.items():
            if trigger_snippet in stmt:
                raise RuntimeError(err_msg)

    def execute_query(self, query: str, max_rows: int = 100, is_student: bool = False):
        q_clean = query.strip()
        for trigger_snippet, err_msg in self.should_raise_on_query.items():
            if trigger_snippet in q_clean:
                raise RuntimeError(err_msg)

        for pattern, (rows, cols) in self.query_responses.items():
            if pattern.lower() in q_clean.lower():
                return rows[:max_rows], cols, len(rows), False

        return [], [], 0, False


# ══════════════════════════════════════════════════════════════════════════
# Suite 1: Negative Security & Anti-False-Positive Tests
# ══════════════════════════════════════════════════════════════════════════
def run_negative_security_tests():
    print("\n--- Suite 1: Negative Security & Anti-False-Positive Guard Tests ---")

    # 1.1 AST Guard blocks administrative commands (pg_read_file)
    sql_bad = "SELECT * FROM departments; SELECT pg_read_file('/etc/passwd');"
    res = validate_sql_ast(sql_bad, mode='query_only')
    record_result(
        "AST Guard blocks pg_read_file attempt",
        not res['valid_syntax'] or len(res['forbidden_detected']) > 0,
        f"Forbidden: {res['forbidden_detected']}"
    )

    # 1.2 AST Guard blocks table dropping in query mode
    sql_drop = "DROP TABLE students CASCADE;"
    res_drop = validate_sql_ast(sql_drop, mode='query_only')
    record_result(
        "AST Guard blocks DROP TABLE in query_only mode",
        not res_drop['valid_syntax'] or 'statement' in str(res_drop.get('reason', '')).lower() or len(res_drop['forbidden_detected']) > 0
    )

    # 1.3 Comment Immunity: 'copy' or 'version' in comments/strings must NOT be flagged as forbidden
    sql_comment = """
    -- This is a copy of assignment 1 checking PostgreSQL version 16
    SELECT name, 'final_copy' AS doc_version FROM reports;
    """
    res_comm = validate_sql_ast(sql_comment, mode='query_only')
    record_result(
        "AST Guard comment immunity: comments mentioning 'copy'/'version' pass safely",
        res_comm['valid_syntax'] is True and len(res_comm['forbidden_detected']) == 0,
        f"Forbidden flagged: {res_comm['forbidden_detected']}"
    )

    # 1.4 AST Guard detects missing required clauses
    sql_simple = "SELECT id, name FROM departments WHERE id = 1;"
    manifest_req = {'astRequirements': {'requiredClauses': ['JOIN', 'GROUP BY']}}
    res_missing = validate_sql_ast(sql_simple, mode='query_only', manifest=manifest_req)
    reqs = res_missing['required_clauses_checked']
    record_result(
        "AST Guard detects missing JOIN & GROUP BY clauses",
        reqs.get('JOIN') is False and reqs.get('GROUP BY') is False,
        f"Clauses checked: {reqs}"
    )

    # 1.5 Golden Fixture 02: AST Syntax Error
    f_path_02 = os.path.join(test_dir, 'fixtures', 'sql', 'golden_02_ast_syntax_error.sql')
    if os.path.exists(f_path_02):
        with open(f_path_02, 'r') as f:
            broken_sql = f.read()
        res_02 = validate_sql_ast(broken_sql, mode='query_only')
        record_result(
            "Golden Fixture 02: Syntax error correctly identified and rejected",
            res_02['valid_syntax'] is False
        )

    # 1.6 Golden Fixture 03: Injection Attempt
    f_path_03 = os.path.join(test_dir, 'fixtures', 'sql', 'golden_03_injection_attempt.sql')
    if os.path.exists(f_path_03):
        with open(f_path_03, 'r') as f:
            inject_sql = f.read()
        res_03 = validate_sql_ast(inject_sql, mode='query_only')
        record_result(
            "Golden Fixture 03: Security injection (DROP + pg_read_file) strictly blocked",
            not res_03['valid_syntax'] or len(res_03['forbidden_detected']) > 0,
            f"Detected: {res_03['forbidden_detected']}"
        )


# ══════════════════════════════════════════════════════════════════════════
# Suite 2: Positive Flexibility, Projection & Multiset Tolerances
# ══════════════════════════════════════════════════════════════════════════
def run_positive_tolerance_tests():
    print("\n--- Suite 2: Positive Tolerances & Pedagogical Flexibility Tests ---")

    # 2.1 Unordered Multiset Row Equivalence
    actual_rows = [
        ('Finance', 5, Decimal('85000.50')),
        ('Engineering', 20, Decimal('120000.00')),
    ]
    expected_rows = [
        ('Engineering', 20, 120000.0),
        ('Finance', 5, 85000.5),
    ]
    res_multiset = compare_query_results(actual_rows, expected_rows, enforce_order=False)
    record_result(
        "Multiset comparison: unordered rows match with 100% score",
        res_multiset['passed'] is True and res_multiset['score_factor'] == 1.0,
        f"Score: {res_multiset['score_factor']}"
    )

    # 2.2 Ordered strict comparison fails when order differs
    res_ordered = compare_query_results(actual_rows, expected_rows, enforce_order=True)
    record_result(
        "Ordered comparison: strict row ordering failure when order differs",
        res_ordered['passed'] is False and 'Row order mismatch' in res_ordered['reason']
    )

    # 2.3 Float & Decimal numerical normalization with 1e-4 tolerance
    row_act = [(Decimal('45.12345'),)]
    row_exp = [(45.1234,),]
    res_float = compare_query_results(row_act, row_exp, enforce_order=True)
    record_result(
        "Numerical tolerance: Decimal vs Float within 1e-4 matches",
        res_float['passed'] is True and res_float['score_factor'] == 1.0
    )

    # 2.4 Column Reordering Tolerance
    act_reordered = [(Decimal('100.0'), 'Widget A'), (Decimal('200.0'), 'Widget B')]
    exp_standard = [('Widget A', 100.0), ('Widget B', 200.0)]
    res_reorder = compare_query_results(
        actual_rows=act_reordered,
        expected_rows=exp_standard,
        actual_columns=['cost', 'item_name'],
        expected_columns=['item_name', 'cost'],
        enforce_order=True
    )
    record_result(
        "Column reordering: columns provided in reverse order match properly",
        res_reorder['passed'] is True and res_reorder['score_factor'] >= 0.90,
        f"Score: {res_reorder['score_factor']}"
    )

    # 2.5 Projection Tolerance for SELECT * (extra columns present)
    act_star = [(1, 'Laptop', Decimal('1200.00')), (2, 'Phone', Decimal('800.00'))]
    exp_proj = [('Laptop', 1200.00), ('Phone', 800.00)]
    res_star = compare_query_results(
        actual_rows=act_star,
        expected_rows=exp_proj,
        actual_columns=['product_id', 'product_name', 'price'],
        expected_columns=['product_name', 'price'],
        enforce_order=True
    )
    record_result(
        "Projection tolerance: SELECT * extra column awards calibrated 85% credit",
        res_star['passed'] is True and res_star['score_factor'] == 0.85,
        f"Score: {res_star['score_factor']}"
    )

    # 2.6 Partial Credit for Row Overlap (3 of 5 rows match)
    act_subset = [(1,), (2,), (3,), (99,), (100,)]
    exp_full = [(1,), (2,), (3,), (4,), (5,)]
    res_part = compare_query_results(act_subset, exp_full, enforce_order=False)
    record_result(
        "Row subset partial credit: 3/5 rows awards 0.60 score factor",
        res_part['passed'] is False and res_part['score_factor'] == 0.6,
        f"Score factor: {res_part['score_factor']}"
    )

    # 2.7 Golden Fixture 01: Perfect Query
    f_path_01 = os.path.join(test_dir, 'fixtures', 'sql', 'golden_01_perfect_query.sql')
    if os.path.exists(f_path_01):
        with open(f_path_01, 'r') as f:
            perf_sql = f.read()
        res_01 = validate_sql_ast(perf_sql, mode='query_only', manifest={
            'astRequirements': {'requiredClauses': ['JOIN', 'GROUP BY', 'HAVING', 'ORDER BY']}
        })
        record_result(
            "Golden Fixture 01: Valid complex query passes AST and all required clauses",
            res_01['valid_syntax'] is True and all(res_01['required_clauses_checked'].values()),
            f"Clauses checked: {res_01['required_clauses_checked']}"
        )


# ══════════════════════════════════════════════════════════════════════════
# Suite 3: Enterprise Runners (Triggers, Procedures & DCL)
# ══════════════════════════════════════════════════════════════════════════
def run_enterprise_runners_tests():
    print("\n--- Suite 3: Enterprise Runners (Triggers, Procedures & DCL) Tests ---")

    # 3.1 Trigger Runner: Event Mutation & Side-Effect Assertion
    mock_trigger = MockRunner()
    mock_trigger.query_responses["audit_log"] = (
        [(1, 'INSERT', 'orders', 101)],
        ['id', 'action', 'table_name', 'record_id']
    )
    tc_trigger_success = {
        'tableName': 'orders',
        'events': [
            {
                'description': 'AFTER INSERT audit log verification',
                'statement': "INSERT INTO orders (id, amount) VALUES (101, 250.00);",
                'verifyQuery': "SELECT id, action, table_name, record_id FROM audit_log WHERE record_id = 101;",
                'expectedRows': [(1, 'INSERT', 'orders', 101)],
                'expectedColumns': ['id', 'action', 'table_name', 'record_id']
            }
        ]
    }
    res_trig = evaluate_trigger_test_case(mock_trigger, tc=tc_trigger_success, schema_name="eval_test")
    record_result(
        "Trigger Runner: Row-level AFTER INSERT trigger event and audit log verified",
        res_trig['passed'] is True and res_trig['score_factor'] == 1.0,
        f"Passed events: {res_trig['passed_events']}/{res_trig['total_events']}"
    )

    # 3.2 Trigger Runner: Exception Handling & Rollback (expectException: True)
    mock_trig_exc = MockRunner()
    mock_trig_exc.should_raise_on_script["NEGATIVE_QTY"] = "CHECK violation: Quantity must be positive"
    mock_trig_exc.query_responses["from inventory"] = ([(0,)], ['count'])

    tc_trigger_exception = {
        'tableName': 'inventory',
        'events': [
            {
                'description': 'Trigger aborts on invalid stock mutation',
                'statement': "INSERT INTO inventory (item, qty) VALUES ('NEGATIVE_QTY', -5);",
                'expectException': True,
                'verifyQuery': "SELECT COUNT(*) FROM inventory WHERE item = 'NEGATIVE_QTY';",
                'expectedRows': [(0,)],
                'expectedColumns': ['count']
            }
        ]
    }
    res_trig_exc = evaluate_trigger_test_case(mock_trig_exc, tc=tc_trigger_exception, schema_name="eval_test")
    record_result(
        "Trigger Runner: RAISE EXCEPTION conditional trigger verified with state rollback",
        res_trig_exc['passed'] is True and res_trig_exc['score_factor'] == 1.0,
        f"Details: {res_trig_exc['reason']}"
    )

    # 3.3 Routine Runner: Stored Procedure CALL & Argument Formatting
    mock_routine = MockRunner()
    mock_routine.query_responses["get_customer_balance"] = ([(Decimal('750.00'),)], ['balance'])

    tc_routine = {
        'routineName': 'get_customer_balance',
        'routineType': 'function',
        'invocations': [
            {
                'description': 'Query customer 42 balance',
                'args': [42],
                'expectedReturn': Decimal('750.00')
            }
        ]
    }
    res_routine = evaluate_routine_test_case(mock_routine, tc=tc_routine, schema_name="eval_test")
    record_result(
        "Routine Runner: Stored Function evaluation with arguments and return validation",
        res_routine['passed'] is True and res_routine['score_factor'] == 1.0,
        f"Passed invocations: {res_routine['passed_invocations']}/{res_routine['total_invocations']}"
    )

    # 3.4 Routine Runner: SQL Literal Formatter test across types
    lit_none = _format_sql_literal(None)
    lit_bool = _format_sql_literal(True)
    lit_str = _format_sql_literal("O'Reilly")
    lit_arr = _format_sql_literal([1, "two", 3.5])
    lit_dict = _format_sql_literal({"key": "val"})
    record_result(
        "Routine Runner: _format_sql_literal safely formats NULL, booleans, arrays & JSON",
        lit_none == "NULL" and lit_bool == "TRUE" and lit_str == "'O''Reilly'" and "ARRAY[" in lit_arr and "::jsonb" in lit_dict
    )

    # 3.5 DCL Runner: Role Permissions & Privileges Evaluation
    mock_dcl = MockRunner()
    # Mock allowed query returns rows
    mock_dcl.query_responses["from public_catalog"] = ([('Item A', 10.0)], ['name', 'price'])
    # Mock prohibited query raises permission denied
    mock_dcl.should_raise_on_query["from secret_salaries"] = "permission denied for table secret_salaries"

    tc_dcl = {
        'role': 'app_student_role',
        'allowedQueries': ["SELECT * FROM public_catalog;"],
        'prohibitedQueries': ["SELECT * FROM secret_salaries;"]
    }
    res_dcl = evaluate_dcl_test_case(mock_dcl, tc=tc_dcl, schema_name="eval_test")
    record_result(
        "DCL Runner: Role permission verification (allowed vs prohibited tables)",
        res_dcl['passed'] is True and res_dcl['score_factor'] == 1.0,
        f"Details: {res_dcl['reason']}"
    )


# ══════════════════════════════════════════════════════════════════════════
# Suite 4: Schema Differ, Creative Mock Datasets & AST Concept Matcher
# ══════════════════════════════════════════════════════════════════════════
def run_creative_and_sanitization_tests():
    print("\n--- Suite 4: Schema Differ, Creative Datasets & Sanitization Tests ---")

    # 4.1 Student notes and terminal headers sanitized without losing SQL
    raw_student_sql = """
    Here is my submission for homework 3:
    Step 1: Create departments
    CREATE TABLE departments (id INT PRIMARY KEY, name VARCHAR(50));
    -- Step 2: Insert data
    INSERT INTO departments (id, name) VALUES (1, 'Engineering');
    SELECT * FROM departments ORDER BY id;
    """
    stmts, sanitized, diags = sanitize_and_split_student_sql(raw_student_sql)
    record_result(
        "Sanitizer strips student prose notes into diagnostics while keeping valid SQL intact",
        len(stmts) >= 3 and len(diags) >= 2 and any("departments" in s for s in stmts),
        f"Statements: {len(stmts)}, Diagnostics: {len(diags)}"
    )

    # 4.2 Stray trailing character recovery
    stray_sql = "SELECT * FROM inventory ORDER BY id DESC LIMIT 5;q"
    s_stmts, s_san, s_diags = sanitize_and_split_student_sql(stray_sql)
    record_result(
        "Sanitizer strips stray trailing character (e.g. ';q') gracefully",
        any("stray_trailing_char" == d.get('type') for d in s_diags) and not any(s.strip() == 'q' for s in s_stmts)
    )

    # 4.3 Mode B Creative Mock Data: Logical reference query rewritten to student table mapping
    ref_q = "SELECT name, salary FROM staff WHERE salary > 50000 ORDER BY salary DESC;"
    manifest_map = {"tables": {"staff": "my_custom_employees"}}
    rewritten_q = rewrite_query_with_schema_mapping(ref_q, manifest_map)
    record_result(
        "Mode B: rewrite_query_with_schema_mapping remaps tables to student custom tables",
        "FROM my_custom_employees" in rewritten_q,
        f"Rewritten: {rewritten_q.strip()}"
    )

    # 4.4 AST Concept Matching: ORDER BY DESC LIMIT OFFSET matched despite table/column variation
    cand_pag = "SELECT product_title, price FROM stock_items ORDER BY price DESC LIMIT 3 OFFSET 2;"
    ref_pag = "SELECT product_name, cost FROM inventory ORDER BY cost DESC LIMIT 3 OFFSET 2;"
    matched_c, factor_c, expl_c = check_ast_concept_match(cand_pag, ref_pag)
    record_result(
        "AST Concept Matching: correctly awards >= 85% for demonstrated pagination logic",
        matched_c is True and factor_c >= 0.85,
        f"Factor: {factor_c}, Note: {expl_c}"
    )

    # 4.5 Prune bulk inserts for LLM prompt safety
    large_insert_sql = """
    INSERT INTO items (id, name) VALUES (1, 'A'), (2, 'B'), (3, 'C'), (4, 'D'), (5, 'E');
    SELECT * FROM items;
    """
    pruned = prune_bulk_inserts_for_llm(large_insert_sql, max_tuples=2)
    record_result(
        "Prune bulk inserts safely truncates repetitive tuples without breaking SQL syntax",
        "(1, 'A')" in pruned and "(2, 'B')" in pruned and "(5, 'E')" not in pruned and "SELECT * FROM items;" in pruned
    )


# ══════════════════════════════════════════════════════════════════════════
# Main Test Suite Runner
# ══════════════════════════════════════════════════════════════════════════
def main():
    print("======================================================================")
    print("🚀 SQL 2.0 Ground-Truth Regression & Enterprise Verification Suite")
    print("======================================================================")

    run_negative_security_tests()
    run_positive_tolerance_tests()
    run_enterprise_runners_tests()
    run_creative_and_sanitization_tests()

    print("\n======================================================================")
    print(f"📊 Regression Suite Summary: {passed_tests} PASSED, {failed_tests} FAILED out of {total_tests} tests.")
    print("======================================================================")

    if failed_tests > 0:
        print("\n❌ Failed Tests:")
        for ft in failed_test_names:
            print(f"  - {ft}")
        sys.exit(1)
    else:
        print("✅ ALL SQL 2.0 Ground-Truth Regression Tests PASSED WITH ZERO FAILURES!")
        sys.exit(0)


if __name__ == '__main__':
    main()
