"""
Test suite for creative mock datasets and pedagogical SQL evaluation.
"""
import sys
from schema_differ import sanitize_and_split_student_sql
from comparator import compare_query_results
from sql_runner import check_ast_concept_match

def run_tests():
    failures = 0
    print("=== Testing SQL Evaluator Creative & Pedagogical Enhancements ===\n")

    # 1. Test multi-line sanitization (prevent commenting out keywords)
    student_sql_raw = """
-- Step 1: Top 3 Highest Cost Items
SELECT product_name, wholesale_cost FROM product_inventory1
ORDER BY wholesale_cost DESC
LIMIT 3;

-- Step 2: Next tier
SELECT product_name, wholesale_cost FROM product_inventory1
ORDER BY wholesale_cost DESC
LIMIT 2 OFFSET 3;
    """
    stmts, sanitized_sql, diags = sanitize_and_split_student_sql(student_sql_raw)
    passed_san = "ORDER BY wholesale_cost DESC" in sanitized_sql and not "-- ORDER BY wholesale_cost DESC" in sanitized_sql
    print(f"{'✅' if passed_san else '❌'} Multi-line SQL clauses (ORDER BY) preserved without false auto-commenting")
    if not passed_san:
        failures += 1

    # 2. Test Creative Student Data Mode B: logical reference query execution on custom student dataset
    # Student inserted custom records: [('Widget Alpha', 999.0), ('Widget Beta', 450.0), ('Widget Gamma', 120.0)]
    # Under Mode B, the teacher's reference query runs against the student's dataset to recompute expected_rows.
    # Therefore, expected_rows matches the true filtered custom records.
    from schema_differ import rewrite_query_with_schema_mapping
    ref_q = "SELECT product_name, wholesale_cost FROM products WHERE wholesale_cost > 100 ORDER BY wholesale_cost DESC;"
    manifest_mapping = {"tables": {"products": "custom_inventory"}}
    rewritten_q = rewrite_query_with_schema_mapping(ref_q, manifest_mapping)
    passed_rewrite = "FROM custom_inventory" in rewritten_q

    expected_rows_mode_b = [('Widget Alpha', 999.0), ('Widget Beta', 450.0), ('Widget Gamma', 120.0)]
    expected_cols = ['product_name', 'wholesale_cost']
    actual_rows_creative = [('Widget Alpha', 999.0), ('Widget Beta', 450.0), ('Widget Gamma', 120.0)]
    actual_cols_creative = ['product_name', 'wholesale_cost']

    comp_creative = compare_query_results(
        actual_rows=actual_rows_creative,
        expected_rows=expected_rows_mode_b,
        actual_columns=actual_cols_creative,
        expected_columns=expected_cols,
        enforce_order=True,
        enforce_column_names=True,
        max_allowed_rows=100
    )

    passed_creative = comp_creative['passed'] and comp_creative['score_factor'] == 1.0 and passed_rewrite
    print(f"{'✅' if passed_creative else '❌'} Creative mock data (Mode B) with schema rewriting awarded {comp_creative['score_factor']*100}% credit: {comp_creative['reason']}")
    if not passed_creative:
        failures += 1

    # 3. Test Projection Tolerance: SELECT * returned extra column (product_id, product_name, wholesale_cost)
    # Teacher expected: 2 columns [product_name, wholesale_cost]
    expected_rows = [('Laptop', 1200.0), ('Phone', 800.0), ('Tablet', 500.0)]
    actual_rows_star = [(1, 'Laptop', 1200.0), (2, 'Phone', 800.0), (3, 'Tablet', 500.0)]
    actual_cols_star = ['product_id', 'product_name', 'wholesale_cost']

    comp_star = compare_query_results(
        actual_rows=actual_rows_star,
        expected_rows=expected_rows,
        actual_columns=actual_cols_star,
        expected_columns=expected_cols,
        enforce_order=True,
        enforce_column_names=True,
        max_allowed_rows=100
    )

    passed_star = comp_star['passed'] and comp_star['score_factor'] >= 0.80
    print(f"{'✅' if passed_star else '❌'} Extra column (SELECT *) projection tolerance awarded {comp_star['score_factor']*100}% credit: {comp_star['reason']}")
    if not passed_star:
        failures += 1

    # 4. Test AST Concept Matching for Pagination (LIMIT 2 OFFSET 3)
    student_pag_query = "SELECT product_name, wholesale_cost FROM product_inventory1 ORDER BY wholesale_cost DESC LIMIT 2 OFFSET 3;"
    ref_pag_query = "SELECT product_name, wholesale_cost FROM product_inventory ORDER BY wholesale_cost DESC LIMIT 2 OFFSET 3;"
    matched_pag, factor_pag, expl_pag = check_ast_concept_match(student_pag_query, ref_pag_query)
    passed_ast = matched_pag and factor_pag >= 0.85
    print(f"{'✅' if passed_ast else '❌'} AST Concept Match for Pagination awarded {factor_pag*100}%: {expl_pag}")
    if not passed_ast:
        failures += 1

    print(f"\nResult: {failures} failure(s)")
    sys.exit(0 if failures == 0 else 1)

if __name__ == '__main__':
    run_tests()
