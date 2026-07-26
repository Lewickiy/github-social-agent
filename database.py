import sqlite3
from datetime import datetime, timezone
from config import DATABASE

CURRENT_SCORE_VERSION = 3


class Database:
    """Thin wrapper around SQLite for the github_social schema."""

    def __init__(self, path=None):
        self.conn = sqlite3.connect(path or DATABASE)

    # --------------------------------------------------
    # Users
    # --------------------------------------------------

    def add_user(self, username, source, owner=False):
        self.conn.execute(
            """
            INSERT OR IGNORE INTO users
                (username, discovered_from, created_at, owner)
            VALUES (?, ?, ?, ?)
            """,
            (username, source, datetime.now(timezone.utc).isoformat(), 1 if owner else 0),
        )
        self.conn.commit()

    def set_owner(self, username):
        """Mark *username* as the owner.  Clears any previous owner."""
        self.conn.execute("UPDATE users SET owner = 0")
        self.conn.execute(
            "UPDATE users SET owner = 1 WHERE username = ?",
            (username,),
        )
        self.conn.commit()

    def get_owner(self):
        """Return the owner username, or None."""
        row = self.conn.execute(
            "SELECT username FROM users WHERE owner = 1"
        ).fetchone()
        return row[0] if row else None

    def update_score(self, username, score, info):
        self.conn.execute(
            """
            UPDATE users
            SET score        = ?,
                public_repos = ?,
                followers    = ?,
                bio          = ?,
                company      = ?,
                scored_at    = ?,
                score_version = ?
            WHERE username = ?
            """,
            (
                score,
                info.get("public_repos", 0),
                info.get("followers", 0),
                info.get("bio"),
                info.get("company"),
                datetime.now(timezone.utc).isoformat(),
                CURRENT_SCORE_VERSION,
                username,
            ),
        )
        self.conn.commit()

    def top_users(self, limit):
        rows = self.conn.execute(
            """
            SELECT username, score
            FROM users
            WHERE status = 'NEW'
            ORDER BY score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows

    def unscored_users(self):
        """Return usernames that need (re-)scoring.

        A user needs scoring when any of these is true:
          - never scored  (score = 0 AND scored_at IS NULL),  OR
          - score_version is behind the current algorithm version,  OR
          - has no repository data yet (repos not collected),  OR
          - repos were collected AFTER the last scoring (stale score).

        The owner is excluded.
        """
        rows = self.conn.execute(
            """
            SELECT u.username FROM users u
            WHERE u.owner = 0
              AND (   (u.score = 0 AND u.scored_at IS NULL)
                   OR u.score_version < ?
                   OR NOT EXISTS (
                       SELECT 1 FROM repositories r WHERE r.user_id = u.username
                   )
                   OR EXISTS (
                       SELECT 1 FROM repositories r
                       WHERE r.user_id = u.username
                         AND u.scored_at < r.updated_at
                   )
                  )
            """,
            (CURRENT_SCORE_VERSION,),
        ).fetchall()
        return rows

    def mark_followed(self, username):
        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """
            UPDATE users
            SET status = 'FOLLOWED',
                followed_at = ?
            WHERE username = ?
            """,
            (now, username),
        )

        self.conn.execute(
            """
            INSERT INTO actions (username, action, created_at)
            VALUES (?, ?, ?)
            """,
            (username, "FOLLOW", now),
        )

        self.conn.commit()

    # --------------------------------------------------
    # Actions / stats
    # --------------------------------------------------

    def today_follows(self):
        return self.conn.execute(
            """
            SELECT COUNT(*)
            FROM actions
            WHERE action = 'FOLLOW'
              AND date(created_at) = date('now')
            """
        ).fetchone()[0]

    # --------------------------------------------------
    # Repositories
    # --------------------------------------------------

    def save_repository(self, user_id, repo_data):
        """Insert or update a repository record.  Returns the local repo id."""
        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """
            INSERT INTO repositories (
                github_repository_id, user_id, name, description, html_url,
                stars, forks, watchers, is_fork, is_archived, default_branch,
                created_at, updated_at, repository_created_at, repository_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(github_repository_id) DO UPDATE SET
                name                  = excluded.name,
                description           = excluded.description,
                html_url              = excluded.html_url,
                stars                 = excluded.stars,
                forks                 = excluded.forks,
                watchers              = excluded.watchers,
                is_fork               = excluded.is_fork,
                is_archived           = excluded.is_archived,
                default_branch        = excluded.default_branch,
                updated_at            = excluded.updated_at,
                repository_updated_at = excluded.repository_updated_at
            """,
            (
                repo_data["id"],
                user_id,
                repo_data["name"],
                repo_data.get("description"),
                repo_data["html_url"],
                repo_data.get("stargazers_count", 0),
                repo_data.get("forks_count", 0),
                repo_data.get("watchers_count", 0),
                1 if repo_data.get("fork") else 0,
                1 if repo_data.get("archived") else 0,
                repo_data.get("default_branch", "main"),
                now,
                now,
                repo_data.get("created_at"),
                repo_data.get("updated_at"),
            ),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT id FROM repositories WHERE github_repository_id = ?",
            (repo_data["id"],),
        ).fetchone()
        return row[0]

    # --------------------------------------------------
    # Languages
    # --------------------------------------------------

    def ensure_language(self, name):
        """Insert language if not present.  Always returns the language id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO languages (name) VALUES (?)",
            (name,),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT id FROM languages WHERE name = ?",
            (name,),
        ).fetchone()
        return row[0]

    def save_repository_languages(self, repository_id, language_bytes):
        """Save language weights for a repository.

        *language_bytes* is a dict like {"Java": 150000, "Python": 50000}.
        Weights are stored as percentages (0-100, float).
        """
        total = sum(language_bytes.values())
        if total == 0:
            return

        # Clear old entries for this repo (re-score idempotency)
        self.conn.execute(
            "DELETE FROM repository_languages WHERE repository_id = ?",
            (repository_id,),
        )

        for lang_name, byte_count in language_bytes.items():
            weight = round((byte_count / total) * 100, 2)
            lang_id = self.ensure_language(lang_name)

            self.conn.execute(
                """
                INSERT INTO repository_languages
                    (repository_id, language_id, weight)
                VALUES (?, ?, ?)
                """,
                (repository_id, lang_id, weight),
            )

        self.conn.commit()

    # --------------------------------------------------
    # Developer profile
    # --------------------------------------------------

    def user_languages(self, username):
        """Return aggregated language percentages for *username*.

        Sums weights across all repos, normalises to percentages, and
        returns a sorted list of (language_name, percentage).
        """
        rows = self.conn.execute(
            """
            SELECT l.name, SUM(rl.weight) AS total
            FROM repository_languages rl
            JOIN repositories r  ON r.id  = rl.repository_id
            JOIN languages   l  ON l.id  = rl.language_id
            WHERE r.user_id = ?
            GROUP BY l.name
            ORDER BY total DESC
            """,
            (username,),
        ).fetchall()

        if not rows:
            return []

        grand_total = sum(w for _, w in rows)
        if grand_total == 0:
            return []

        return [(name, round((w / grand_total) * 100, 1)) for name, w in rows]

    def user_repo_count(self, username):
        return self.conn.execute(
            "SELECT COUNT(*) FROM repositories WHERE user_id = ?",
            (username,),
        ).fetchone()[0]

    def user_repo_recency(self, username):
        """Return days since the user's most recently updated repository.

        Returns None if the user has no repos.
        """
        row = self.conn.execute(
            """
            SELECT MAX(repository_updated_at)
            FROM repositories
            WHERE user_id = ?
            """,
            (username,),
        ).fetchone()

        if not row or not row[0]:
            return None

        try:
            updated = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - updated).days
        except (ValueError, TypeError):
            return None

    # --------------------------------------------------
    # Topics
    # --------------------------------------------------

    def ensure_topic(self, name):
        """Insert topic if not present.  Always returns the topic id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO topics (name) VALUES (?)",
            (name,),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT id FROM topics WHERE name = ?",
            (name,),
        ).fetchone()
        return row[0]

    def save_repository_topics(self, repository_id, topics):
        """Save topics for a repository (normalised many-to-many).

        *topics* is a list of topic strings from the GitHub API.
        """
        # Clear old entries for this repo (idempotent re-collection)
        self.conn.execute(
            "DELETE FROM repository_topics WHERE repository_id = ?",
            (repository_id,),
        )

        for topic_name in topics:
            topic_id = self.ensure_topic(topic_name)
            self.conn.execute(
                """
                INSERT INTO repository_topics (repository_id, topic_id)
                VALUES (?, ?)
                """,
                (repository_id, topic_id),
            )

        self.conn.commit()

    def user_topics(self, username):
        """Return all unique topics across the user's repositories."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT t.name
            FROM repository_topics rt
            JOIN repositories r ON r.id  = rt.repository_id
            JOIN topics        t ON t.id  = rt.topic_id
            WHERE r.user_id = ?
            ORDER BY t.name
            """,
            (username,),
        ).fetchall()
        return [row[0] for row in rows]

    def developer_profile(self, username):
        """Build a full developer profile dict for display."""
        user_row = self.conn.execute(
            """
            SELECT username, score, public_repos, followers, bio, company
            FROM users WHERE username = ?
            """,
            (username,),
        ).fetchone()

        if not user_row:
            return None

        langs = self.user_languages(username)
        repo_count = self.user_repo_count(username)
        topics = self.user_topics(username)

        return {
            "username": user_row[0],
            "score": user_row[1],
            "public_repos": user_row[2],
            "followers": user_row[3],
            "bio": user_row[4],
            "company": user_row[5],
            "repo_count": repo_count,
            "languages": langs,
            "topics": topics,
        }
