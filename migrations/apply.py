#!/usr/bin/env python3
"""Apply paas/migrations/*.sql in lexicographic order, forward-only.

- Tracks applied files in schema_migrations on the default database, so re-runs
  are no-ops (SPEC-03 AC-1). A checksum change on an already-applied file is a
  loud warning: forward-only migrations are never edited after the fact.
- A migration may declare its target database on its first line:

      -- target-db: autopilot_audit

  (default target is the database in DATABASE_URL - SPEC-03 §3 splits
  audit_log into its own database).

- DSN comes from DATABASE_URL and must authenticate as autopilot_migrator:
  the only role that may run DDL (SPEC-03 R-2). It has BYPASSRLS because FORCE
  row-level security applies to the table owner too.

Usage:
    python paas/migrations/apply.py              # apply everything pending
    python paas/migrations/apply.py --check      # exit 1 if anything is pending
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).parent

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

TARGET_DB_RE = re.compile(r"^\s*--\s*target-db:\s*([A-Za-z0-9_]+)\s*$")


def declared_target_db(text: str) -> str | None:
    if text:
        first = text.splitlines()[0]
        m = TARGET_DB_RE.match(first)
        if m:
            return m.group(1)
    return None


def dsn_for(target_db: str | None) -> str:
    base = os.environ.get("DATABASE_URL", "")
    if not base:
        raise SystemExit("DATABASE_URL is not set (migrator DSN required)")
    from psycopg import conninfo

    kwargs = {}
    if target_db:
        kwargs["dbname"] = target_db
    return conninfo.make_conninfo(**conninfo.conninfo_to_dict(base, **kwargs))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if pending migrations exist")
    args = parser.parse_args()

    files = sorted(HERE.glob("*.sql"))
    if not files:
        print("no migration files found")
        return 0

    with psycopg.connect(dsn_for(None)) as conn:
        conn.execute(TRACKING_DDL)
        conn.commit()
        applied = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, checksum FROM schema_migrations"
            ).fetchall()
        }

    pending: list[Path] = []
    for f in files:
        if f.name not in applied:
            pending.append(f)

    failed = False
    for f in files:
        checksum = hashlib.sha256(f.read_bytes()).hexdigest()
        known = applied.get(f.name)
        if known is not None and known != checksum:
            print(f"WARNING: {f.name} changed after being applied "
                  "(forward-only migrations must never be edited)")
            failed = True
            continue

    if args.check:
        for f in pending:
            print(f"pending: {f.name}")
        return 1 if pending else 0

    with psycopg.connect(dsn_for(None)) as bookkeeper:
        for f in files:
            if f.name in applied:
                continue
            checksum = hashlib.sha256(f.read_bytes()).hexdigest()
            text = f.read_text(encoding="utf-8")
            target = declared_target_db(text)
            dsn = dsn_for(target)

            if target is None:
                # default target: the file and its tracking row share one tx,
                # so a migration can never apply without being recorded.
                with bookkeeper.transaction():
                    bookkeeper.execute(text)
                    bookkeeper.execute(
                        "INSERT INTO schema_migrations (id, checksum) VALUES (%s, %s)",
                        (f.name, checksum),
                    )
            else:
                # cross-database migration (SPEC-03 §3): applied in its own tx
                # on the target db; the tracking row is always written to the
                # default database right after.
                with psycopg.connect(dsn) as conn:
                    with conn.transaction():
                        conn.execute(text)
                with bookkeeper.transaction():
                    bookkeeper.execute(
                        "INSERT INTO schema_migrations (id, checksum) VALUES (%s, %s)",
                        (f.name, checksum),
                    )

            print(f"applied: {f.name} -> {target or 'default'}")

    if failed:
        print("\nchecksum drift detected - fix before proceeding", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
