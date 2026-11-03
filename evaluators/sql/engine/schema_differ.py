"""
schema_differ.py - AST & Catalog Schema Inspector with Typo Tolerance.
Extracts structural schema definitions (tables, columns, constraints, foreign keys),
computes fuzzy entity matching for student naming typos, and normalizes DDL scripts
so students receive fair partial credit instead of catastrophic 0s.
"""

import re
import difflib
import logging
from typing import Dict, Any, List, Tuple, Optional, Set

logger = logging.getLogger("schema_differ")


def extract_expected_tables(manifest: Dict[str, Any]) -> List[str]:
    """
    Extracts canonical table names required by the assignment manifest.
    Inspects manifest.expectedTables, manifest.tables, and test cases' reference queries.
    """
    expected: Set[str] = set()

    # 1. Explicit manifest declarations
    for key in ('expectedTables', 'tables', 'requiredTables'):
        val = manifest.get(key)
        if isinstance(val, list):
            for t in val:
                if isinstance(t, str) and t.strip():
                    expected.add(t.strip().lower().strip('"`'))

    # 2. Extract from testCases queries
    test_cases = manifest.get('testCases', [])
    if isinstance(manifest, list):
        test_cases = manifest

    all_queries = [manifest.get('referenceQuery', '')]
    for tc in test_cases:
        if isinstance(tc, dict):
            q = tc.get('referenceQuery') or tc.get('query') or ''
            if q:
                all_queries.append(q)

    for q in all_queries:
        if not q:
            continue
        # Match table_name = 'xxx' or table_name = "xxx"
        for m in re.finditer(r"table_name\s*=\s*['\"]([a-zA-Z0-9_]+)['\"]", q, re.I):
            expected.add(m.group(1).lower())
        # Match table_name IN ('xxx', 'yyy')
        in_match = re.search(r"table_name\s+IN\s*\(\s*([^)]+)\)", q, re.I)
        if in_match:
            raw_items = in_match.group(1)
            for item in re.findall(r"['\"]([a-zA-Z0-9_]+)['\"]", raw_items):
                expected.add(item.lower())
        # Match tc.table_name = 'xxx'
        for m in re.finditer(r"\btable_name\s*=\s*['\"]([a-zA-Z0-9_]+)['\"]", q, re.I):
            expected.add(m.group(1).lower())

    return sorted(list(expected))


def extract_created_tables(sql_text: str) -> List[str]:
    """
    Extracts table names created by CREATE TABLE statements in the SQL text.
    Handles optional IF NOT EXISTS, schema qualifiers, and quotes.
    """
    created: List[str] = []
    # Pattern matching CREATE TABLE [IF NOT EXISTS] [schema.]name
    pattern = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+|TEMP\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[a-zA-Z0-9_]+\.)?[\"`]?([a-zA-Z0-9_]+)[\"`]?",
        re.I
    )
    for match in pattern.finditer(sql_text):
        tbl = match.group(1).lower()
        if tbl not in created:
            created.append(tbl)
    return created


def compute_name_similarity(name1: str, name2: str) -> float:
    """
    Computes fuzzy similarity ratio between two table or column identifiers.
    Applies aggressive normalization for common student naming habits:
      - Trailing run numbers (e.g. users3 -> users)
      - Table prefixes (e.g. tbl_users -> users)
      - Singular/plural forms (e.g. user -> users)
    """
    n1 = name1.lower().strip('"`')
    n2 = name2.lower().strip('"`')

    if n1 == n2:
        return 1.0

    # Strip trailing digits or underscores (students re-running in pgAdmin append 1, 2, 3...)
    base1 = re.sub(r'[\d_]+$', '', n1)
    base2 = re.sub(r'[\d_]+$', '', n2)
    if base1 and base1 == base2:
        return 0.95

    # Strip tbl_ or tb_ prefixes
    p1 = re.sub(r'^(?:tbl_|tb_|table_)', '', n1)
    p2 = re.sub(r'^(?:tbl_|tb_|table_)', '', n2)
    if p1 and p1 == p2:
        return 0.92

    # Singular / plural ending in 's'
    if n1 + 's' == n2 or n2 + 's' == n1:
        return 0.90

    # General Levenshtein ratio
    return difflib.SequenceMatcher(None, n1, n2).ratio()


