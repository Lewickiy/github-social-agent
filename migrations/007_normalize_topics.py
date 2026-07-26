"""Migration 007 — Normalize topics into a separate table (many-to-many).

Creates:
  * topics            — unique topic names (like the languages table)
  * repository_topics — junction table linking repositories to topics

Migrates existing data from the old repository_topics table (which stored
topic strings directly) into the new normalised structure, then drops
the old table.
"""


def up(conn):
    # ----------------------------------------------------------
    # 1. Rename old table so we can migrate its data
    # ----------------------------------------------------------
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if "repository_topics" in tables:
        conn.execute("ALTER TABLE repository_topics RENAME TO _repository_topics_old")
    else:
        # Table doesn't exist yet — nothing to migrate
        pass

    # ----------------------------------------------------------
    # 2. Create the normalised topics table
    # ----------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topics (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE
        )
        """
    )

    # ----------------------------------------------------------
    # 3. Create the new junction table
    # ----------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repository_topics (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            repository_id INTEGER NOT NULL,
            topic_id      INTEGER NOT NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id),
            FOREIGN KEY (topic_id)      REFERENCES topics(id),
            UNIQUE(repository_id, topic_id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_repo_topics_repo
        ON repository_topics(repository_id)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_repo_topics_topic
        ON repository_topics(topic_id)
        """
    )

    # ----------------------------------------------------------
    # 4. Migrate data from the old table (if it existed)
    # ----------------------------------------------------------
    if "_repository_topics_old" in tables:
        old_rows = conn.execute(
            "SELECT repository_id, topic FROM _repository_topics_old"
        ).fetchall()

        for repo_id, topic_name in old_rows:
            # Insert topic, get its id (ON CONFLICT — no duplicates)
            conn.execute(
                "INSERT OR IGNORE INTO topics (name) VALUES (?)",
                (topic_name,),
            )
            topic_id = conn.execute(
                "SELECT id FROM topics WHERE name = ?",
                (topic_name,),
            ).fetchone()[0]

            # Link repo ↔ topic
            conn.execute(
                """
                INSERT OR IGNORE INTO repository_topics
                    (repository_id, topic_id)
                VALUES (?, ?)
                """,
                (repo_id, topic_id),
            )

        # Drop the old table
        conn.execute("DROP TABLE _repository_topics_old")

    conn.commit()


def down(conn):
    """Restore the old flat repository_topics table."""
    conn.execute("DROP INDEX IF EXISTS idx_repo_topics_topic")
    conn.execute("DROP INDEX IF EXISTS idx_repo_topics_repo")
    conn.execute("DROP TABLE IF EXISTS repository_topics")
    conn.execute("DROP TABLE IF EXISTS topics")

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
