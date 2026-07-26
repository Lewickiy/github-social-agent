"""Migration 005 — Add score_version column to users for algorithm versioning."""


def up(conn):
    # Check if column already exists to make migration idempotent
    columns = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]

    if "score_version" not in columns:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN score_version INTEGER DEFAULT 0
            """
        )

    conn.commit()


def down(conn):
    # SQLite doesn't support DROP COLUMN in older versions,
    # but for completeness we note this is not reversible without table rebuild.
    pass
