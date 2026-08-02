"""Migration 017 — ETag cache columns for conditional (free) API requests.

GitHub supports conditional requests: when the client sends an
``If-None-Match`` header with a previously received ``ETag`` and the
resource is unchanged, GitHub answers ``304 Not Modified`` **without
counting the request against the rate limit**.

New columns:
  * users.repos_etag            — ETag of the last GET /users/{user}/repos
  * repositories.languages_etag — ETag of the last
                                  GET /repos/{owner}/{repo}/languages

These let repeat collection runs (every REPO_FRESHNESS_DAYS) skip
unchanged data for free instead of paying full price.
"""


def up(conn):
    user_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    ]
    if "repos_etag" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN repos_etag TEXT")

    repo_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(repositories)").fetchall()
    ]
    if "languages_etag" not in repo_cols:
        conn.execute("ALTER TABLE repositories ADD COLUMN languages_etag TEXT")

    conn.commit()


def down(conn):
    pass
