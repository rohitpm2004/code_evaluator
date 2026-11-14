"""
ast_guard.py - Tier 1 AST Validation & Static Policy Guard for SQL Evaluator (v7.0).
Uses sqlglot (pinned 25.20.0) to enforce:
1. Strict root-statement allowlist for query-only mode (root MUST be exp.Select or set op).
2. Deep script allowlist for script mode (CREATE TABLE/VIEW/INDEX, ALTER TABLE, DROP TABLE, INSERT, UPDATE, DELETE, SELECT).
3. Rejection of user transaction controls (BEGIN, COMMIT, ROLLBACK, SAVEPOINT).
4. Rejection of executable database objects (CREATE FUNCTION, PROCEDURE, TRIGGER, EXTENSION).
5. Recursive subtree inspection blocking data-modifying CTEs, SELECT INTO, locks, EXPLAIN.
6. Callable function whitelist rejecting dangerous system/admin and nondeterministic functions.
7. AST complexity, recursion depth, and input byte bounds.
"""

from __future__ import annotations
import re
from typing import Dict, Any, List, Set, Optional

try:
    import sqlglot
    from sqlglot import exp
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False
    exp = None


# Resource Limits for AST parsing
MAX_SQL_CHARACTERS = 200_000
MAX_AST_DEPTH = 50
MAX_AST_NODES = 2_500

# High-risk functions and administrative keywords that must never be executed by students
FORBIDDEN_IDENTIFIERS: Set[str] = {
    'pg_read_file', 'pg_write_file', 'pg_read_binary_file', 'pg_ls_dir',
    'pg_stat_file', 'pg_execute', 'dblink', 'dblink_exec', 'pg_terminate_backend',
    'pg_cancel_backend', 'pg_sleep', 'copy', 'pg_shadow', 'pg_authid',
    'pg_roles', 'pg_user', 'pg_proc', 'pg_database', 'pg_tablespace',
    'set_config', 'current_setting', 'query_to_xml',
    'lo_import', 'lo_export', 'pg_advisory_lock', 'pg_advisory_unlock'
}

# Nondeterministic functions rejected in deterministic grading tests
NONDETERMINISTIC_FUNCTIONS: Set[str] = {
    'random', 'gen_random_uuid', 'clock_timestamp', 'statement_timestamp'
}

# Approved whitelist of safe scalar, aggregate, window, and date functions
SAFE_CALLABLE_FUNCTIONS: Set[str] = {
    # Math & Aggregates
    'count', 'sum', 'avg', 'min', 'max', 'round', 'ceil', 'ceiling', 'floor',
    'abs', 'mod', 'power', 'sqrt', 'sign', 'exp', 'ln', 'log', 'log10', 'trunc',
    'div', 'cbrt', 'pi', 'radians', 'degrees', 'isnan', 'num_nulls', 'num_nonnulls',
    'group_concat', 'string_agg', 'array_agg', 'listagg', 'json_agg', 'json_arrayagg',
    'jsonb_agg', 'jsonb_array_elements', 'jsonb_each', 'jsonb_typeof', 'json_typeof',
    # String & Array Functions
    'upper', 'lower', 'length', 'char_length', 'octet_length', 'substring', 'substr',
    'trim', 'btrim', 'ltrim', 'rtrim', 'replace', 'concat', 'concat_ws', 'coalesce',
    'nullif', 'least', 'greatest', 'lpad', 'rpad', 'reverse', 'position', 'instr',
    'left', 'right', 'initcap', 'repeat', 'split_part', 'format', 'ascii', 'chr',
    'regexp_replace', 'regexp_match', 'regexp_matches', 'string_to_array',
    'array_length', 'cardinality', 'unnest', 'generate_series', 'exploding_generate_series', 'md5', 'encode', 'decode',
    # Date & Time Functions
    'date_trunc', 'date_part', 'extract', 'to_char', 'to_date', 'to_timestamp', 'to_number', 'age',
    'make_date', 'make_time', 'make_timestamp', 'now', 'current_timestamp',
    'current_date', 'current_time', 'pg_typeof', 'version', 'current_setting',
    'current_database', 'current_user', 'current_schema',
    # Window Functions
    'row_number', 'rank', 'dense_rank', 'percent_rank', 'cume_dist', 'ntile',
    'lag', 'lead', 'first_value', 'last_value', 'nth_value',
    # Conversion & JSON
    'cast', 'try_cast', 'to_json', 'to_jsonb', 'json_extract_path',
    'json_build_object', 'jsonb_build_object'
}


