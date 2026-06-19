#!/usr/bin/env python3
"""One-time SQLite → Postgres table copy for early production cutover.

Requires: pip install psycopg[binary]

Usage:
  export DATABASE_URL=postgresql://user:pass@localhost:5432/lab_scheduler
  python scripts/migrate_sqlite_to_postgres.py --sqlite demo.sqlite3
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

TABLES_IN_ORDER = (
    "tenants",
    "tenant_user_accounts",
    "tenant_configurations",
    "employees",
    "qualifications",
    "employee_qualifications",
    "shift_templates",
    "shift_template_qualifications",
    "schedule_periods",
    "shift_assignments",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy core scheduler tables SQLite → Postgres")
    parser.add_argument("--sqlite", required=True, help="Path to source SQLite database")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("Set DATABASE_URL to a Postgres connection string.")

    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Install psycopg: pip install 'psycopg[binary]'") from exc

    src = sqlite3.connect(args.sqlite)
    src.row_factory = sqlite3.Row
    dst = psycopg.connect(database_url)

    with dst.cursor() as cur, src:
        for table in TABLES_IN_ORDER:
            try:
                rows = src.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            if not rows:
                continue
            columns = rows[0].keys()
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(columns)
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            for row in rows:
                cur.execute(sql, tuple(row[col] for col in columns))
        dst.commit()
    dst.close()
    print(f"Migrated {len(TABLES_IN_ORDER)} table groups from {Path(args.sqlite).resolve()}")


if __name__ == "__main__":
    main()
