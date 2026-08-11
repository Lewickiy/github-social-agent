"""Migration 027 — worker runtime status table.

Holds one row per background worker thread so the dashboard can show
live per-worker state and pause/resume workers at runtime:

  * ``enabled`` — the Management-tab toggle (1 = active).  Defaults to 1
    so **every worker is active by default** when the system starts; the
    dashboard can pause/resume a worker without restarting the bot.
  * ``started_at`` / ``stopped_at`` — when the worker thread last started
    / cleanly stopped.
  * ``last_action_at`` — when the worker last completed a unit of work.
  * ``last_error_at`` / ``last_error`` — when the worker last hit an
    error and its message.
  * ``heartbeat_at`` — liveness tick refreshed every ~60 s even during
    long (hour/day) sleeps; the dashboard treats a stale heartbeat as an
    unreachable (down/crashed) worker.

Rows are seeded for all six workers (INSERT OR IGNORE), honouring the
FOLLOW_WORKER_ENABLED / DISCOVERY_WORKER_ENABLED config flags as the
fresh-install default for those two workers (both default True).
"""

from core.config import DISCOVERY_WORKER_ENABLED, FOLLOW_WORKER_ENABLED

# (worker key, enabled-by-default) — keys must match the WORKER_KEY
# constants in workers/*.py and the registry in workers/registry.py.
WORKERS = (
    ("follow", FOLLOW_WORKER_ENABLED),
    ("graph_discovery", DISCOVERY_WORKER_ENABLED),
    ("ml_trainer", True),
    ("company", True),
    ("followback_check", True),
    ("snapshot", True),
)


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_status (
            name           TEXT PRIMARY KEY,
            enabled        INTEGER NOT NULL DEFAULT 1,
            started_at     TEXT,
            stopped_at     TEXT,
            last_action_at TEXT,
            last_error_at  TEXT,
            last_error     TEXT,
            heartbeat_at   TEXT
        )
        """
    )
    for name, enabled in WORKERS:
        conn.execute(
            "INSERT OR IGNORE INTO worker_status (name, enabled) VALUES (?, ?)",
            (name, 1 if enabled else 0),
        )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS worker_status")
    conn.commit()