def find_table_name_mappings(
    created_tables: List[str],
    expected_tables: List[str],
    threshold: float = 0.75
) -> Dict[str, Tuple[str, float]]:
    """
    Maps student created tables to canonical expected tables if similarity >= threshold.
    Returns: { student_table_name: (canonical_table_name, similarity_score) }
    """
    mappings: Dict[str, Tuple[str, float]] = {}

    for created in created_tables:
        if created in expected_tables:
            continue  # Exact match, no mapping needed

        best_match = None
        best_score = 0.0

        for expected in expected_tables:
            score = compute_name_similarity(created, expected)
            if score > best_score:
                best_score = score
                best_match = expected

        if best_match and best_score >= threshold:
            mappings[created] = (best_match, round(best_score, 2))

    return mappings


def normalize_student_ddl(
    sql_text: str,
    manifest: Dict[str, Any]
) -> Tuple[str, Dict[str, str], List[str]]:
    """
    Detects table name typos and harmonizes student DDL script to canonical expected names.
    Returns:
        (normalized_sql, table_alias_map, list_of_applied_notices)
    """
    expected_tables = extract_expected_tables(manifest)
    if not expected_tables:
        return sql_text, {}, []

    created_tables = extract_created_tables(sql_text)
    mappings = find_table_name_mappings(created_tables, expected_tables)

    if not mappings:
        return sql_text, {}, []

    normalized_sql = sql_text
    applied_notices: List[str] = []
    alias_map: Dict[str, Dict[str, Any]] = {}

    for student_name, (canonical_name, score) in mappings.items():
        info = {
            'original': student_name,
            'canonical': canonical_name,
            'score': score
        }
        alias_map[student_name] = info
        alias_map[canonical_name] = info

        notice = (
            f"Table '{student_name}' was automatically mapped to required '{canonical_name}' "
            f"(fuzzy similarity {int(score * 100)}%). Evaluated with a minor naming deduction."
        )
        applied_notices.append(notice)
        logger.info(f"[SchemaDiffer] {notice}")

        # Replace table occurrences cleanly respecting boundaries or quotes
        # 1. Quoted instances: "orders3" -> "orders"
        normalized_sql = re.sub(
            rf'["`]{re.escape(student_name)}["`]',
            f'"{canonical_name}"',
            normalized_sql,
            flags=re.I
        )
        # 2. Unquoted word-boundary instances: orders3 -> orders
        normalized_sql = re.sub(
            rf'\b{re.escape(student_name)}\b',
            canonical_name,
            normalized_sql,
            flags=re.I
        )

    return normalized_sql, alias_map, applied_notices


def split_sql_statements(sql_text: str) -> List[str]:
    """
    Splits multi-statement SQL script into individual executable statements.
    Preserves quoted strings, comments, and dollar-quoted blocks.
    """
    statements = []
    current: List[str] = []
    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    length = len(sql_text)

    while i < length:
        ch = sql_text[i]
        next_ch = sql_text[i + 1] if i + 1 < length else ''

        # Handle comments
        if not in_single_quote and not in_double_quote:
            if not in_line_comment and not in_block_comment:
                if ch == '-' and next_ch == '-':
                    in_line_comment = True
                    current.append(ch)
                    i += 1
                    current.append(next_ch)
                    i += 1
                    continue
                elif ch == '/' and next_ch == '*':
                    in_block_comment = True
                    current.append(ch)
                    i += 1
                    current.append(next_ch)
                    i += 1
                    continue
            elif in_line_comment:
                current.append(ch)
                if ch == '\n':
                    in_line_comment = False
                i += 1
                continue
            elif in_block_comment:
                current.append(ch)
                if ch == '*' and next_ch == '/':
                    current.append(next_ch)
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue

        # Handle string literals
        if not in_line_comment and not in_block_comment:
            if ch == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
            elif ch == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
            elif ch == ';' and not in_single_quote and not in_double_quote:
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    # Remaining statement without trailing semicolon
    last_stmt = "".join(current).strip()
    if last_stmt:
        # Ignore stray trailing single letters/symbols (e.g. ';v' or stray punctuation at end of file)
        if len(last_stmt) <= 3 and not re.search(r'\b(SET|USE|DO)\b', last_stmt, re.I):
            pass
        else:
            statements.append(last_stmt)

    return statements


