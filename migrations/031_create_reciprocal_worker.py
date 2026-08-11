"""Migration 031 — ``reciprocal`` row in worker_status (issue #24).

Adds the ``reciprocal`` row to ``worker_status`` — the Management-tab
toggle for the new ReciprocalWorker (workers/reciprocal_worker.py), which
reacts to new interactors recorded by the attention worker (#21) by
following them and starring their most relevant repository, paced like
follows.  The row is seeded with the RECIPROCAL_WORKER_ENABLED config flag
as the fresh-install default (True), exactly like migration 027/028/030
did for the other workers.  ``INSERT OR IGNORE`` keeps it a no-op on
re-run and never overwrites an existing toggle state.
"""

from core.config import RECIPROCAL_WORKER_ENABLED


def up(conn):
    conn.execute(
        "INSERT OR IGNORE INTO worker_status (name, enabled) VALUES (?, ?)",
        ("reciprocal", 1 if RECIPROCAL_WORKER_ENABLED else 0),
    )
    conn.commit()


def down(conn):
    conn.execute("DELETE FROM worker_status WHERE name = 'reciprocal'")
    conn.commit()
