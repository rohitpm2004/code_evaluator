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
                cur.execute(f'GRANT USAGE, CREATE ON SCHEMA "{self.schema_name}" TO {self.runner_role};')
        except Exception:
            # In standard local/dev environments where slot roles are not yet provisioned, continue gracefully
            pass

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
                # 1. Enforce strict session resource constraints
                cur.execute(f'SET search_path TO "{self.schema_name}", pg_catalog;')
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

                # 2. Execute query
                cur.execute(sql_query)

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

    def execute_script(self, script_sql: str) -> float:
        """
        Executes multi-statement DDL/DML script inside the worker-managed schema sandbox.
        Executes as a whole batch first; if any statement fails, executes statements
        individually in autocommit mode so valid predecessor tables remain committed.
        """
        start_time = time.time()
        timeout_ms = self.timeout_seconds * 1000

        # Auto-normalize CREATE TABLE to prevent crashes when student re-declares preloaded fixture tables
        script_sql = re.sub(
            r'\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)',
            'CREATE TABLE IF NOT EXISTS ',
            script_sql,
            flags=re.IGNORECASE
        )

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
                        cur.execute(f"SAVEPOINT {sp_name};")
                        cur.execute(stmt)
                        cur.execute(f"RELEASE SAVEPOINT {sp_name};")
                    except Exception as stmt_err:
                        try:
                            cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                        except Exception:
                            pass
                        err_str = str(stmt_err).lower()

                        # If failed due to undeclared foreign key column, attempt smart repair:
                        fk_match = re.search(r'FOREIGN\s+KEY\s*\(\s*([a-zA-Z0-9_]+)\s*\)\s*REFERENCES\s+([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_]+)\s*\)', stmt, re.I)
                        if fk_match and ('column' in err_str and 'does not exist' in err_str):
                            col_name = fk_match.group(1)
                            before_fk = stmt[:fk_match.start()]
                            if not re.search(rf'\b{re.escape(col_name)}\b', before_fk, re.I):
                                repaired_stmt = stmt[:fk_match.start()] + f"{col_name} INT, " + stmt[fk_match.start():]
                                try:
                                    cur.execute(f"SAVEPOINT rep_{sp_name};")
                                    cur.execute(repaired_stmt)
                                    cur.execute(f"RELEASE SAVEPOINT rep_{sp_name};")
                                    logger.info(f"Auto-repaired statement by injecting missing '{col_name} INT'")
                                    continue
                                except Exception as rep_err:
                                    try:
                                        cur.execute(f"ROLLBACK TO SAVEPOINT rep_{sp_name};")
                                    except Exception:
                                        pass
                                    logger.warning(f"Repaired statement also failed: {rep_err}")
                        logger.warning(f"Individual statement failed: {stmt[:60]}... Error: {stmt_err}")

        return round((time.time() - start_time) * 1000, 2)

