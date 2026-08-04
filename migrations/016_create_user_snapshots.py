"""Migration 016 — user_snapshots table (daily historical snapshots).

Stores one snapshot per user per day of mutable GitHub profile data:
  * followers_count   — number of followers
  * following_count   — number of users the user follows
  * public_repos_count — public repository count
  * public_gists_count — public gist count
  * extra_json         — any other changing fields (name, company, bio,
                         location, …) captured for future analytics.

The table is generic (keyed by username), so snapshots can later be
accumulated for *every* user — today the collector only writes the owner.

``UNIQUE(username, snapshot_date)`` guarantees at most one snapshot per
user per day (the midnight update is an upsert).
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_snapshots (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            username           TEXT    NOT NULL,
            snapshot_date      TEXT    NOT NULL,
            followers_count    INTEGER,
            following_count    INTEGER,
            public_repos_count INTEGER,
            public_gists_count INTEGER,
            extra_json         TEXT,
            created_at         TEXT,
            updated_at         TEXT,
            UNIQUE (username, snapshot_date)
        )
        """
    )
    # NOTE: UNIQUE (username, snapshot_date) already creates the implicit
    # index SQLite uses to enforce the constraint — no extra index needed.
    conn.commit()
    print("  Created user_snapshots table.")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS user_snapshots")
    conn.commit()
