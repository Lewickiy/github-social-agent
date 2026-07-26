"""Migration 003 — Create languages table."""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS languages (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE
        )
        """
    )

    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS languages")
    conn.commit()
