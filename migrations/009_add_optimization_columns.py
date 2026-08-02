"""Migration 009 — Add optimization columns for API call reduction.

New columns:
  * users.repos_fetched_at    — timestamp of last repo collection (TTL-based skip)
  * users.followers_scanned_at — timestamp of last follower graph scan
  * users.followers_count      — stored follower count (incremental scan detection)
  * status 'FOLLOWBACK'        — user followed us back after we followed them
"""


def up(conn):
    columns = [
        row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    ]

    if "repos_fetched_at" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN repos_fetched_at TEXT"
        )

    if "followers_scanned_at" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN followers_scanned_at TEXT"
        )

    if "followers_count" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN followers_count INTEGER DEFAULT 0"
        )

    conn.commit()


def down(conn):
    pass
