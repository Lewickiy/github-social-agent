"""Migration 021 — statuses live only in user_status_history.

Removes the now-redundant ``users.status`` / ``users.followed_at``
columns.  ``user_status_history`` is the single store of user statuses;
the *current* status is a projection of its latest row, exposed through
the ``user_current_status`` view (one row per user: latest status +
``changed_at``).

The view keeps every current-status read on one consistent source, so
two tables can never disagree about a user's status again.
"""


def up(conn):
    # Defensive safety net: ensure every user has at least one history
    # row (the NEW transition) before the users.status column disappears.
    # After migration 020 this is normally a no-op.
    conn.execute(
        """
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT username, 'NEW',
               COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        FROM users
        WHERE NOT EXISTS (
            SELECT 1 FROM user_status_history h
            WHERE h.username = users.username
        )
        """
    )

    # "Latest row per user" — the view needs an efficient (username, id)
    # index for the MAX(id) GROUP BY.
    #
    # NOTE: the view recomputes the latest row per user on every reference
    # (a GROUP BY over the history log).  At ~90k users this is tens of ms
    # per query — fine for a single-user dashboard, but if the history log
    # grows very large this is the first place to revisit.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_status_history_username_id "
        "ON user_status_history(username, id)"
    )

    conn.execute("DROP VIEW IF EXISTS user_current_status")
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

    conn.execute("ALTER TABLE users DROP COLUMN status")
    conn.execute("ALTER TABLE users DROP COLUMN followed_at")

    conn.commit()


def down(conn):
    conn.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'NEW'")
    conn.execute("ALTER TABLE users ADD COLUMN followed_at TEXT")

    # Restore current status / follow time from the history log (best effort).
    conn.execute(
        """
        UPDATE users SET status = COALESCE((
            SELECT h.status FROM user_status_history h
            WHERE h.username = users.username
            ORDER BY h.id DESC LIMIT 1
        ), 'NEW')
        """
    )
    conn.execute(
        """
        UPDATE users SET followed_at = (
            SELECT h.changed_at FROM user_status_history h
            WHERE h.username = users.username AND h.status = 'FOLLOWED'
            ORDER BY h.id DESC LIMIT 1
        )
        """
    )
    conn.execute("DROP VIEW IF EXISTS user_current_status")
    conn.commit()
