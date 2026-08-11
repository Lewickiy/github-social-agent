"""Migration 028 — seed the unfollow worker's status row.

Adds the ``unfollow`` row to ``worker_status`` — the Management-tab
toggle for the new UnfollowWorker (workers/unfollow_worker.py).  The row
is seeded with the UNFOLLOW_WORKER_ENABLED config flag as the
fresh-install default (True), exactly like migration 027 did for the
other workers.  ``INSERT OR IGNORE`` keeps it a no-op on re-run and
never overwrites an existing toggle state.
"""

from core.config import UNFOLLOW_WORKER_ENABLED


def up(conn):
    conn.execute(
        "INSERT OR IGNORE INTO worker_status (name, enabled) VALUES (?, ?)",
        ("unfollow", 1 if UNFOLLOW_WORKER_ENABLED else 0),
    )
    conn.commit()


def down(conn):
    conn.execute("DELETE FROM worker_status WHERE name = 'unfollow'")
    conn.commit()
