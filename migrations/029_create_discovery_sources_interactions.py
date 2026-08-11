"""Migration 029 — schema groundwork: ``discovery_sources`` and ``interactions``.

Two new tables used by the repo-content features:

* ``discovery_sources`` — seed rotation for repo-content discovery (which
  repositories have already been mined for stargazers / contributors), so
  the discovery pass rotates seeds instead of re-fetching the same repo
  every cycle (mirrors the ``followers_scanned_at`` / ``followers_count``
  pattern used for graph discovery).
* ``interactions`` — persisted record of users interacting with the owner
  (WatchEvent / ForkEvent / IssuesEvent / PullRequestEvent /
  IssueCommentEvent / …), deduped on ``event_id`` with the REAL event
  timestamp in ``created_at`` (never the ingestion time) — required for
  temporal hygiene in ML feature extraction.

No behavioural change: nothing reads the new tables yet (issue #19).
"""


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovery_sources (
            id              INTEGER PRIMARY KEY,
            repo_full_name  TEXT    NOT NULL,      -- owner/name
            source_type     TEXT    NOT NULL,      -- 'stargazers' | 'contributors'
            last_checked_at TEXT,
            last_count      INTEGER,               -- last known result size
            UNIQUE (repo_full_name, source_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interactions (
            id               INTEGER PRIMARY KEY,
            username         TEXT    NOT NULL,     -- the actor
            event_type       TEXT    NOT NULL,     -- GitHub event type
            repo_full_name   TEXT,                 -- repository involved, when applicable
            event_id         TEXT    NOT NULL UNIQUE,  -- dedup key
            created_at       TEXT    NOT NULL,     -- the REAL event timestamp
            seen_at          TEXT    NOT NULL,     -- when the bot first noticed it
            we_starred       INTEGER NOT NULL DEFAULT 0,
            we_followed_back INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interactions_username_created "
        "ON interactions (username, created_at)"
    )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS interactions")
    conn.execute("DROP TABLE IF EXISTS discovery_sources")
    conn.commit()