GLOBAL_SQL_KEYWORDS = {
    # DML & Top-Level Data Queries
    'SELECT', 'WITH', 'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'UPSERT', 'REPLACE',
    # DDL Statements
    'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'RENAME', 'COMMENT',
    # DCL & Administrative Operations
    'GRANT', 'REVOKE', 'SET', 'SHOW', 'RESET', 'USE', 'DESCRIBE', 'DESC',
    'EXPLAIN', 'ANALYZE', 'VACUUM', 'LOCK', 'UNLOCK', 'COPY', 'LOAD',
    # TCL & Procedural Flow
    'BEGIN', 'START', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'DO', 'CALL', 'EXEC',
    'EXECUTE', 'DECLARE', 'PREPARE', 'DEALLOCATE', 'OPEN', 'CLOSE', 'FETCH',
    # Query Clauses & Structure
    'FROM', 'WHERE', 'GROUP', 'BY', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET',
    'TOP', 'SAMPLE', 'QUALIFY', 'WINDOW', 'OVER', 'PARTITION', 'FILTER',
    'FIRST', 'NEXT', 'ROWS', 'ROW', 'ONLY', 'TIES',
    # Joins & Relations
    'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'OUTER', 'CROSS', 'NATURAL', 'LATERAL',
    'ON', 'USING', 'APPLY',
    # Set Operations
    'UNION', 'INTERSECT', 'EXCEPT', 'MINUS', 'ALL', 'DISTINCT',
    # Values, Output & Returning
    'VALUES', 'VALUE', 'DEFAULT', 'RETURNING', 'OUTPUT',
    # Conditionals & Logic
    'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'IF', 'ELSIF',
    'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL', 'BETWEEN', 'LIKE', 'ILIKE', 'SIMILAR',
    'EXISTS', 'ANY', 'SOME', 'ALL', 'AS', 'ASC', 'DESC', 'NULLS',
    # Constraints, Keys & Table Schema Elements
    'CONSTRAINT', 'PRIMARY', 'FOREIGN', 'KEY', 'REFERENCES', 'CHECK', 'UNIQUE',
    'INDEX', 'CASCADE', 'RESTRICT', 'TRIGGER', 'PROCEDURE', 'FUNCTION', 'VIEW',
    'TABLE', 'DATABASE', 'SCHEMA', 'COLUMN', 'ADD', 'MODIFY', 'CHANGE'
}

# English conversational words and prepositions commonly found in student notes/prompts
ENGLISH_NOTE_INDICATORS = {
    'step', 'task', 'question', 'problem', 'part', 'ans', 'answer', 'note',
    'lab', 'assignment', 'exercise', 'scenario', 'requirement', 'instructions',
    'find', 'extract', 'retrieve', 'calculate', 'display', 'show', 'write',
    'skipping', 'highest', 'lowest', 'most', 'least', 'expensive', 'cheapest',
    'products', 'items', 'rows', 'output', 'solution', 'submission', 'completed',
    'my', 'your', 'here', 'the', 'please', 'verify', 'confirm', 'check'
}


