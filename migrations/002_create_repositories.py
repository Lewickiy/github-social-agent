"""Migration 002 — Create repositories table."""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repositories (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            github_repository_id  INTEGER NOT NULL UNIQUE,
            user_id               TEXT    NOT NULL,
            name                  TEXT    NOT NULL,
            description           TEXT,
            html_url              TEXT,
            stars                 INTEGER DEFAULT 0,
            forks                 INTEGER DEFAULT 0,
            watchers              INTEGER DEFAULT 0,
            is_fork               INTEGER DEFAULT 0,
            is_archived           INTEGER DEFAULT 0,
            default_branch        TEXT,
            created_at            TEXT,
            updated_at            TEXT,
            repository_created_at TEXT,
            repository_updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(username)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_repositories_user_id
        ON repositories(user_id)
        """
    )

    conn.commit()


def down(conn):
    conn.execute("DROP INDEX IF EXISTS idx_repositories_user_id")
    conn.execute("DROP TABLE IF EXISTS repositories")
    conn.commit()
