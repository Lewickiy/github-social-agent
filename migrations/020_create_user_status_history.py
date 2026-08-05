"""Migration 020 — user status history.

Creates ``user_status_history`` — an append-only transition log storing
every lifecycle status a user has entered and *when* they entered it
(``changed_at``).  This log becomes the single store of user statuses
(see migration 021, which removes the duplicated ``users.status`` /
``users.followed_at`` columns and exposes the current status through the
``user_current_status`` view), so dashboard windows ("users entering
each status · last N days") can be scoped by the actual transition time
instead of approximations.

Backfill reconstructs each user's history from the data already stored:

* ``NEW`` — the initial state, stamped with ``users.created_at``;
* ``FOLLOWED`` — from logged ``actions`` FOLLOW events where available,
  otherwise from ``users.followed_at`` (the follow time);
* ``FOLLOWBACK`` / ``UNFOLLOWED_AFTER_MUTUAL_FOLLOW`` / ``DELETED`` —
  from logged ``actions`` events where available, otherwise stamped with
  the closest timestamp we have (``followed_at`` → ``created_at``).

All inserts are idempotent (``NOT EXISTS`` + ``GROUP BY``), so re-running
the migration never duplicates rows.
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_status_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL,
            status     TEXT    NOT NULL,
            changed_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_status_history_user "
        "ON user_status_history(username, changed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_status_history_status "
        "ON user_status_history(status, changed_at)"
    )

    # Timestamp fallback in the same ISO-8601 shape the app writes
    # (datetime.now(timezone.utc).isoformat()).
    _NOW = "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"

    # 1) Initial state — every user entered as NEW when discovered.
    conn.execute(
        f"""
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT username, 'NEW', COALESCE(created_at, {_NOW})
        FROM users
        WHERE NOT EXISTS (
            SELECT 1 FROM user_status_history h
            WHERE h.username = users.username AND h.status = 'NEW'
        )
        """
    )

    # 2) FOLLOWED — from logged FOLLOW actions where available…
    conn.execute(
        """
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT a.username, 'FOLLOWED', MIN(a.created_at)
        FROM actions a
        WHERE a.action = 'FOLLOW'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = a.username AND h.status = 'FOLLOWED'
          )
        GROUP BY a.username
        """
    )
    # …else from users.followed_at (the follow time).  Covers every user
    # that was ever followed, including those whose follow was never logged
    # (e.g. the silent "already following" path), users who have since moved
    # on to FOLLOWBACK / UNFOLLOWED_AFTER_MUTUAL_FOLLOW / DELETED, and the
    # rare current-FOLLOWED user without a stored follow time (falls back
    # to created_at).
    conn.execute(
        f"""
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT username, 'FOLLOWED',
               COALESCE(followed_at, created_at, {_NOW})
        FROM users
        WHERE (followed_at IS NOT NULL OR status = 'FOLLOWED')
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = users.username AND h.status = 'FOLLOWED'
          )
        """
    )

    # 3) FOLLOWBACK — logged actions where available, else followed_at.
    conn.execute(
        """
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT a.username, 'FOLLOWBACK', MIN(a.created_at)
        FROM actions a
        WHERE a.action = 'FOLLOWBACK'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = a.username AND h.status = 'FOLLOWBACK'
          )
        GROUP BY a.username
        """
    )
    conn.execute(
        f"""
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT username, 'FOLLOWBACK',
               COALESCE(followed_at, created_at, {_NOW})
        FROM users
        WHERE status = 'FOLLOWBACK'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = users.username AND h.status = 'FOLLOWBACK'
          )
        """
    )

    # 4) UNFOLLOWED_AFTER_MUTUAL_FOLLOW — logged actions, else followed_at.
    conn.execute(
        """
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT a.username, 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW', MIN(a.created_at)
        FROM actions a
        WHERE a.action = 'UNFOLLOWED'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = a.username
                AND h.status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'
          )
        GROUP BY a.username
        """
    )
    conn.execute(
        f"""
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT username, 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW',
               COALESCE(followed_at, created_at, {_NOW})
        FROM users
        WHERE status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = users.username
                AND h.status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'
          )
        """
    )

    # 5) DELETED — logged actions, else the closest timestamp available.
    conn.execute(
        """
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT a.username, 'DELETED', MIN(a.created_at)
        FROM actions a
        WHERE a.action = 'DELETED'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = a.username AND h.status = 'DELETED'
          )
        GROUP BY a.username
        """
    )
    conn.execute(
        f"""
        INSERT INTO user_status_history (username, status, changed_at)
        SELECT username, 'DELETED',
               COALESCE(followed_at, created_at, {_NOW})
        FROM users
        WHERE status = 'DELETED'
          AND NOT EXISTS (
              SELECT 1 FROM user_status_history h
              WHERE h.username = users.username AND h.status = 'DELETED'
          )
        """
    )

    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS user_status_history")
    conn.commit()
