"""Migration 022 — App settings (key/value) table.

Holds user preferences that the dashboard can change at runtime, e.g.
the user's timezone (``timezone`` = IANA name, e.g. ``Europe/Moscow``).
Absence of a row means the value is unset — the app then falls back to
its built-in default (UTC) and the UI offers auto-detection.
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS settings")
    conn.commit()
