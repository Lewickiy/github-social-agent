"""Migration 004 — Create repository_languages join table."""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repository_languages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            repository_id INTEGER NOT NULL,
            language_id   INTEGER NOT NULL,
            weight        REAL    NOT NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id),
            FOREIGN KEY (language_id)   REFERENCES languages(id),
            UNIQUE(repository_id, language_id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_repo_lang_repo
        ON repository_languages(repository_id)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_repo_lang_lang
        ON repository_languages(language_id)
        """
    )

    conn.commit()


def down(conn):
    conn.execute("DROP INDEX IF EXISTS idx_repo_lang_lang")
    conn.execute("DROP INDEX IF EXISTS idx_repo_lang_repo")
    conn.execute("DROP TABLE IF EXISTS repository_languages")
    conn.commit()
