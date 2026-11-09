#!/usr/bin/env python3
"""Print a schema fingerprint for a kanban board DB — strictly read-only.

Used to prove that a change did not touch the board schema (WS1 / AC8):
run it against a copy of the production DB and against a DB created by the
base commit; the two fingerprints must be byte-identical.

The fingerprint covers:

* ``PRAGMA user_version``
* the sorted ``sqlite_master`` table list
* every table's column definition (name, type, notnull, default, pk)
* every index's SQL
* a sha256 over the canonical rendering of all of the above

The DB is opened with ``mode=ro`` so this can never modify production data.

Usage:
    python scripts/kanban_schema_fingerprint.py /tmp/kb_copy.db
    python scripts/kanban_schema_fingerprint.py /tmp/kb_copy.db --json
    python scripts/kanban_schema_fingerprint.py --default   # current board DB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def fingerprint(db_path: Path) -> dict:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        columns: dict[str, list[dict]] = {}
        for table in tables:
            if table.startswith("sqlite_"):
                continue
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns[table] = [
                {
                    "name": r["name"],
                    "type": (r["type"] or "").upper(),
                    "notnull": int(r["notnull"]),
                    "default": r["dflt_value"],
                    "pk": int(r["pk"]),
                }
                for r in rows
            ]
        indexes = [
            r["sql"] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
                "ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()

    canonical = json.dumps(
        {"user_version": user_version, "tables": tables, "columns": columns},
        sort_keys=True, separators=(",", ":"),
    )
    return {
        "db": str(db_path),
        "user_version": user_version,
        "tables": tables,
        "columns": columns,
        "index_count": len(indexes),
        "schema_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", nargs="?", help="Path to a kanban DB (opened read-only)")
    parser.add_argument("--default", action="store_true",
                        help="Fingerprint the current board DB instead of a path")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    if args.default:
        from hermes_cli import kanban_db as kb

        db_path = kb.kanban_db_path()
    elif args.db:
        db_path = Path(args.db).expanduser()
    else:
        parser.error("pass a DB path or --default")

    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    fp = fingerprint(db_path)
    if args.json:
        print(json.dumps(fp, indent=2, sort_keys=True))
        return 0

    print(f"db:           {fp['db']}")
    print(f"user_version: {fp['user_version']}")
    print(f"tables:       {', '.join(fp['tables'])}")
    print(f"indexes:      {fp['index_count']}")
    for table, cols in fp["columns"].items():
        rendered = ", ".join(f"{c['name']}:{c['type']}" for c in cols)
        print(f"  {table}({rendered})")
    print(f"schema_sha256: {fp['schema_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
