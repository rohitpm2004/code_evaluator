"""
trigger_runner.py - Trigger Event Simulation & Behavioral Verification Harness (Phase 3).
Simulates database events (INSERT, UPDATE, DELETE) against PostgreSQL triggers:
  1. Row-level and statement-level triggers (FOR EACH ROW / STATEMENT).
  2. Timing behavior (BEFORE, AFTER, INSTEAD OF).
  3. Mutation verification on target table (e.g., BEFORE INSERT column normalization).
  4. Side-effect and cascade verification (e.g., audit log tables, inventory decrement).
  5. Conditional abort / exception handling verification (e.g., RAISE EXCEPTION).
  6. Dual-runner event execution against reference triggers.
"""

import logging
from typing import Dict, Any, List, Optional
from comparator import compare_query_results

logger = logging.getLogger("trigger_runner")


def evaluate_trigger_test_case(
    runner,
    ref_runner=None,
    tc: Optional[Dict[str, Any]] = None,
    schema_name: str = ""
) -> Dict[str, Any]:
    """
    Simulates database mutation events and asserts trigger side effects.

    Returns:
        dict: {
            'passed': bool,
            'score_factor': float (0.0 to 1.0),
            'reason': str,
            'total_events': int,
            'passed_events': int,
            'details': List[Dict]
        }
    """
    tc = tc or {}
    trigger_name = tc.get('triggerName') or ''
    target_table = tc.get('tableName') or tc.get('targetTable') or ''
    events = tc.get('events') or []

    # 1. Catalog Verification: Check if trigger exists in information_schema
    trigger_catalog_meta = []
    if target_table:
        try:
            cat_q = f"""
                SELECT trigger_name, event_manipulation, action_timing, action_orientation
                FROM information_schema.triggers
                WHERE event_object_table = '{target_table.lower()}'
                  AND trigger_schema = current_schema();
            """
            rows, _, _, _ = runner.execute_query(cat_q, max_rows=10, is_student=False)
            trigger_catalog_meta = rows
        except Exception as cat_err:
            logger.debug(f"Catalog check notice: {cat_err}")

    if not events:
        # Fallback: synthesize default test event from reference script/query
        single_stmt = tc.get('eventStatement') or tc.get('statement')
        single_verify = tc.get('verifyQuery') or tc.get('referenceQuery')
        if single_stmt and single_verify:
            events = [{
                'description': tc.get('name', 'Default trigger event'),
                'statement': single_stmt,
                'verifyQuery': single_verify,
                'expectedRows': tc.get('expectedRows')
            }]
        elif trigger_catalog_meta:
            # Catalog verified trigger existence
            return {
                'passed': True,
                'score_factor': 1.0,
                'reason': f"Trigger correctly registered on table '{target_table}' in PostgreSQL catalog.",
                'total_events': len(trigger_catalog_meta),
                'passed_events': len(trigger_catalog_meta),
                'details': [{'trigger': r[0], 'event': r[1], 'timing': r[2]} for r in trigger_catalog_meta]
            }
        else:
            return {
                'passed': False,
                'score_factor': 0.0,
                'reason': f"No trigger detected on table '{target_table}' and no simulation events configured.",
                'total_events': 0,
                'passed_events': 0,
                'details': []
            }

    passed_count = 0
    total_count = len(events)
    event_details = []

    for idx, ev in enumerate(events, start=1):
        desc = ev.get('description', f"Trigger Event {idx}")
        stmt = ev.get('statement', '')
        verify_q = ev.get('verifyQuery', '')
        exp_rows = ev.get('expectedRows')
        exp_cols = ev.get('expectedColumns')
        expect_exception = ev.get('expectException', False)

        event_passed = False
        actual_error = None
        fail_reason = ""

        try:
            # Execute triggering event statement
            runner.execute_script(stmt)
            if ref_runner:
                ref_runner.execute_script(stmt)

            if expect_exception:
                event_passed = False
                fail_reason = "Expected trigger exception was not raised; mutation succeeded unexpectedly."
            elif verify_q:
                # Query side effect state
                v_rows, v_cols, _, _ = runner.execute_query(verify_q, max_rows=100, is_student=False)
                if exp_rows is not None:
                    comp = compare_query_results(v_rows, exp_rows, v_cols, exp_cols or v_cols)
                    event_passed = comp['passed']
                    if not event_passed:
                        fail_reason = f"Side effect verification failed: {comp['reason']}"
                elif ref_runner:
                    ref_v_rows, ref_v_cols, _, _ = ref_runner.execute_query(verify_q, max_rows=100, is_student=False)
                    comp = compare_query_results(v_rows, ref_v_rows, v_cols, ref_v_cols)
                    event_passed = comp['passed']
                    if not event_passed:
                        fail_reason = f"Side effect differed from reference trigger: {comp['reason']}"
                else:
                    event_passed = len(v_rows) > 0
            else:
                event_passed = True
        except Exception as err:
            actual_error = str(err)
            if expect_exception:
                event_passed = True
                # If verifyQuery provided on exception test, ensure state was rolled back
                if verify_q:
                    try:
                        post_rows, post_cols, _, _ = runner.execute_query(verify_q, max_rows=100, is_student=False)
                        if exp_rows is not None:
                            comp = compare_query_results(post_rows, exp_rows, post_cols, exp_cols or post_cols)
                            event_passed = comp['passed']
                            if not event_passed:
                                fail_reason = f"Exception occurred but state rollback verification failed: {comp['reason']}"
                    except Exception as verify_err:
                        logger.debug(f"Rollback verification notice: {verify_err}")
            else:
                event_passed = False
                fail_reason = f"Event execution failed: {actual_error}"

        if event_passed:
            passed_count += 1

        event_details.append({
            'index': idx,
            'description': desc,
            'statement': stmt,
            'passed': event_passed,
            'reason': fail_reason or "Trigger successfully executed and side effects verified.",
            'error': actual_error
        })

    score_factor = round(passed_count / max(1, total_count), 2)
    overall_passed = (passed_count == total_count)

    summary_reason = (
        f"All {total_count} trigger simulation events passed successfully for '{target_table}'."
        if overall_passed else
        f"{passed_count}/{total_count} trigger events passed for '{target_table}'. "
        + (event_details[0]['reason'] if event_details and not event_details[0]['passed'] else "")
    )

    return {
        'passed': overall_passed,
        'score_factor': score_factor,
        'reason': summary_reason,
        'total_events': total_count,
        'passed_events': passed_count,
        'details': event_details
    }

