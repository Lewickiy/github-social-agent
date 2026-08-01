"""Migration 013 — Add per-repository last_checked_at column.

Adds a `last_checked_at` timestamp to the `repositories` table
to enable per-repo TTL checks in silent mode (skip the
``/repos/{owner}/{repo}/languages`` API call for repos that
were verified recently — by default within 5 days).

Unlike `updated_at`, which is only refreshed when a tracked
field actually changes, `last_checked_at` is updated every time
we process the repo (whether or not its data changed).  This
matches the "freshness of our last check" semantics that the
silent-mode TTL needs.

The column is backfilled from `updated_at` for existing rows so
that none of them are treated as "never checked" after the
migration runs.
"""


def up(conn):
    columns = [
        row[1] for row in conn.execute("PRAGMA table_info(repositories)").fetchall()
    ]

    if "last_checked_at" not in columns:
        conn.execute("ALTER TABLE repositories ADD COLUMN last_checked_at TEXT")

    # Backfill: any repo that already has an `updated_at` is treated
    # as already checked — avoids a mass re-fetch right after deploy.
    cur = conn.execute(
        """
        UPDATE repositories
        SET last_checked_at = updated_at
        WHERE last_checked_at IS NULL
          AND updated_at IS NOT NULL
        """
    )
    conn.commit()

    print(f"  Backfilled last_checked_at for {cur.rowcount} repositories.")


def down(conn):
    # SQLite < 3.35 has limited DROP COLUMN support; leave the column in place
    # rather than risking a destructive rewrite of the repositories table.
    pass
