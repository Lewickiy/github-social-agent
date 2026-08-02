"""Migration 010 — Backfill repos_fetched_at for existing users.

Users that already have repository data collected before the
optimization migration (009) have repos_fetched_at = NULL.
Without backfilling, the system would treat all 90K+ users as
"never fetched" and attempt to re-collect everything on the
next run.

This migration sets repos_fetched_at = updated_at for users
that have at least one repository, so they are treated as
fresh (and will be skipped until the TTL expires naturally).
"""


def up(conn):
    conn.execute(
        """
        UPDATE users
        SET repos_fetched_at = (
            SELECT MAX(r.updated_at)
            FROM repositories r
            WHERE r.user_id = users.username
        )
        WHERE repos_fetched_at IS NULL
          AND EXISTS (
              SELECT 1 FROM repositories r WHERE r.user_id = users.username
          )
        """
    )
    conn.commit()

    # Report how many rows were updated
    count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE repos_fetched_at IS NOT NULL"
    ).fetchone()[0]
    print(f"  Backfilled repos_fetched_at for {count} users.")


def down(conn):
    conn.execute("UPDATE users SET repos_fetched_at = NULL")
    conn.commit()