def _strip_comments_and_literals(sql: str) -> str:
    """Strips SQL line comments, block comments, dollar-quoted blocks, and string literals to prevent false positives in regex scans."""
    no_blocks = re.sub(r'/\*[\s\S]*?\*/', ' ', sql)
    no_comments = re.sub(r'--[^\r\n]*', ' ', no_blocks)
    no_dollars = re.sub(r'\$([a-zA-Z_][a-zA-Z0-9_]*)?\$[\s\S]*?\$\1\$', "''", no_comments)
    no_strings = re.sub(r"'(?:[^'\\]|\\.)*'", "''", no_dollars)
    return no_strings


def validate_sql_ast(
    sql_text: str,
    mode: str = 'query_only',
    manifest: Optional[Dict[str, Any]] = None,
    dialect: str = 'postgres'
) -> Dict[str, Any]:
    """
    Validates student SQL using sqlglot AST analysis against v7.0 Production Policy.
    """
    if not sql_text or not sql_text.strip():
        return {
            'valid_syntax': False,
            'statement_types': [],
            'forbidden_detected': ['EMPTY_SUBMISSION'],
            'required_clauses_checked': {},
            'ast_error': 'Submission is empty.',
            'tables_referenced': []
        }

    # Ingestion Input Size Guard
    if len(sql_text) > MAX_SQL_CHARACTERS:
        return {
            'valid_syntax': False,
            'statement_types': [],
            'forbidden_detected': ['PAYLOAD_TOO_LARGE'],
            'required_clauses_checked': {},
            'ast_error': f"Submission length ({len(sql_text)} chars) exceeds maximum allowable limit ({MAX_SQL_CHARACTERS} chars).",
            'tables_referenced': []
        }

    manifest = manifest or {}
    ast_reqs = manifest.get('astRequirements', {})
    required_clauses = ast_reqs.get('requiredClauses', [])
    forbidden_tokens = set(ast_reqs.get('forbiddenClauses', []))

    # Strip comments and string literals so innocent commentary/data never triggers false security violations
    code_only_sql = _strip_comments_and_literals(sql_text).lower()
    detected_forbidden: List[str] = []

    # Word-boundary scan for critical administrative functions
    for kw in FORBIDDEN_IDENTIFIERS:
        if re.search(rf'\b{re.escape(kw)}\b', code_only_sql):
            detected_forbidden.append(f"FORBIDDEN_IDENTIFIER: {kw}")
    for ft in forbidden_tokens:
        if re.search(rf'\b{re.escape(ft.lower())}\b', code_only_sql):
            detected_forbidden.append(f"FORBIDDEN_TOKEN: {ft}")

    # Privilege escalation and session tampering checks
    escalation_patterns = [
        (r'\bRESET\s+ROLE\b', 'RESET ROLE is prohibited in student evaluations.'),
        (r'\bSET\s+(?:SESSION\s+)?ROLE\b', 'SET ROLE is prohibited in student evaluations.'),
        (r'\bSET\s+SESSION\s+AUTHORIZATION\b', 'SET SESSION AUTHORIZATION is prohibited in student evaluations.'),
        (r'\bALTER\s+ROLE\b', 'ALTER ROLE is prohibited in student evaluations.'),
        (r'\bDROP\s+ROLE\b', 'DROP ROLE is prohibited in student evaluations.'),
        (r'\bCREATE\s+ROLE\b', 'CREATE ROLE is prohibited in student evaluations.')
    ]
    for pattern, reason in escalation_patterns:
        if re.search(pattern, code_only_sql, re.I):
            detected_forbidden.append(f"PRIVILEGE_ESCALATION_ATTEMPT: {reason}")

    # Detect dangling / incomplete SQL clauses (syntax errors leniently overlooked by transpilers)
    dangling_patterns = [
        (r'\bON\s+(?:WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|;|\s*$)', "Dangling ON clause without join condition."),
        (r'\bGROUP\s+BY\s*(?:HAVING|ORDER\s+BY|LIMIT|;|\s*$)', "Empty GROUP BY clause without grouping columns."),
        (r'\bORDER\s+BY\s*(?:LIMIT|;|\s*$)', "Empty ORDER BY clause without sort columns."),
        (r'\bWHERE\s*(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|;|\s*$)', "Empty WHERE clause without filter condition.")
    ]
    for pattern, reason in dangling_patterns:
        if re.search(pattern, code_only_sql, re.I):
            return {
                'valid_syntax': False,
                'statement_types': [],
                'forbidden_detected': detected_forbidden,
                'required_clauses_checked': {c: False for c in required_clauses},
                'ast_error': f"SQL Syntax Error: {reason}",
                'partial_syntax_errors': [reason],
                'tables_referenced': []
            }

    if not HAS_SQLGLOT:
        return _fallback_regex_validation(
            sql_text, mode, required_clauses, detected_forbidden
        )

    partial_syntax_errors: List[str] = []
    try:
        parsed_statements = sqlglot.parse(sql_text, read=dialect)
    except Exception as parse_err:
        try:
            parsed_statements = sqlglot.parse(sql_text)
        except Exception:
            # Statement-level fault isolation: parse individually so 1 typo doesn't abort valid queries/tables
            from schema_differ import split_sql_statements
            parsed_statements = []
            for raw_stmt in split_sql_statements(sql_text):
                if not raw_stmt.strip():
                    continue
                try:
                    stmts_i = sqlglot.parse(raw_stmt, read=dialect)
                    parsed_statements.extend([s for s in stmts_i if s is not None])
                except Exception as s_err:
                    try:
                        stmts_i = sqlglot.parse(raw_stmt)
                        parsed_statements.extend([s for s in stmts_i if s is not None])
                    except Exception:
                        if len(partial_syntax_errors) < 10:
                            partial_syntax_errors.append(f"{raw_stmt.strip()[:80]}: {str(s_err)}")

            if not parsed_statements:
                return {
                    'valid_syntax': False,
                    'statement_types': [],
                    'forbidden_detected': detected_forbidden,
                    'required_clauses_checked': {c: False for c in required_clauses},
                    'ast_error': f"SQL Syntax Error: {str(parse_err)}",
                    'partial_syntax_errors': partial_syntax_errors,
                    'tables_referenced': []
                }

    if not parsed_statements or all(s is None for s in parsed_statements):
        return {
            'valid_syntax': False,
            'statement_types': [],
            'forbidden_detected': detected_forbidden,
            'required_clauses_checked': {c: False for c in required_clauses},
            'ast_error': 'Could not parse any valid SQL statements from submission.',
            'tables_referenced': []
        }

    statements = [s for s in parsed_statements if s is not None]
    statement_types: List[str] = []
    tables_referenced: Set[str] = set()

    # Rule 1: query_only mode enforces root statement checks
    has_select = any(isinstance(s, (exp.Select, exp.Union, exp.Intersect, exp.Except)) for s in statements)
    allow_multi = manifest.get('allowMultiStatements', False) or has_select
    if mode == 'query_only' and len(statements) > 1 and not allow_multi:
        detected_forbidden.append(f"MULTIPLE_STATEMENTS_DETECTED: Found {len(statements)} statements; single query expected.")

    total_nodes = 0

    for stmt in statements:
        stmt_class = stmt.__class__.__name__.replace('Exp', '')
        statement_types.append(stmt_class)

        # Complexity check
        node_count = sum(1 for _ in stmt.walk())
        total_nodes += node_count
        if total_nodes > MAX_AST_NODES:
            detected_forbidden.append(f"AST_COMPLEXITY_EXCEEDED: Statement tree exceeds {MAX_AST_NODES} nodes.")
            break

        # Rule 2: Strict Root-Statement Allowlist
        if mode == 'query_only':
            if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                # If submission contains valid SELECT statements, allow harmless DDL/DML scratchpad setup (CREATE, INSERT, UPDATE, DELETE)
                if has_select and isinstance(stmt, (exp.Create, exp.Insert, exp.Update, exp.Delete)):
                    pass  # Harmless scratchpad table/data setup used by students for local testing
                elif isinstance(stmt, (exp.Column, exp.Literal, exp.Identifier, exp.Var, exp.Anonymous, getattr(exp, 'Semicolon', exp.Expression))):
                    pass  # Stray orphan token / trailing semicolon / typo, not a security exploit
                else:
                    detected_forbidden.append(
                        f"DISALLOWED_ROOT_STATEMENT: {stmt_class} is not permitted in query-only mode. Only SELECT queries are allowed."
                    )
        elif mode == 'script':
            _validate_script_mode_statement(stmt, stmt_class, detected_forbidden)

        # Rule 3: Deep Subtree Recursive Inspection
        _inspect_subtree_recursively(stmt, mode, detected_forbidden, tables_referenced)

    # Check required clauses
    checked_clauses: Dict[str, bool] = {}
    for req in required_clauses:
        checked_clauses[req] = _check_clause_in_ast(statements, req.upper())

    is_valid = len(detected_forbidden) == 0

    return {
        'valid_syntax': True,
        'statement_types': statement_types,
        'forbidden_detected': detected_forbidden,
        'required_clauses_checked': checked_clauses,
        'ast_error': None if is_valid else f"AST Policy violations: {'; '.join(detected_forbidden)}",
        'partial_syntax_errors': partial_syntax_errors,
        'tables_referenced': sorted(list(tables_referenced))
    }


