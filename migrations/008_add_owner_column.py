"""Migration 008 — Add owner column to users table.

The owner is the system operator whose profile is used as the baseline
for similarity scoring.  Exactly one user can have owner = 1.
"""


def up(conn):
    columns = [
        row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    ]

    if "owner" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN owner INTEGER DEFAULT 0"
        )

    conn.commit()


def down(conn):
    # SQLite doesn't support DROP COLUMN in older versions.
    pass