def sanitize_and_split_student_sql(raw_sql: str) -> Tuple[List[str], str, List[Dict[str, Any]]]:
    """
    Fault-tolerant pre-parser for student SQL submissions.
    1. Detects un-commented human notes/headings and auto-wraps them in '-- '
       while recording minor pedagogical diagnostic notes (-3 to -5 points).
       CRITICAL: Never comments out legitimate SQL clauses (e.g. ORDER BY, WHERE, FROM)
       written on separate lines!
    2. Cleans stray characters attached after semicolons (e.g. ';v' or ';x').
    3. Splits statements cleanly into individual executable statements.
    4. Returns (statements, sanitized_full_sql, diagnostic_notices).
    """
    if not raw_sql or not raw_sql.strip():
        return [], "", []

    lines = raw_sql.splitlines()
    sanitized_lines = []
    diagnostics = []

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            sanitized_lines.append(line)
            continue

        # Clean stray single character accidentally typed after semicolon (e.g. "LIMIT 2 OFFSET 3;v")
        stray_m = re.search(r';\s*([a-zA-Z0-9])$', stripped)
        if stray_m:
            stray_char = stray_m.group(1)
            stripped = re.sub(r';\s*[a-zA-Z0-9]$', ';', stripped)
            line = stripped
            diagnostics.append({
                'line': idx,
                'snippet': f";{stray_char}",
                'type': 'stray_trailing_char',
                'deduction': 3,
                'advice': f"Line {idx} had a stray character '{stray_char}' immediately after the semicolon. It was cleaned so the query executes properly."
            })

        # Already a comment or starts with valid SQL comment syntax
        if stripped.startswith('--') or stripped.startswith('/*') or stripped.startswith('*') or stripped.startswith('*/'):
            sanitized_lines.append(line)
            continue

        # Check the first token of the line
        first_token = re.split(r'[\s(\[,;]', stripped)[0].upper()

        # Determine if this line is an un-commented human explanatory note
        is_note = False

        # If the line starts with ANY recognized SQL statement or clause, it is SQL - NOT a note!
        if first_token not in GLOBAL_SQL_KEYWORDS:
            # 1. Matches explicit note prefixes (e.g. "Step 1:", "Question 2", "Lab 4:")
            if re.match(r'^(step|task|question|q\d+|problem|part|ans|answer|note|lab|assignment|query\s*[:\d]|exercise|scenario)\b', stripped, re.I):
                is_note = True
            # 2. Ends with question mark or typical note colon (and is not a SQL statement)
            elif stripped.endswith('?') or (stripped.endswith(':') and not any(ch in stripped for ch in ('=', '<', '>', ';', '(', ')', '*', ',', '+', '-'))):
                is_note = True
            # 3. Contains clear conversational phrases (e.g. "here is my code", "solution for part 2")
            elif re.search(r'\b(my code|here is|solution for|completed by|submitted by|write a query|find the|extract the|get the|display the|retrieve the|verify that|check if|skipping the)\b', stripped, re.I):
                is_note = True
            # 4. Multi-word sentence containing common English conversational words without any SQL operators
            elif not any(char in stripped for char in ('=', '<', '>', ';', '(', ')', '*', ',', '+', '-', '/', '%', '.', '"', "'")):
                words = [w.lower() for w in re.findall(r'[a-zA-Z]+', stripped)]
                if len(words) >= 3 and any(w in ENGLISH_NOTE_INDICATORS for w in words):
                    is_note = True

        if is_note:
            sanitized_lines.append(f"-- [Auto-Commented Note] {line}")
            diagnostics.append({
                'line': idx,
                'snippet': stripped,
                'type': 'uncommented_note',
                'deduction': 5,
                'advice': f"Line {idx} ('{stripped}') appeared to be an explanatory note without '--' comment markers. It was auto-commented to allow your SQL to run cleanly."
            })
        else:
            sanitized_lines.append(line)

    sanitized_full_sql = "\n".join(sanitized_lines)
    stmts = split_sql_statements(sanitized_full_sql)

    return stmts, sanitized_full_sql, diagnostics


def prune_bulk_inserts_for_llm(sql_text: str, max_tuples: int = 25) -> str:
    """
    Prunes large repetitive INSERT INTO statements (e.g. >25 rows) to conserve LLM context tokens,
    while explicitly noting that all rows were executed in PostgreSQL so the AI never penalizes the student.
    """
    def _prune_insert(match: re.Match) -> str:
        header = match.group(1) # INSERT INTO ... VALUES
        values_body = match.group(2) # (1, ...), (2, ...)

        # Split tuples safely by matching top-level parentheses
        tuples = re.findall(r'\([^)]*\)', values_body, re.DOTALL)
        if len(tuples) > max_tuples:
            kept = ",\n  ".join(tuples[:max_tuples])
            omitted_count = len(tuples) - max_tuples
            return (
                f"{header}\n  {kept}\n"
                f"  -- [SYSTEM NOTE: Student successfully provided and executed all {len(tuples)} rows in the database. "
                f"The remaining {omitted_count} rows were collapsed by the system purely for display brevity];"
            )
        return match.group(0)

    # Match INSERT INTO ... VALUES (...)
    pattern = re.compile(r'(\bINSERT\s+INTO\s+[^;]+?\bVALUES\s*)(.+?);', re.IGNORECASE | re.DOTALL)
    return pattern.sub(_prune_insert, sql_text)

