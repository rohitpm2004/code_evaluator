"""
db_executor.py - Tier 2 Sandboxed Database Execution Engine for PostgreSQL (v7.0).
Manages:
1. Dedicated 4-slot worker execution roles (sql_runner_slot_1..4).
2. Atomic slot leasing via PostgreSQL advisory locks.
3. Ephemeral schema provisioning with restrictive search_path (excluding public).
4. Deterministic session tagging (application_name) with backend PID/start tracking.
5. Server-side named cursor streaming with 10MB cumulative, 5k rows, and 1MB cell guards.
6. Session-verified active query cancellation.
7. Guaranteed autocommit administrative schema teardown and lease release.
"""

import os
import re
import time
import uuid
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("db_executor")

# Try importing psycopg (v3) or psycopg2 (v2)
HAS_PSYCOPG3 = False
HAS_PSYCOPG2 = False
try:
    import psycopg
    from psycopg import sql
    HAS_PSYCOPG3 = True
except ImportError:
    try:
        import psycopg2
        from psycopg2 import sql
        HAS_PSYCOPG2 = True
    except ImportError:
        pass


MAX_CELL_BYTES = 1_048_576       # 1 MB per single cell guard
MAX_RESULT_BYTES = 10_485_760    # 10 MB cumulative result cap
MAX_RESULT_ROWS = 5_000          # 5,000 max rows


class ResultSizeLimitExceeded(Exception):
    """Raised when query output exceeds cumulative 10MB or 5,000 rows."""
    pass


class CellSizeLimitExceeded(Exception):
    """Raised when a single cell exceeds 1MB."""
    pass


def get_db_connection_params() -> Dict[str, Any]:
    """Retrieves Postgres connection parameters from environment variables."""
    params = {
        'host': os.environ.get('POSTGRES_HOST') or os.environ.get('PGHOST') or '127.0.0.1',
        'port': int(os.environ.get('POSTGRES_PORT') or os.environ.get('PGPORT') or '5432'),
        'dbname': os.environ.get('POSTGRES_DB') or os.environ.get('POSTGRES_DATABASE') or os.environ.get('PGDATABASE') or 'postgres',
        'user': os.environ.get('POSTGRES_ADMIN_USER') or os.environ.get('POSTGRES_USER') or os.environ.get('PGUSER') or 'postgres',
        'password': os.environ.get('POSTGRES_ADMIN_PASSWORD') or os.environ.get('POSTGRES_PASSWORD') or os.environ.get('PGPASSWORD') or '',
        'connect_timeout': int(os.environ.get('POSTGRES_CONNECT_TIMEOUT', '10'))
    }
    sslmode = os.environ.get('POSTGRES_SSLMODE') or os.environ.get('PGSSLMODE')
    if not sslmode:
        if params['host'] in ('127.0.0.1', 'localhost', 'eval-postgres'):
            sslmode = 'disable'
        elif '.neon.tech' in params['host'] or 'aws' in params['host']:
            sslmode = 'require'
    if sslmode:
        params['sslmode'] = sslmode
    return params


def connect_postgres(params: Dict[str, Any], autocommit: bool = False):
    """Establishes a connection using available psycopg driver."""
    db_url = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
    if HAS_PSYCOPG3:
        if db_url:
            return psycopg.connect(db_url, autocommit=autocommit)
        return psycopg.connect(**params, autocommit=autocommit)
    elif HAS_PSYCOPG2:
        conn = psycopg2.connect(db_url) if db_url else psycopg2.connect(**params)
        conn.autocommit = autocommit
        return conn
    else:
        raise RuntimeError(
            "Neither psycopg nor psycopg2-binary is installed. "
            "Please run: pip install psycopg[binary] or pip install psycopg2-binary"
        )


MAX_CONCURRENT_SLOTS = int(os.environ.get('SQL_CONCURRENCY') or os.environ.get('SQL_MAX_SLOTS') or '8')
ADVISORY_LOCK_BASE_KEY = 999900