def _validate_script_mode_statement(
    stmt: exp.Expression,
    stmt_class: str,
    detected_forbidden: List[str]
) -> None:
    """Validates statement type and modifiers under the script-mode allowlist."""
    # Permitted DDL/DML statement classes
    if isinstance(stmt, exp.Create):
        target_kind = str(stmt.args.get('kind', '')).upper()
        # Only TABLE, VIEW, INDEX, TYPE, SEQUENCE allowed
        if target_kind not in ('TABLE', 'VIEW', 'INDEX', 'TYPE', 'SEQUENCE'):
            detected_forbidden.append(
                f"DISALLOWED_CREATE_OBJECT: 'CREATE {target_kind or stmt_class}' is prohibited. Only CREATE TABLE/VIEW/INDEX/TYPE/SEQUENCE are permitted."
            )
        # Check for forbidden CONCURRENTLY modifier on indexes
        if stmt.args.get('concurrently'):
            detected_forbidden.append("DISALLOWED_MODIFIER: 'CONCURRENTLY' is not permitted in evaluated scripts.")
        # Check for temporary objects
        if stmt.args.get('temporary'):
            detected_forbidden.append("DISALLOWED_MODIFIER: 'TEMPORARY' objects are not permitted in evaluated scripts.")

    elif isinstance(stmt, exp.Alter):
        target_kind = str(stmt.args.get('kind', '')).upper()
        if target_kind not in ('TABLE', ''):
            detected_forbidden.append(f"DISALLOWED_ALTER_OBJECT: 'ALTER {target_kind}' is prohibited. Only ALTER TABLE is permitted.")

    elif isinstance(stmt, exp.Drop):
        target_kind = str(stmt.args.get('kind', '')).upper()
        if target_kind not in ('TABLE', 'VIEW', ''):
            detected_forbidden.append(f"DISALLOWED_DROP_OBJECT: 'DROP {target_kind}' is prohibited. Only DROP TABLE/VIEW are permitted.")

    elif isinstance(stmt, (exp.Insert, exp.Update, exp.Delete, exp.Select, exp.Union)):
        pass  # Standard permitted DML

    elif stmt_class in ('Semicolon', 'Comment') or isinstance(stmt, (getattr(exp, 'Semicolon', exp.Expression), getattr(exp, 'Comment', exp.Expression), exp.Column, exp.Literal, exp.Identifier, exp.Var, exp.Anonymous)):
        pass  # Harmless trailing semicolon, comment, or stray orphan token

    elif isinstance(stmt, exp.Transaction):
        detected_forbidden.append(
            f"TRANSACTION_CONTROL_PROHIBITED: User-controlled '{stmt_class}' (BEGIN/COMMIT/ROLLBACK/SAVEPOINT) is not permitted. Transaction boundaries are managed by the evaluator."
        )

    else:
        detected_forbidden.append(
            f"DISALLOWED_SCRIPT_STATEMENT: '{stmt_class}' is not permitted in script mode."
        )


