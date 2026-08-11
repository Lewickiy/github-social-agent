"""Migration 030 — seed the attention worker's status row.

Adds the ``attention`` row to ``worker_status`` — the Management-tab
toggle for the new AttentionWorker (workers/attention_worker.py), which
polls the owner's event timeline and persists interactions with the
owner's repositories.  The row is seeded with the ATTENTION_WORKER_ENABLED
config flag as the fresh-install default (True), exactly like migration
027/028 did for the other workers.  ``INSERT OR IGNORE`` keeps it a no-op
on re-run and never overwrites an existing toggle state.
"""

from core.config import ATTENTION_WORKER_ENABLED


def up(conn):
    conn.execute(
        "INSERT OR IGNORE INTO worker_status (name, enabled) VALUES (?, ?)",
        ("attention", 1 if ATTENTION_WORKER_ENABLED else 0),
    )
    conn.commit()


def down(conn):
    conn.execute("DELETE FROM worker_status WHERE name = 'attention'")
    conn.commit()
