"""Migration 018 — github_api_requests log table.

Every GitHub API round-trip made by the bot subprocesses is appended here
(github_client.record_api_request) so the dashboard can show the *real*
request rate per hour (rolling 60-minute window) instead of a static
counter.  Rows older than ~48h are pruned by the API server, so the table
stays small.
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS github_api_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint    TEXT    NOT NULL,
            status_code INTEGER,
            created_at  TEXT    NOT NULL
        )
        """
    )
    # Fast per-hour / per-day range scans on the request log.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_github_api_requests_created "
        "ON github_api_requests(created_at)"
    )
    conn.commit()
    print("  Created github_api_requests table.")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS github_api_requests")
    conn.commit()