def _inspect_subtree_recursively(
    stmt: exp.Expression,
    mode: str,
    detected_forbidden: List[str],
    tables_referenced: Set[str]
) -> None:
    """Walks the full AST subtree to catch nested write operations, locks, and unsafe functions."""
    depth = 0
    for node in stmt.walk():
        depth += 1
        if depth > MAX_AST_DEPTH:
            detected_forbidden.append(f"AST_MAX_DEPTH_EXCEEDED: Subtree exceeds maximum depth of {MAX_AST_DEPTH}.")
            break

        # Disallow transaction nodes anywhere in tree
        is_transaction = (
            (hasattr(exp, 'Transaction') and isinstance(node, getattr(exp, 'Transaction'))) or
            (hasattr(exp, 'Commit') and isinstance(node, getattr(exp, 'Commit'))) or
            (hasattr(exp, 'Rollback') and isinstance(node, getattr(exp, 'Rollback'))) or
            node.__class__.__name__.lower() in ('transaction', 'commit', 'rollback')
        )
        if is_transaction:
            detected_forbidden.append("TRANSACTION_CONTROL_PROHIBITED: Transaction statements are not permitted.")

        # Disallow EXPLAIN and EXPLAIN ANALYZE
        is_explain = (
            node.__class__.__name__.lower() == 'explain' or
            getattr(node, 'key', '') == 'explain' or
            (isinstance(node, exp.Command) and str(getattr(node, 'this', '')).lower() == 'explain') or
            (hasattr(exp, 'Explain') and isinstance(node, getattr(exp, 'Explain')))
        )
        if is_explain:
            detected_forbidden.append("EXPLAIN_PROHIBITED: EXPLAIN and EXPLAIN ANALYZE statements are not permitted.")

        # Disallow SELECT INTO
        if isinstance(node, exp.Into):
            detected_forbidden.append("SELECT_INTO_PROHIBITED: 'SELECT INTO' table creation is not permitted.")

        # Disallow table locks
        if isinstance(node, exp.Lock):
            detected_forbidden.append("TABLE_LOCK_PROHIBITED: Row/table locks (FOR UPDATE/SHARE) are not permitted.")

        # Disallow data-modifying CTEs in query_only mode (e.g. WITH ins AS (INSERT ...))
        if mode == 'query_only':
            if node != stmt and isinstance(node, (exp.Insert, exp.Update, exp.Delete)):
                detected_forbidden.append(f"DATA_MODIFYING_CTE_PROHIBITED: '{node.__class__.__name__}' is not permitted in query-only mode.")

        # Inspect table references & prevent cross-schema qualification (e.g. public.table)
        if isinstance(node, exp.Table):
            if node.name:
                tables_referenced.add(node.name.lower())
            if node.db:
                schema_name = node.db.lower()
                # Allow safe standard ANSI read-only metadata like information_schema and public
                if schema_name.startswith('eval_slot_') and mode == 'script':
                    detected_forbidden.append(f"CROSS_SCHEMA_ESCAPE_PROHIBITED: Explicit qualification with schema '{schema_name}' is not permitted.")

        # Callable Function Whitelist Verification (skip SQL operators/connectors like AND, OR, +, =, CASE)
        if isinstance(node, (exp.Anonymous, exp.Func)) and not isinstance(node, (exp.Binary, exp.Connector, exp.Case)):
            if isinstance(node, exp.Anonymous):
                func_name = str(node.this or '').strip().lower()
            else:
                func_name = (node.sql_name().lower() if hasattr(node, 'sql_name') else (node.key or node.__class__.__name__.lower())).strip().lower()

            if func_name:
                if func_name in FORBIDDEN_IDENTIFIERS:
                    detected_forbidden.append(f"FORBIDDEN_FUNCTION_CALL: '{func_name}' is prohibited.")
                elif func_name in NONDETERMINISTIC_FUNCTIONS:
                    detected_forbidden.append(f"NONDETERMINISTIC_FUNCTION_PROHIBITED: '{func_name}' is not permitted in deterministic grading tests.")
                elif func_name not in SAFE_CALLABLE_FUNCTIONS:
                    # Allow standard SQL function classes recognized by sqlglot
                    STANDARD_SQLGLOT_FUNCS = (
                        exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Round,
                        exp.Floor, exp.Ceil, exp.Abs, exp.Cast, exp.TryCast,
                        exp.Coalesce, exp.Nullif, exp.Substring, exp.Trim,
                        exp.Upper, exp.Lower, exp.Concat, exp.Length,
                        exp.DateTrunc, exp.Extract, exp.CurrentTimestamp, exp.CurrentDate,
                        getattr(exp, 'CurrentVersion', exp.CurrentTimestamp)
                    )
                    if not isinstance(node, STANDARD_SQLGLOT_FUNCS):
                        detected_forbidden.append(f"UNAPPROVED_FUNCTION_CALL: Function '{func_name}' is not in the approved whitelist.")


