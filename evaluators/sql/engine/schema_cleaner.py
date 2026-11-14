"""
schema_cleaner.py - Background & Startup Sweeper for Ephemeral Evaluation Schemas.
Detects and drops orphaned schemas starting with 'eval_' to prevent resource bloat
if an evaluation process was hard-killed or crashed before cleanup.
"""

import re
import time
import logging
from typing import Any

logger = logging.getLogger("schema_cleaner")


def sweep_orphaned_schemas(admin_conn: Any, max_age_seconds: int = 900) -> int:
    """
    Sweeps the database for any schemas matching 'eval_%' that were created
    more than `max_age_seconds` ago, and drops them CASCADE.

    Returns the number of dropped schemas.
    """
    dropped_count = 0
    try:
        with admin_conn.cursor() as cur:
            # Query pg_namespace for eval_ schemas
            cur.execute("""
                SELECT nspname
                FROM pg_namespace
                WHERE nspname LIKE 'eval_%'
                ORDER BY nspname ASC
                LIMIT 50;
            """)
            schemas = [row[0] for row in cur.fetchall()]

            now = time.time()
            for schema_name in schemas:
                created_ts = None

                # 1. Exact regex match for epoch timestamp embedded by sanitize_schema_name
                ts_match = re.search(r'eval_slot_\d+_(\d{10})_', schema_name)
                if ts_match:
                    try:
                        created_ts = float(ts_match.group(1))
                    except (ValueError, TypeError):
                        pass

                # 2. Fallback search across parts for legacy schemas
                if created_ts is None:
                    parts = schema_name.split('_')
                    for part in reversed(parts):
                        try:
                            val = float(part)
                            if val > 1_000_000_000_000:  # millisecond epoch
                                val /= 1000.0
                            if 1_500_000_000 <= val <= 2_500_000_000:
                                created_ts = val
                                break
                        except (ValueError, TypeError):
                            continue

                should_drop = False
                if created_ts is not None:
                    if (now - created_ts) > max_age_seconds:
                        should_drop = True
                else:
                    # Legacy schema without timestamp: drop only if no active activity references it
                    try:
                        cur.execute("""
                            SELECT COUNT(*) FROM pg_stat_activity
                            WHERE state = 'active'
                              AND (query LIKE %s OR query LIKE %s);
                        """, (f'%"{schema_name}"%', f'%{schema_name}%'))
                        res = cur.fetchone()
                        if res and res[0] == 0:
                            should_drop = True
                    except Exception:
                        pass

                if should_drop:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE;')
                    dropped_count += 1

        if dropped_count > 0:
            admin_conn.commit()
    except Exception as e:
        logger.warning(f"Error during schema sweeper: {e}")
        try:
            admin_conn.rollback()
        except Exception:
            pass

    return dropped_count


if __name__ == '__main__':
    from db_executor import get_db_connection_params, connect_postgres
    try:
        conn = connect_postgres(get_db_connection_params(), autocommit=True)
        count = sweep_orphaned_schemas(conn)
        print(f"Swept {count} orphaned schema(s).")
        conn.close()
    except Exception as e:
        print(f"Sweep notice: {e}")
