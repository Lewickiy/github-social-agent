"""Migration 025 — ``discovery_runs``: per-pass history of the graph-discovery worker.

The calm background worker (``workers/graph_discovery_worker.py``) walks
the follower graph once per hour window.  Each pass is now recorded here
so the dashboard can show its activity: when it ran, how many users it
walked, how many new users it found, how many GitHub requests it actually
made (against the ``DISCOVERY_RATE_LIMIT_PER_HOUR`` budget) and how long
it took.

The bot process writes the rows (``Database.record_discovery_run``); the
dashboard process (separate container) reads them via the shared SQLite
DB — the same split-brain-free layout as ``ml_training_runs``.
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at       TEXT,
            finished_at      TEXT,
            users_walked     INTEGER,
            new_users        INTEGER,
            requests         INTEGER,
            duration_seconds REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovery_runs_started_at "
        "ON discovery_runs(started_at)"
    )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS discovery_runs")
    conn.commit()
