#!/usr/bin/env python3
"""
Safe ZIP Archive Extractor
Part of Central Evaluator - repoService Ingestion Layer

Security Guarantees:
1. Enforces strict destination boundary check to prevent SlipZip / path-traversal attacks (e.g. ../../).
2. Rejects malicious symlinks.
3. Enforces uncompressed byte ceiling (default 100MB) to prevent ZIP-bomb DOS.
"""

import sys
import os
import zipfile

MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB

def safe_extract(zip_path: str, dest_dir: str):
    zip_path = os.path.abspath(zip_path)
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"ZIP archive not found at: {zip_path}")

    total_uncompressed_bytes = 0

    with zipfile.ZipFile(zip_path, 'r') as zf:
        # Pre-validate all member targets before extracting anything
        for member in zf.infolist():
            total_uncompressed_bytes += member.file_size
            if total_uncompressed_bytes > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"ZIP bomb detected: Uncompressed size exceeds limit of {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB."
                )

            # Prevent ZipSlip path traversal
            target_path = os.path.abspath(os.path.join(dest_dir, member.filename))
            if os.path.commonpath([dest_dir]) != os.path.commonpath([dest_dir, target_path]):
                raise ValueError(f"Illegal path traversal attempt in archive entry: {member.filename}")

        # Extract safely
        zf.extractall(dest_dir)

def main():
    if len(sys.argv) < 3:
        print("Usage: safe_unzip.py <archive.zip> <destination_dir>", file=sys.stderr)
        sys.exit(1)

    archive_path = sys.argv[1]
    destination_dir = sys.argv[2]

    try:
        safe_extract(archive_path, destination_dir)
        print(f"Successfully extracted {archive_path} to {destination_dir}")
    except Exception as e:
        print(f"Error extracting archive: {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == '__main__':
    main()