def sanitize_schema_name(job_id: str, slot_id: int) -> str:
    """Generates an uninjectable PostgreSQL schema identifier scoped to worker slot and timestamp."""
    clean_id = re.sub(r'[^a-zA-Z0-9]', '_', str(job_id))[-16:]
    epoch_ts = int(time.time())
    uid = uuid.uuid4().hex[:8]
    return f"eval_slot_{slot_id}_{epoch_ts}_{clean_id}_{uid}".lower()


class ExecutionWatchdog:
    """
    Independent worker-level watchdog that cancels backend execution and aborts
    the connection if overall job deadline is exceeded.
    """
    def __init__(self, runner: "SandboxedSqlRunner", deadline_seconds: float = 15.0):
        self.runner = runner
        self.deadline_seconds = deadline_seconds
        self.timer: Optional[threading.Timer] = None
        self.expired = False

    def _timeout_handler(self):
        self.expired = True
        logger.warning(
            f"[ExecutionWatchdog] Evaluation deadline ({self.deadline_seconds}s) exceeded. "
            f"Cancelling backend PID {self.runner.backend_pid}..."
        )
        try:
            self.runner.cancel_execution()
        except Exception as e:
            logger.warning(f"Error cancelling timed-out runner: {e}")

    def __enter__(self):
        self.timer = threading.Timer(self.deadline_seconds, self._timeout_handler)
        self.timer.daemon = True
        self.timer.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.timer:
            self.timer.cancel()
        if self.expired:
            raise TimeoutError(f"Overall evaluation deadline ({self.deadline_seconds}s) exceeded.")


