"""Migration 023 — Materialise ``user_current_status`` as a table.

``user_status_history`` is the single source of truth for statuses; the
``user_current_status`` view computed the latest transition on the fly.
On a 90k-user database that GROUP BY + self-join was re-evaluated on
*every* dashboard query, making /api/stats take ~2.3 s.

This migration replaces the view with a **table of the same name** that
is incrementally maintained by the app (see ``Database._upsert_current_status``),
so reads become cheap PK lookups instead of recomputed aggregates.

The table also carries ``followed_at`` (time of the latest FOLLOWED
transition) so the Users list can sort by follow time without a
correlated subquery, and a set of covering indexes for the dashboard's
counting / sorting queries.
"""


def up(conn):
    # Same name as the old view (tables and views share one namespace,
    # so the view must be dropped first).
    conn.execute("DROP VIEW IF EXISTS user_current_status")
    conn.execute(
        """
        CREATE TABLE user_current_status (
            username    TEXT PRIMARY KEY,
            status      TEXT NOT NULL,
            changed_at  TEXT,
            followed_at TEXT
        )
        """
    )
    # Backfill: latest transition per user, plus the time of their last
    # FOLLOWED transition (NULL when never followed).
    conn.execute(
        """
        INSERT INTO user_current_status (username, status, changed_at, followed_at)
        SELECT h.username, h.status, h.changed_at,
               (SELECT h2.changed_at FROM user_status_history h2
                WHERE h2.username = h.username AND h2.status = 'FOLLOWED'
                ORDER BY h2.id DESC LIMIT 1)
        FROM user_status_history h
        JOIN (
            SELECT username, MAX(id) AS max_id
            FROM user_status_history
            GROUP BY username
        ) m ON m.username = h.username AND m.max_id = h.id
        """
    )

    # Covering indexes so COUNT / ORDER BY queries never scan the wide
    # ``users`` rows (90k rows × 30+ columns, including JSON blobs).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_owner         ON users(owner)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_score         ON users(score)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_followers     ON users(followers)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_created_at    ON users(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_repos_fetched ON users(repos_fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_current_status_status     ON user_current_status(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_current_status_followed_at ON user_current_status(followed_at)")

    # Refresh planner statistics so the new indexes get used.
    conn.execute("ANALYZE")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS user_current_status")
    conn.execute(
        """
        CREATE VIEW user_current_status AS
        SELECT h.username, h.status, h.changed_at
        FROM user_status_history h
        JOIN (
            SELECT username, MAX(id) AS max_id
            FROM user_status_history
            GROUP BY username
        ) m ON m.username = h.username AND m.max_id = h.id
        """
    )
    conn.commit()
