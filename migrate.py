"""Simple migration runner for SQLite.

Usage:
    python migrate.py              # apply all pending migrations
    python migrate.py --status     # show applied / pending migrations
    python migrate.py --rollback   # undo the last migration
"""

import os
import sys
import sqlite3
import importlib.util

from config import DATABASE

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def _load_module(path):
    spec = importlib.util.spec_from_file_location("_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ensure_migrations_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL UNIQUE,
            applied_at TEXT    NOT NULL
        )
        """
    )
    conn.commit()


def _applied_names(conn):
    rows = conn.execute("SELECT name FROM migrations ORDER BY id").fetchall()
    return {r[0] for r in rows}


def _migration_files():
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    files = sorted(
        f for f in os.listdir(MIGRATIONS_DIR)
        if f.endswith(".py") and not f.startswith("_")
    )
    return files


def _record(conn, name):
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO migrations (name, applied_at) VALUES (?, ?)",
        (name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _remove_record(conn, name):
    conn.execute("DELETE FROM migrations WHERE name = ?", (name,))
    conn.commit()


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def status(path=None):
    conn = sqlite3.connect(path or DATABASE)
    _ensure_migrations_table(conn)

    applied = _applied_names(conn)
    files = _migration_files()

    print(f"{'Migration':<45} {'Status'}")
    print("-" * 55)

    for f in files:
        st = "applied" if f in applied else "pending"
        print(f"{f:<45} {st}")

    conn.close()


def migrate(path=None):
    conn = sqlite3.connect(path or DATABASE)
    _ensure_migrations_table(conn)

    # Включаем WAL-режим для параллельного доступа (Docker + DBeaver)
    conn.execute("PRAGMA journal_mode=WAL;")

    applied = _applied_names(conn)
    files = _migration_files()

    pending = [f for f in files if f not in applied]

    if not pending:
        print("Nothing to migrate.")
        conn.close()
        return

    for f in pending:
        mod_path = os.path.join(MIGRATIONS_DIR, f)
        mod = _load_module(mod_path)

        print(f"Applying {f} ...")
        mod.up(conn)
        _record(conn, f)
        print(f"  done.")

    conn.close()


def rollback(path=None):
    conn = sqlite3.connect(path or DATABASE)
    _ensure_migrations_table(conn)

    applied = _applied_names(conn)
    if not applied:
        print("Nothing to rollback.")
        conn.close()
        return

    last = sorted(applied)[-1]
    mod_path = os.path.join(MIGRATIONS_DIR, last)
    mod = _load_module(mod_path)

    print(f"Rolling back {last} ...")
    mod.down(conn)
    _remove_record(conn, last)
    print("  done.")

    conn.close()


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    elif "--rollback" in sys.argv:
        rollback()
    else:
        migrate()
