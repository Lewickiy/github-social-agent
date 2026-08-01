"""Migration 015 — job_runs table for the dashboard.

Tracks bot mode invocations started from the web dashboard
(collect, score, follow, silent, ...) so the UI can show
running/finished jobs, exit codes and errors.
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mode        TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'PENDING',
            pid         INTEGER,
            started_at  TEXT,
            finished_at TEXT,
            exit_code   INTEGER,
            error       TEXT,
            created_at  TEXT
        )
        """
    )
    conn.commit()
    print("  Created job_runs table.")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS job_runs")
    conn.commit()