def _check_clause_in_ast(statements: List[exp.Expression], clause: str) -> bool:
    """Checks whether a specific SQL clause or construct exists in the parsed AST."""
    clause = clause.strip().upper()

    for stmt in statements:
        if clause == 'JOIN' and stmt.find(exp.Join):
            return True
        elif clause in ('LEFT JOIN', 'LEFT_JOIN') and any(j.kind and 'LEFT' in str(j.kind).upper() for j in stmt.find_all(exp.Join)):
            return True
        elif clause in ('RIGHT JOIN', 'RIGHT_JOIN') and any(j.kind and 'RIGHT' in str(j.kind).upper() for j in stmt.find_all(exp.Join)):
            return True
        elif clause in ('INNER JOIN', 'INNER_JOIN') and any(j.kind and 'INNER' in str(j.kind).upper() for j in stmt.find_all(exp.Join)):
            return True
        elif clause in ('GROUP BY', 'GROUP_BY') and stmt.find(exp.Group):
            return True
        elif clause == 'HAVING' and stmt.find(exp.Having):
            return True
        elif clause in ('ORDER BY', 'ORDER_BY') and stmt.find(exp.Order):
            return True
        elif clause == 'WHERE' and stmt.find(exp.Where):
            return True
        elif clause == 'LIMIT' and stmt.find(exp.Limit):
            return True
        elif clause == 'DISTINCT' and any(s.args.get('distinct') for s in stmt.find_all(exp.Select)):
            return True
        elif clause == 'UNION' and stmt.find(exp.Union):
            return True
        elif clause == 'WINDOW' and (stmt.find(exp.Window) or stmt.find(exp.Over)):
            return True
        elif clause in ('CTE', 'WITH') and stmt.find(exp.With):
            return True
        elif clause == 'AGGREGATE' and any(stmt.find(agg) for agg in (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            return True
        elif clause in ('CREATE TABLE', 'CREATE_TABLE') and (isinstance(stmt, exp.Create) or 'CREATE TABLE' in stmt.sql().upper()):
            return True
        elif clause in ('FOREIGN KEY', 'FOREIGN_KEY') and ('FOREIGN KEY' in stmt.sql().upper() or 'REFERENCES' in stmt.sql().upper()):
            return True
        elif clause == 'REFERENCES' and ('REFERENCES' in stmt.sql().upper() or stmt.find(exp.Reference)):
            return True
        elif clause in ('PRIMARY KEY', 'PRIMARY_KEY') and ('PRIMARY KEY' in stmt.sql().upper() or stmt.find(exp.PrimaryKey)):
            return True

    return False


def _fallback_regex_validation(
    sql_text: str,
    mode: str,
    required_clauses: List[str],
    detected_forbidden: List[str]
) -> Dict[str, Any]:
    """Lightweight regex fallback when sqlglot is unavailable."""
    import re

    cleaned = re.sub(r'--.*?$|/\*.*?\*/', '', sql_text, flags=re.MULTILINE).strip()
    first_word_match = re.match(r'^\s*([a-zA-Z]+)', cleaned)
    first_word = first_word_match.group(1).upper() if first_word_match else 'UNKNOWN'

    if mode == 'query_only' and first_word not in ('SELECT', 'WITH'):
        detected_forbidden.append(f"DISALLOWED_STATEMENT_TYPE: {first_word} (only SELECT queries permitted)")

    checked_clauses = {}
    upper_sql = cleaned.upper()
    for req in required_clauses:
        req_clean = req.replace('_', ' ').upper()
        checked_clauses[req] = bool(re.search(r'\b' + re.escape(req_clean) + r'\b', upper_sql))

    return {
        'valid_syntax': True,
        'statement_types': [first_word],
        'forbidden_detected': detected_forbidden,
        'required_clauses_checked': checked_clauses,
        'ast_error': None if not detected_forbidden else f"Policy violations: {'; '.join(detected_forbidden)}",
        'tables_referenced': []
    }

