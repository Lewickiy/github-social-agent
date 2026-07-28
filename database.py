import re
import sqlite3
from datetime import datetime, timezone
from config import (
    DATABASE,
    CURRENT_SCORE_VERSION,
    REPO_FRESHNESS_DAYS,
    SCORE_FRESHNESS_DAYS,
)

# ── Company parser (pure function, no external dependencies) ──
_COMPANY_RE = re.compile(r"@([a-zA-Z0-9_-]+)")


def extract_companies(text):
    """Extract @-prefixed GitHub organisation logins from *text*.

    Returns a list of lowercase login strings.
    Returns an empty list if *text* is None or contains no @-mentions.
    """
    if not text:
        return []
    return [m.group(1).lower() for m in _COMPANY_RE.finditer(text)]


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

        The owner and deleted users are excluded.
        """
        rows = self.conn.execute(
            """
            SELECT u.username FROM users u
            WHERE u.owner = 0
              AND u.status != 'DELETED'
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

    def users_for_silent_processing(self):
        """Return users eligible for silent-mode collection + scoring.

        Excludes:
          - the owner,
          - deleted users,
          - users whose repos were fetched within REPO_FRESHNESS_DAYS,
          - users already scored with the current algorithm version
            within SCORE_FRESHNESS_DAYS.

        Ordered by:
          1. Owner followers (discovered_from = 'owner_followers') — priority,
          2. Then by followers DESC (popular users first).
        """
        rows = self.conn.execute(
            """
            SELECT u.username
            FROM users u
            WHERE u.owner = 0
              AND u.status != 'DELETED'
              AND (
                  u.repos_fetched_at IS NULL
                  OR u.repos_fetched_at < datetime('now', ?)
              )
              AND NOT (
                  u.score_version = ?
                  AND u.scored_at IS NOT NULL
                  AND u.scored_at >= datetime('now', ?)
              )
            ORDER BY
                CASE WHEN u.discovered_from = 'owner_followers' THEN 0 ELSE 1 END,
                u.followers DESC
            """,
            (
                f"-{REPO_FRESHNESS_DAYS} days",
                CURRENT_SCORE_VERSION,
                f"-{SCORE_FRESHNESS_DAYS} days",
            ),
        ).fetchall()
        return rows

    def users_for_repo_collection(self):
        """Return users that need repo re-collection.

        Includes users whose repos_fetched_at is NULL (never collected)
        or older than REPO_FRESHNESS_DAYS.  Excludes deleted users.
        """
        rows = self.conn.execute(
            """
            SELECT username FROM users
            WHERE status != 'DELETED'
              AND (
                  repos_fetched_at IS NULL
                  OR repos_fetched_at < datetime('now', ?)
              )
            ORDER BY repos_fetched_at ASC NULLS FIRST
            """,
            (f"-{REPO_FRESHNESS_DAYS} days",),
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

    def mark_followback(self, username):
        """Mark a user as FOLLOWBACK — they followed us after we followed them."""
        self.conn.execute(
            "UPDATE users SET status = 'FOLLOWBACK' WHERE username = ?",
            (username,),
        )
        self.conn.commit()

    # --------------------------------------------------
    # Cached profile info (avoid redundant API calls)
    # --------------------------------------------------

    def get_user_cached_info(self, username):
        """Return cached user profile from the users table, or None."""
        row = self.conn.execute(
            """
            SELECT username, public_repos, followers, bio, company, score, scored_at, status
            FROM users WHERE username = ?
            """,
            (username,),
        ).fetchone()

        if not row:
            return None

        return {
            "login": row[0],
            "public_repos": row[1] or 0,
            "followers": row[2] or 0,
            "bio": row[3],
            "company": row[4],
            "score": row[5],
            "scored_at": row[6],
            "status": row[7],
        }

    # --------------------------------------------------
    # Optimisation helpers — freshness / TTL
    # --------------------------------------------------

    def mark_repos_fetched(self, username):
        """Record that repos for *username* were fetched right now."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE users SET repos_fetched_at = ? WHERE username = ?",
            (now, username),
        )
        self.conn.commit()

    def is_repos_fresh(self, username, days=None):
        """Return True if repos for *username* were fetched within *days*."""
        if days is None:
            days = REPO_FRESHNESS_DAYS
        row = self.conn.execute(
            """
            SELECT repos_fetched_at FROM users
            WHERE username = ?
              AND repos_fetched_at IS NOT NULL
              AND repos_fetched_at >= datetime('now', ?)
            """,
            (username, f"-{days} days"),
        ).fetchone()
        return row is not None

    # --------------------------------------------------
    # Followers count (incremental scan detection)
    # --------------------------------------------------

    def get_followers_count(self, username):
        """Return the stored follower count, or 0 if unknown."""
        row = self.conn.execute(
            "SELECT followers_count FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return (row[0] or 0) if row else 0

    def store_followers_count(self, username, count):
        """Persist the current follower count and scan timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE users
            SET followers_count = ?,
                followers_scanned_at = ?
            WHERE username = ?
            """,
            (count, now, username),
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
        """Insert or update a repository record.  Returns (repo_id, changed).

        *changed* is True when the row was inserted or at least one field
        was actually updated; False when everything is already up-to-date.
        """
        now = datetime.now(timezone.utc).isoformat()
        gh_id = repo_data["id"]

        cur = self.conn.execute(
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
            WHERE name                  IS NOT excluded.name
               OR description           IS NOT excluded.description
               OR html_url              IS NOT excluded.html_url
               OR stars                 != excluded.stars
               OR forks                 != excluded.forks
               OR watchers              != excluded.watchers
               OR is_fork               != excluded.is_fork
               OR is_archived           != excluded.is_archived
               OR default_branch        IS NOT excluded.default_branch
               OR repository_updated_at IS NOT excluded.repository_updated_at
            """,
            (
                gh_id,
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
        changed = cur.rowcount > 0
        self.conn.commit()

        row = self.conn.execute(
            "SELECT id FROM repositories WHERE github_repository_id = ?",
            (gh_id,),
        ).fetchone()
        return row[0], changed

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
        """Save language weights for a repository.  Returns True if changed.

        *language_bytes* is a dict like {"Java": 150000, "Python": 50000}.
        Weights are stored as percentages (0-100, float).
        Skips the write entirely if the stored data already matches.
        """
        total = sum(language_bytes.values())
        if total == 0:
            return False

        # Build the new set of {lang_name: weight}
        new_langs = {
            name: round((bytes_ / total) * 100, 2)
            for name, bytes_ in language_bytes.items()
        }

        # Compare with existing data
        existing = self.conn.execute(
            """
            SELECT l.name, rl.weight
            FROM repository_languages rl
            JOIN languages l ON l.id = rl.language_id
            WHERE rl.repository_id = ?
            """,
            (repository_id,),
        ).fetchall()

        existing_map = {name: weight for name, weight in existing}
        if existing_map == new_langs:
            return False

        # Clear old entries and insert new
        self.conn.execute(
            "DELETE FROM repository_languages WHERE repository_id = ?",
            (repository_id,),
        )

        for lang_name, weight in new_langs.items():
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
        return True

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
        """Save topics for a repository.  Returns True if changed.

        *topics* is a list of topic strings from the GitHub API.
        Skips the write entirely if the stored data already matches.
        """
        new_set = set(topics)

        existing = self.conn.execute(
            """
            SELECT t.name
            FROM repository_topics rt
            JOIN topics t ON t.id = rt.topic_id
            WHERE rt.repository_id = ?
            """,
            (repository_id,),
        ).fetchall()

        existing_set = {row[0] for row in existing}
        if existing_set == new_set:
            return False

        # Clear old entries and insert new
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
        return True

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

    # --------------------------------------------------
    # Companies
    # --------------------------------------------------

    def extract_and_save_companies(self, username, company_text):
        """Parse @-mentions from *company_text* and persist companies + links.

        For each @login found:
          1. INSERT OR IGNORE into companies (login only, api_fetched_at=NULL).
          2. INSERT OR IGNORE into user_companies.

        The background worker later enriches company data via the API.
        """
        logins = extract_companies(company_text)
        if not logins:
            return

        now = datetime.now(timezone.utc).isoformat()
        for login in logins:
            self.conn.execute(
                "INSERT OR IGNORE INTO companies (login, created_at) VALUES (?, ?)",
                (login, now),
            )
            comp_id = self.conn.execute(
                "SELECT id FROM companies WHERE login = ?", (login,)
            ).fetchone()
            if comp_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO user_companies (username, company_id) VALUES (?, ?)",
                    (username, comp_id[0]),
                )
        self.conn.commit()

    def get_next_company_to_fetch(self):
        """Return the next company that needs API enrichment.

        Priority:
          1. Companies never fetched (api_fetched_at IS NULL).
          2. Companies whose github_updated_at > updated_at (stale).
          3. Oldest api_fetched_at.

        Returns (id, login, updated_at) or None.
        """
        row = self.conn.execute(
            """
            SELECT id, login, updated_at FROM companies
            WHERE api_fetched_at IS NULL
               OR (github_updated_at IS NOT NULL
                   AND updated_at IS NOT NULL
                   AND github_updated_at > updated_at)
            ORDER BY api_fetched_at ASC NULLS FIRST
            LIMIT 1
            """
        ).fetchone()
        return row

    def update_company_data(self, company_id, api_data):
        """Save full GitHub API response for a company."""
        now = datetime.now(timezone.utc).isoformat()
        gh_updated = api_data.get("updated_at")

        self.conn.execute(
            """
            UPDATE companies SET
                github_id         = ?,
                name              = ?,
                description       = ?,
                html_url          = ?,
                blog              = ?,
                location          = ?,
                email             = ?,
                twitter_username  = ?,
                public_repos      = ?,
                followers         = ?,
                following         = ?,
                avatar_url        = ?,
                company_type      = ?,
                github_created_at = ?,
                github_updated_at = ?,
                updated_at        = ?,
                api_fetched_at    = ?
            WHERE id = ?
            """,
            (
                api_data.get("id"),
                api_data.get("name"),
                api_data.get("bio") or api_data.get("description"),
                api_data.get("html_url"),
                api_data.get("blog"),
                api_data.get("location"),
                api_data.get("email"),
                api_data.get("twitter_username"),
                api_data.get("public_repos", 0),
                api_data.get("followers", 0),
                api_data.get("following", 0),
                api_data.get("avatar_url"),
                api_data.get("type"),
                api_data.get("created_at"),
                gh_updated,
                now,
                now,
                company_id,
            ),
        )
        self.conn.commit()

    def mark_company_fetched(self, company_id):
        """Mark a company as fetched (even if no data was returned).

        Prevents infinite retries for non-existent organisations.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE companies SET api_fetched_at = ? WHERE id = ?",
            (now, company_id),
        )
        self.conn.commit()

    def get_user_companies(self, username):
        """Return list of company logins linked to *username*."""
        rows = self.conn.execute(
            """
            SELECT c.login, c.name, c.company_type
            FROM user_companies uc
            JOIN companies c ON c.id = uc.company_id
            WHERE uc.username = ?
            ORDER BY c.login
            """,
            (username,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

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

        companies = self.get_user_companies(username)

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
            "companies": companies,
        }
