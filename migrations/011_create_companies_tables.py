"""Migration 011 — Create companies and user_companies tables.

* companies        — full profile data for GitHub organizations/users
                     referenced via @login in users.company field.
* user_companies   — many-to-many junction between users and companies.
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            login             TEXT    UNIQUE NOT NULL,
            github_id         INTEGER UNIQUE,
            name              TEXT,
            description       TEXT,
            html_url          TEXT,
            blog              TEXT,
            location          TEXT,
            email             TEXT,
            twitter_username  TEXT,
            public_repos      INTEGER DEFAULT 0,
            followers         INTEGER DEFAULT 0,
            following         INTEGER DEFAULT 0,
            avatar_url        TEXT,
            company_type      TEXT,
            github_created_at TEXT,
            github_updated_at TEXT,
            created_at        TEXT,
            updated_at        TEXT,
            api_fetched_at    TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_companies (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL,
            company_id INTEGER NOT NULL,
            FOREIGN KEY (username)   REFERENCES users(username) ON DELETE CASCADE,
            FOREIGN KEY (company_id) REFERENCES companies(id)   ON DELETE CASCADE,
            UNIQUE(username, company_id)
        )
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_companies_username ON user_companies(username)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_companies_company  ON user_companies(company_id)"
    )

    conn.commit()


def down(conn):
    conn.execute("DROP INDEX IF EXISTS idx_user_companies_company")
    conn.execute("DROP INDEX IF EXISTS idx_user_companies_username")
    conn.execute("DROP TABLE IF EXISTS user_companies")
    conn.execute("DROP TABLE IF EXISTS companies")
    conn.commit()
