"""Migration 001 — Initial schema: users + actions tables."""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username         TEXT PRIMARY KEY,
            discovered_from  TEXT,
            score            INTEGER DEFAULT 0,
            public_repos     INTEGER DEFAULT 0,
            followers        INTEGER DEFAULT 0,
            bio              TEXT,
            company          TEXT,
            status           TEXT    DEFAULT 'NEW',
            created_at       TEXT,
            scored_at        TEXT,
            followed_at      TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS actions (
            id         INTEGER PRIMARY KEY,
            username   TEXT,
            action     TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS actions")
    conn.execute("DROP TABLE IF EXISTS users")
    conn.commit()