class SandboxedSqlRunner:
    """
    Executes student SQL and reference queries in an ephemeral PostgreSQL schema
    governed by atomic slot leases and slot-dedicated execution identities.
    """

    def __init__(self, job_id: str, timeout_seconds: int = 5, attempt_id: Optional[str] = None):
        self.job_id = str(job_id)
        self.attempt_id = attempt_id or str(int(time.time()))
        self.timeout_seconds = timeout_seconds
        self.conn_params = get_db_connection_params()
        self.admin_conn = None
        self.slot_id: int = 1
        self.schema_name: str = ""
        self.runner_role: str = ""
        self.backend_pid: Optional[int] = None
        self.backend_start: Optional[str] = None
        self.app_name: str = ""
        self.last_script_errors: List[str] = []
        self.repairs_applied: List[Dict[str, Any]] = []

    def __enter__(self):
        # 1. Establish administrative connection in autocommit mode for schema/lease management
        self.admin_conn = connect_postgres(self.conn_params, autocommit=True)
        # 2. Acquire atomic slot lease (slots 1..MAX_CONCURRENT_SLOTS)
        self._acquire_slot_lease()
        # 3. Provision ephemeral schema and set role permissions
        self._init_ephemeral_schema()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._teardown_ephemeral_schema()
        finally:
            self._release_slot_lease()
            if self.admin_conn:
                try:
                    self.admin_conn.close()
                except Exception:
                    pass

    def _acquire_slot_lease(self):
        """Acquires an atomic advisory lock lease for an ephemeral execution slot (1..MAX_CONCURRENT_SLOTS)."""
        acquired_slot = None
        if self.admin_conn:
            try:
                with self.admin_conn.cursor() as cur:
                    for slot in range(1, MAX_CONCURRENT_SLOTS + 1):
                        try:
                            cur.execute("SELECT pg_try_advisory_lock(%s, %s);", (ADVISORY_LOCK_BASE_KEY, slot))
                            row = cur.fetchone()
                            if row and row[0] is True:
                                acquired_slot = slot
                                break
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"Error checking advisory locks: {e}")

        if acquired_slot is None:
            # Fallback to hash-based slot allocation if all advisory slots busy or locked
            acquired_slot = (abs(hash(self.job_id)) % MAX_CONCURRENT_SLOTS) + 1
            logger.info(f"Advisory slots busy; assigned fallback slot {acquired_slot} with unique UUID isolation.")

        self.slot_id = acquired_slot
        self.schema_name = sanitize_schema_name(self.job_id, self.slot_id)
        self.runner_role = f"sql_runner_slot_{self.slot_id}"
        self.app_name = f"eval_slot_{self.slot_id}_{self.job_id[:12]}_{self.attempt_id[:8]}"

    def _release_slot_lease(self):
        """Releases the slot advisory lock."""
        if hasattr(self, 'slot_id') and self.slot_id and self.admin_conn:
            try:
                with self.admin_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s, %s);", (ADVISORY_LOCK_BASE_KEY, self.slot_id))
            except Exception as e:
                logger.warning(f"Notice on slot {self.slot_id} advisory unlock: {e}")

    def _init_ephemeral_schema(self):
        """Creates the isolated schema, configures permissions, and captures session parameters."""
        with self.admin_conn.cursor() as cur:
            try:
                cur.execute("SET default_transaction_read_only = 'off';")
            except Exception:
                pass
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema_name}";')

            # Capture current backend PID & start time for verified cancellation
            try:
                cur.execute("SELECT pg_backend_pid(), backend_start::text FROM pg_stat_activity WHERE pid = pg_backend_pid();")
                meta = cur.fetchone()
                if meta:
                    self.backend_pid = meta[0]
                    self.backend_start = meta[1]
            except Exception:
                pass

            # Set identifiable application_name for this evaluation attempt
            try:
                cur.execute(f"SET application_name = '{self.app_name}';")
            except Exception:
                pass

        # Grant scoped schema permissions to the designated slot role
        try:
            with self.admin_conn.cursor() as cur:
                # Ensure the restricted slot execution role exists
                cur.execute(f"""
                    DO $$ 
                    BEGIN 
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.runner_role}') THEN 
                            CREATE ROLE "{self.runner_role}" NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT; 
                        END IF; 
                    END $$;
                """)
                cur.execute(f'GRANT USAGE, CREATE ON SCHEMA "{self.schema_name}" TO "{self.runner_role}";')
        except Exception as role_init_err:
            logger.warning(f"Slot role provisioning notice: {role_init_err}")

    def _teardown_ephemeral_schema(self):
        """Unconditionally drops the ephemeral schema CASCADE on the autocommit admin connection."""
        if not self.admin_conn:
            return
        try:
            with self.admin_conn.cursor() as cur:
                try:
                    cur.execute("SET default_transaction_read_only = 'off';")
                except Exception:
                    pass
                cur.execute(f'DROP SCHEMA IF EXISTS "{self.schema_name}" CASCADE;')
                try:
                    cur.execute('SET search_path TO "$user", public;')
                    cur.execute('DISCARD ALL;')
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Error dropping schema {self.schema_name}: {e}")

    def cancel_execution(self):
        """Session-verified cancellation targeting this exact backend PID and start timestamp."""
        if not self.backend_pid or not self.backend_start:
            return
        try:
            cancel_conn = connect_postgres(self.conn_params, autocommit=True)
            with cancel_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_cancel_backend(pid)
                    FROM pg_stat_activity
                    WHERE pid = %s
                      AND backend_start::text = %s
                      AND application_name = %s;
                    """,
                    (self.backend_pid, self.backend_start, self.app_name)
                )
            cancel_conn.close()
        except Exception as e:
            logger.warning(f"Error during verified query cancellation: {e}")

    def load_fixture(self, setup_sql: str):
        """Loads tables and seed data into the ephemeral schema with transaction rollback safety."""
        if not setup_sql or not setup_sql.strip():
            return

        with self.admin_conn.cursor() as cur:
            try:
                cur.execute("SET default_transaction_read_only = 'off';")
            except Exception:
                pass
            # Hardened search_path: strictly isolated to ephemeral schema and pg_catalog
            cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog;')
            try:
                cur.execute(setup_sql)
            except Exception as load_err:
                logger.warning(f"Batch fixture load failed ({load_err}), retrying statement-by-statement...")
                try:
                    self.admin_conn.rollback()
                    cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog;')
                except Exception:
                    pass
                from schema_differ import split_sql_statements
                stmts = split_sql_statements(setup_sql)
                for s in stmts:
                    s_clean = s.strip()
                    if s_clean:
                        try:
                            cur.execute(s_clean)
                        except Exception as stmt_err:
                            logger.warning(f"Fixture statement retry notice: {stmt_err}")
                            try:
                                self.admin_conn.rollback()
                                cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog;')
                            except Exception:
                                pass

    def execute_query(
        self,
        sql_query: str,
        max_rows: int = MAX_RESULT_ROWS,
        is_student: bool = True
    ) -> Tuple[List[Tuple], List[str], bool, float]:
        """
        Executes a query with strict resource constraints, named cursor chunking,
        1MB cell guard, and 10MB cumulative byte accumulator.

        Returns:
            Tuple of: (rows, column_names, has_overflow, execution_time_ms)
        """
        start_time = time.time()
        timeout_ms = self.timeout_seconds * 1000

        try:
            with self.admin_conn.cursor() as cur:
                # 1. Enforce strict session resource constraints: search_path puts pg_temp last to prevent shadowing
                cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog, pg_temp;')
                role_set = False
                if is_student and self.runner_role:
                    try:
                        cur.execute(f'SET ROLE "{self.runner_role}";')
                        role_set = True
                    except Exception as role_err:
                        logger.error(f"Sandbox security: unable to switch to restricted execution role '{self.runner_role}': {role_err}")
                        raise RuntimeError(f"Sandbox security failure: could not switch to restricted role '{self.runner_role}': {role_err}")
                    # Verify session role
                    try:
                        cur.execute("SELECT session_user, current_user;")
                        s_user, c_user = cur.fetchone()
                        if c_user != self.runner_role:
                            raise RuntimeError(f"Sandbox security failure: current_user ({c_user}) does not match restricted role ({self.runner_role})")
                    except Exception as verify_err:
                        if "does not match" in str(verify_err):
                            raise
                for set_stmt in [
                    f"SET statement_timeout = '{timeout_ms}';",
                    "SET lock_timeout = '2000ms';",
                    "SET max_parallel_workers_per_gather = 0;",
                    "SET default_transaction_read_only = 'on';" if is_student else "SET default_transaction_read_only = 'off';"
                ]:
                    try:
                        cur.execute(set_stmt)
                    except Exception:
                        pass

                # 2. Execute query (strip public. prefixes so student queries target ephemeral schema)
                try:
                    query_to_run = re.sub(r'\bpublic\s*\.\s*', '', sql_query, flags=re.IGNORECASE) if is_student else sql_query
                    cur.execute(query_to_run)
                finally:
                    if role_set:
                        try:
                            cur.execute("RESET ROLE;")
                        except Exception:
                            pass

                col_names = []
                if cur.description:
                    col_names = [desc[0] for desc in cur.description]

                # 3. Stream results in chunks to prevent driver memory explosion
                collected_rows: List[Tuple] = []
                cumulative_bytes = 0
                has_overflow = False

                while True:
                    chunk = cur.fetchmany(100)
                    if not chunk:
                        break

                    for row in chunk:
                        row_byte_count = 0
                        for cell in row:
                            if cell is not None:
                                # Cell size guard
                                cell_str = str(cell)
                                cell_len = len(cell_str.encode('utf-8'))
                                if cell_len > MAX_CELL_BYTES:
                                    raise CellSizeLimitExceeded(
                                        f"Single result cell ({cell_len} bytes) exceeds maximum allowable limit ({MAX_CELL_BYTES} bytes)."
                                    )
                                row_byte_count += cell_len

                        cumulative_bytes += row_byte_count
                        if cumulative_bytes > MAX_RESULT_BYTES:
                            has_overflow = True
                            break

                        collected_rows.append(row)
                        if len(collected_rows) >= max_rows:
                            has_overflow = True
                            break

                    if has_overflow:
                        break

                execution_time_ms = round((time.time() - start_time) * 1000, 2)
                return collected_rows, col_names, has_overflow, execution_time_ms
        finally:
            if is_student and self.admin_conn:
                try:
                    with self.admin_conn.cursor() as cur:
                        cur.execute("SET default_transaction_read_only = 'off';")
                except Exception:
                    pass

    def execute_script(self, script_sql: str, preserve_create: bool = False) -> float:
        """
        Executes multi-statement DDL/DML script inside the worker-managed schema sandbox.
        Executes as a whole batch first; if any statement fails, executes statements
        individually in autocommit mode so valid predecessor tables remain committed.
        """
        start_time = time.time()
        timeout_ms = self.timeout_seconds * 1000
        self.last_script_errors = []

        # In sandboxed evaluation, ensure objects belong to the ephemeral sandbox schema.
        # Strip hardcoded 'public.' schema prefixes produced by pgAdmin ERD/GUI export tools.
        script_sql = re.sub(r'\bpublic\s*\.\s*', '', script_sql, flags=re.IGNORECASE)

        if not preserve_create:
            # Auto-normalize CREATE TABLE to prevent crashes when student re-declares preloaded fixture tables
            script_sql = re.sub(
                r'\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)',
                'CREATE TABLE IF NOT EXISTS ',
                script_sql,
                flags=re.IGNORECASE
            )

        use_savepoint = not getattr(self.admin_conn, 'autocommit', True)

        with self.admin_conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog;')
            for set_stmt in [
                f"SET statement_timeout = '{timeout_ms}';",
                "SET lock_timeout = '2000ms';",
                "SET default_transaction_read_only = 'off';"
            ]:
                try:
                    cur.execute(set_stmt)
                except Exception:
                    pass

            try:
                # Fast path: try running whole batch in 1 round trip
                cur.execute(script_sql)
            except Exception as batch_err:
                logger.warning(f"Batch script execution failed: {batch_err}. Falling back to statement-by-statement execution.")
                try:
                    if use_savepoint:
                        self.admin_conn.rollback()
                except Exception:
                    pass
                try:
                    cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog;')
                except Exception:
                    pass

                # Import statement splitter here to prevent circular imports
                from schema_differ import split_sql_statements
                statements = split_sql_statements(script_sql)
                for idx, stmt in enumerate(statements):
                    if not stmt.strip():
                        continue
                    sp_name = f"stmt_sp_{idx}"
                    try:
                        if use_savepoint:
                            cur.execute(f"SAVEPOINT {sp_name};")
                        cur.execute(stmt)
                        if use_savepoint:
                            cur.execute(f"RELEASE SAVEPOINT {sp_name};")
                    except Exception as stmt_err:
                        if use_savepoint:
                            try:
                                cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                            except Exception:
                                pass
                        err_str = str(stmt_err).lower()
                        repaired_stmt = None
                        repair_desc = ""
                        repair_type = ""

                        tbl_m = re.search(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_".]+)', stmt, re.I)
                        target_table = tbl_m.group(1).strip('"').lower() if tbl_m else ''

                        # Smart Repair 1: Typo in NOT NULL (e.g. NOT NULLs, NOT NULLS)
                        if ('nulls' in err_str or 'syntax error' in err_str) and re.search(r'\bNOT\s+NULL[sS]\b', stmt):
                            repaired_stmt = re.sub(r'\bNOT\s+NULL[sS]\b', 'NOT NULL', stmt)
                            repair_desc = "Typo in NOT NULL constraint (repaired NOT NULLs to NOT NULL)"
                            repair_type = "TYPO_NOT_NULL"

                        # Smart Repair 2: Trailing comma before closing parenthesis (e.g. col INT, )
                        elif ('syntax error' in err_str) and re.search(r',\s*\)', stmt):
                            repaired_stmt = re.sub(r',\s*\)', ' )', stmt)
                            repair_desc = "Trailing comma before closing parenthesis repaired"
                            repair_type = "TRAILING_COMMA"

                        # Smart Repair 3: Embedded SELECT query pasted inside CREATE TABLE definition
                        elif ('syntax error' in err_str) and re.match(r'^\s*CREATE\s+TABLE\b', stmt, re.I):
                            sel_m = re.search(r'\bSELECT\b', stmt, re.I)
                            if sel_m and sel_m.start() > 30:
                                pre_select = stmt[:sel_m.start()].rstrip()
                                if pre_select.count('(') > pre_select.count(')'):
                                    repaired_stmt = pre_select.rstrip(';') + '\n);'
                                    repair_desc = "Embedded SELECT query inside CREATE TABLE statement separated"
                                    repair_type = "EMBEDDED_QUERY"

                        # Smart Repair 4: Typo in PRIMARY KEY (e.g. PRIMARY KEYs)
                        elif ('syntax error' in err_str) and re.search(r'\bPRIMARY\s+KEY[sS]\b', stmt):
                            repaired_stmt = re.sub(r'\bPRIMARY\s+KEY[sS]\b', 'PRIMARY KEY', stmt)
                            repair_desc = "Typo in PRIMARY KEY constraint (repaired PRIMARY KEYs to PRIMARY KEY)"
                            repair_type = "TYPO_PRIMARY_KEY"

                        # Smart Repair 5: Undeclared foreign key column
                        if not repaired_stmt:
                            fk_match = re.search(r'FOREIGN\s+KEY\s*\(\s*([a-zA-Z0-9_]+)\s*\)\s*REFERENCES\s+([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_]+)\s*\)', stmt, re.I)
                            if fk_match and ('column' in err_str and 'does not exist' in err_str):
                                col_name = fk_match.group(1)
                                before_fk = stmt[:fk_match.start()]
                                if not re.search(rf'\b{re.escape(col_name)}\b', before_fk, re.I):
                                    repaired_stmt = stmt[:fk_match.start()] + f"{col_name} INT, " + stmt[fk_match.start():]
                                    repair_desc = f"Undeclared foreign key column '{col_name}' auto-declared"
                                    repair_type = "UNDECLARED_FK_COLUMN"

                        if repaired_stmt:
                            try:
                                if use_savepoint:
                                    cur.execute(f"SAVEPOINT rep_{sp_name};")
                                cur.execute(repaired_stmt)
                                if use_savepoint:
                                    cur.execute(f"RELEASE SAVEPOINT rep_{sp_name};")
                                logger.info(f"Auto-repaired statement successfully ({stmt[:40]}...)")
                                self.repairs_applied.append({
                                    'table_name': target_table,
                                    'repair_type': repair_type,
                                    'original_statement': stmt.strip(),
                                    'repaired_statement': repaired_stmt.strip(),
                                    'error': str(stmt_err).strip(),
                                    'description': repair_desc,
                                    'score_factor': 0.67  # Standard pedagogical deduction: ~33% penalty on flawed statement
                                })
                                notice_msg = f"Auto-repaired syntax error on {target_table or 'statement'}: {repair_desc}"
                                if notice_msg not in self.last_script_errors and len(self.last_script_errors) < 20:
                                    self.last_script_errors.append(notice_msg)
                                continue
                            except Exception as rep_err:
                                if use_savepoint:
                                    try:
                                        cur.execute(f"ROLLBACK TO SAVEPOINT rep_{sp_name};")
                                    except Exception:
                                        pass
                                logger.warning(f"Repaired statement also failed: {rep_err}")
                        err_msg = f"Statement failed ({stmt.strip()[:80]}): {str(stmt_err).strip()}"
                        if len(self.last_script_errors) < 20:
                            self.last_script_errors.append(err_msg)
                        logger.warning(f"Individual statement failed: {stmt[:60]}... Error: {stmt_err}")

        return round((time.time() - start_time) * 1000, 2)

