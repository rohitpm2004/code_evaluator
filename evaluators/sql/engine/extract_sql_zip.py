#!/usr/bin/env python3
"""
Extracts and combines all .sql files from a ZIP archive.
Part of Central Evaluator - SQL Ingestion Layer
"""

import sys
import os
import zipfile

def main():
    if len(sys.argv) < 2:
        print("Usage: extract_sql_zip.py <archive.zip>", file=sys.stderr)
        sys.exit(1)

    zip_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(zip_path):
        print(f"File not found: {zip_path}", file=sys.stderr)
        sys.exit(1)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        # Find all .sql files, excluding macOS metadata files
        sql_members = [
            m for m in zf.infolist()
            if m.filename.lower().endswith('.sql') and not m.filename.startswith('__MACOSX') and not m.filename.startswith('.')
        ]

        if not sql_members:
            # Fallback to .txt if no .sql
            sql_members = [
                m for m in zf.infolist()
                if m.filename.lower().endswith('.txt') and not m.filename.startswith('__MACOSX') and not m.filename.startswith('.')
            ]

        if not sql_members:
            print("Error: No .sql or .txt query files found inside the submitted ZIP archive.", file=sys.stderr)
            sys.exit(2)

        # Sort: DDL (schema/create/table) -> DML (insert/seed/data) -> Queries (select/solution)
        def score(filename):
            fn = filename.lower()
            if any(k in fn for k in ['schema', 'create', 'table', 'ddl', 'init']):
                return 1
            if any(k in fn for k in ['seed', 'insert', 'data', 'dml']):
                return 2
            return 3

        sql_members.sort(key=lambda m: score(m.filename))

        output_parts = []
        for m in sql_members:
            text = zf.read(m).decode('utf-8', errors='ignore')
            output_parts.append(f"-- File: {m.filename}\n{text}")

        # Print combined content to stdout
        sys.stdout.write("\n\n".join(output_parts))

if __name__ == '__main__':
    main()
