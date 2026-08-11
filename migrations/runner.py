"""Simple migration runner for SQLite.

Usage:
    python -m migrations.runner         # apply all pending migrations
    python -m migrations.runner --status    # show applied / pending migrations
    python -m migrations.runner --rollback  # undo the last migration

The runner is safe to call at application startup (bot + dashboard do
this automatically): a per-database lock file serializes concurrent
migrators, so two containers booting together can never both try to
apply the same migration.
"""

import os
import sys
import sqlite3
import importlib.util
import contextlib

from core.config import DATABASE

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))


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
    # Only numbered migration files (NNN_name.py); excludes runner.py,
    # __init__.py, and anything starting with an underscore.
    files = sorted(
        f for f in os.listdir(MIGRATIONS_DIR)
        if f.endswith(".py") and f[:3].isdigit()
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


@contextlib.contextmanager
def _migration_lock(db_path):
    """Serialize migrators across processes sharing one SQLite database.

    The bot and dashboard containers both auto-migrate on startup and may
    boot at the same instant.  A naive migrator would race: both would
    read the same "pending" list, and the loser would crash on
    ``CREATE TABLE``/``UNIQUE`` conflicts.  ``flock`` on a lock file next
    to the DB (both containers bind-mount the same ``./data`` directory,
    so the lock is shared) makes the second migrator wait, then see the
    migrations the first one already recorded and do nothing.

    Falls back to a no-op on platforms without ``fcntl`` (e.g. Windows).

    Note: ``flock`` blocks until the lock is released (no timeout) — fine
    here because a migrate pass is short.  The lock file itself (``<db>.migrate.lock``)
    stays behind as a harmless 0-byte file next to the DB.
    """
    lock_path = db_path + ".migrate.lock"
    try:
        import fcntl
    except ImportError:
        fcntl = None

    with open(lock_path, "w") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lockf, fcntl.LOCK_UN)


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
    db_path = path or DATABASE
    # Serialize concurrent migrators (bot + dashboard boot together).
    with _migration_lock(db_path):
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            _ensure_migrations_table(conn)

            # Journal mode: WAL for parallel access (Docker container +
            # PyCharm/DBeaver). SAFE now: the DB lives in data/github_social.db
            # and the Docker container mounts the whole ./data dir as
            # /app/data, so WAL and SHM files are created NEXT TO the DB file
            # on the host — one shared filesystem location for the container,
            # host scripts and IDE tools. (Previously WAL was forced while
            # `./logs:/app/data` shadowed the DB path, splitting WAL/SHM into
            # ./logs and corrupting the database.)
            conn.execute("PRAGMA journal_mode=WAL;")

            applied = _applied_names(conn)
            files = _migration_files()

            pending = [f for f in files if f not in applied]

            if not pending:
                print("Nothing to migrate.")
                return

            for f in pending:
                mod_path = os.path.join(MIGRATIONS_DIR, f)
                mod = _load_module(mod_path)

                print(f"Applying {f} ...")
                mod.up(conn)
                _record(conn, f)
                print(f"  done.")
        finally:
            conn.close()


def rollback(path=None):
    db_path = path or DATABASE
    # Same lock as migrate(): a manual rollback must never interleave with
    # an auto-migrate from a concurrently booting container.
    with _migration_lock(db_path):
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            _ensure_migrations_table(conn)

            applied = _applied_names(conn)
            if not applied:
                print("Nothing to rollback.")
                return

            last = sorted(applied)[-1]
            mod_path = os.path.join(MIGRATIONS_DIR, last)
            mod = _load_module(mod_path)

            print(f"Rolling back {last} ...")
            mod.down(conn)
            _remove_record(conn, last)
            print("  done.")
        finally:
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
