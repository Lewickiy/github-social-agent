"""Migration 006 — Create repository_topics table."""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repository_topics (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            repository_id INTEGER NOT NULL,
            topic         TEXT    NOT NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id),
            UNIQUE(repository_id, topic)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_repo_topics_repo
        ON repository_topics(repository_id)
        """
    )

    conn.commit()


def down(conn):
    conn.execute("DROP INDEX IF EXISTS idx_repo_topics_repo")
    conn.execute("DROP TABLE IF EXISTS repository_topics")
    conn.commit()
