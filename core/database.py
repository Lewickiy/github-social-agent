import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from core.config import (
    DATABASE,
    CURRENT_SCORE_VERSION,
    REPO_FRESHNESS_DAYS,
    SCORE_FRESHNESS_DAYS,
    SILENT_REPO_CHECK_FRESHNESS_DAYS,
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

    def store_full_profile(self, username, api_data):
        """Cache the full GitHub API user response as JSON.

        Called after every ``github.user(username)`` call so that the ML
        feature extractor has access to all profile fields (following,
        created_at, blog, location, email, etc.) without extra API calls.
        """
        if api_data is None:
            return
        self.conn.execute(
            "UPDATE users SET github_profile_json = ? WHERE username = ?",
            (json.dumps(api_data, ensure_ascii=False), username),
        )
        self.conn.commit()

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

    def users_for_silent_processing(self, prioritize_small=False):
        """Return users eligible for silent-mode collection + scoring.

        Excludes:
          - the owner,
          - deleted users,
          - users whose repos were fetched within REPO_FRESHNESS_DAYS,
          - users already scored with the current algorithm version
            within SCORE_FRESHNESS_DAYS.

        Ordered by:
          1. Owner followers (discovered_from = 'owner_followers') — priority,
          2. Then by followers ASC/DESC depending on *prioritize_small*.
             False (default) = popular first (richer data, better scoring);
             True              = small accounts first (more follow-backs).
        """
        direction = "ASC" if prioritize_small else "DESC"
        rows = self.conn.execute(
            f"""
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
                u.followers {direction}
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
            SELECT username, public_repos, followers, bio, company,
                   score, scored_at, status, github_profile_json
            FROM users WHERE username = ?
            """,
            (username,),
        ).fetchone()

        if not row:
            return None

        result = {
            "login": row[0],
            "public_repos": row[1] or 0,
            "followers": row[2] or 0,
            "bio": row[3],
            "company": row[4],
            "score": row[5],
            "scored_at": row[6],
            "status": row[7],
        }

        # If we have a cached full profile, merge the extra fields
        if row[8]:
            try:
                full = json.loads(row[8])
                # Merge fields that aren't already in the cached info
                for key in (
                    "following", "public_gists", "blog", "location",
                    "email", "hireable", "name", "type",
                    "twitter_username", "created_at",
                ):
                    if key in full:
                        result[key] = full[key]
            except (json.JSONDecodeError, TypeError):
                pass

        return result

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

    def is_repo_fresh(self, repo_id, days=None):
        """Return True if *repo_id* was checked within *days*.

        Per-repo TTL used by silent mode to skip the per-repo
        ``/languages`` API call when the repo was already verified
        recently.  A repo with `last_checked_at IS NULL` is treated
        as fresh = False (never checked), so it will always be fetched.
        """
        if days is None:
            days = SILENT_REPO_CHECK_FRESHNESS_DAYS
        row = self.conn.execute(
            """
            SELECT 1 FROM repositories
            WHERE id = ?
              AND last_checked_at IS NOT NULL
              AND last_checked_at >= datetime('now', ?)
            """,
            (repo_id, f"-{days} days"),
        ).fetchone()
        return row is not None

    def mark_repo_checked(self, repo_id):
        """Stamp `last_checked_at = now` for a single repo.

        Called by silent mode after a repo has been processed
        (languages/topics saved or determined to be unchanged) so the
        per-repo TTL window starts fresh.  Must only be called after
        a successful round-trip — calling it on a rate-limited path
        would postpone the real fetch by the whole TTL window.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE repositories SET last_checked_at = ? WHERE id = ?",
            (now, repo_id),
        )
        self.conn.commit()

    # --------------------------------------------------
    # ETag cache (conditional requests — 304 is free)
    # --------------------------------------------------

    def get_repos_etag(self, username):
        """Return the cached ETag for a user's repo list, or None."""
        row = self.conn.execute(
            "SELECT repos_etag FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return row[0] if row else None

    def set_repos_etag(self, username, etag):
        """Persist the ETag of the last ``/users/{user}/repos`` response."""
        self.conn.execute(
            "UPDATE users SET repos_etag = ? WHERE username = ?",
            (etag, username),
        )
        self.conn.commit()

    def get_languages_etag(self, repo_id):
        """Return the cached ETag for a repo's language breakdown, or None."""
        row = self.conn.execute(
            "SELECT languages_etag FROM repositories WHERE id = ?",
            (repo_id,),
        ).fetchone()
        return row[0] if row else None

    def set_languages_etag(self, repo_id, etag):
        """Persist the ETag of the last ``/repos/{owner}/{repo}/languages`` response."""
        self.conn.execute(
            "UPDATE repositories SET languages_etag = ? WHERE id = ?",
            (etag, repo_id),
        )
        self.conn.commit()

    def repos_needing_languages(self, username, skip_forks=True):
        """Return ``(id, name)`` rows of repos whose languages were never fetched.

        Used by the repos-304 fast-path: when the repo list is unchanged we
        still backfill language data for repos missed in a previous aborted
        run (``last_checked_at IS NULL``), so scoring data stays complete.
        Forks are excluded when *skip_forks* is True (their languages are
        intentionally never fetched — SKIP_FORK_LANGUAGES).
        """
        if skip_forks:
            rows = self.conn.execute(
                """
                SELECT id, name FROM repositories
                WHERE user_id = ? AND last_checked_at IS NULL AND is_fork = 0
                """,
                (username,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id, name FROM repositories
                WHERE user_id = ? AND last_checked_at IS NULL
                """,
                (username,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
    # Historical snapshots (daily mutable-profile data)
    # --------------------------------------------------

    def store_user_snapshot(self, username, followers=None, following=None,
                            public_repos=None, public_gists=None, extra=None):
        """Record today's snapshot for *username* (upsert per day).

        At most one row per (username, snapshot_date) — re-running the
        snapshot on the same day updates that row.  Works for any user,
        so snapshots can later be accumulated for everyone, not just the
        owner.

        *extra* is an optional dict of other mutable profile fields
        (name, company, bio, location, …) stored as JSON for future
        analytics.

        ``snapshot_date`` is the **UTC** date so it matches the rest of
        the codebase (Docker containers default to UTC — "noon" is
        12:00 UTC there).
        """
        today = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO user_snapshots (
                username, snapshot_date, followers_count, following_count,
                public_repos_count, public_gists_count, extra_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, snapshot_date) DO UPDATE SET
                followers_count    = COALESCE(excluded.followers_count,
                                             user_snapshots.followers_count),
                following_count    = COALESCE(excluded.following_count,
                                             user_snapshots.following_count),
                public_repos_count = COALESCE(excluded.public_repos_count,
                                             user_snapshots.public_repos_count),
                public_gists_count = COALESCE(excluded.public_gists_count,
                                             user_snapshots.public_gists_count),
                extra_json         = COALESCE(excluded.extra_json,
                                             user_snapshots.extra_json),
                updated_at         = excluded.updated_at
            """,
            (
                username, today, followers, following, public_repos,
                public_gists, json.dumps(extra, ensure_ascii=False) if extra else None,
                now, now,
            ),
        )
        self.conn.commit()

    def get_user_snapshots(self, username, since_date=None):
        """Return daily snapshots for *username* (oldest first).

        ``since_date`` is an ISO date string (YYYY-MM-DD); None = all.
        Returns a list of dicts.
        """
        sql = (
            "SELECT snapshot_date, followers_count, following_count, "
            "public_repos_count, public_gists_count, extra_json "
            "FROM user_snapshots WHERE username = ?"
        )
        params = [username]
        if since_date:
            sql += " AND snapshot_date >= ?"
            params.append(since_date)
        sql += " ORDER BY snapshot_date"
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            extra = None
            if r[5]:
                try:
                    extra = json.loads(r[5])
                except (json.JSONDecodeError, TypeError):
                    extra = None
            out.append({
                "date": r[0],
                "followers_count": r[1],
                "following_count": r[2],
                "public_repos_count": r[3],
                "public_gists_count": r[4],
                "extra": extra,
            })
        return out

    # --------------------------------------------------
    # GitHub API usage meter
    # --------------------------------------------------

    def record_api_request(self, endpoint, status_code=None):
        """Append one GitHub API round-trip to the request log.

        Called from github_client on every real HTTP call to api.github.com
        (including 304s) so the dashboard can show a live per-hour request
        rate.  Uses UTC ISO timestamps, consistent with the rest of the DB.
        """
        self.conn.execute(
            "INSERT INTO github_api_requests (endpoint, status_code, created_at) "
            "VALUES (?, ?, ?)",
            (endpoint, status_code, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def prune_api_requests(self, older_than_hours=48):
        """Delete request-log rows older than *older_than_hours*.

        Keeps the table small — the per-hour meter only ever needs the last
        few hours of data.  Called opportunistically by the API server.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        ).isoformat()
        self.conn.execute(
            "DELETE FROM github_api_requests WHERE created_at < ?",
            (cutoff,),
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

    def get_next_queued_user(self, threshold):
        """Return the earliest NEW user with score >= threshold (FIFO).

        Ordered by ``created_at ASC`` so the user who entered the system
        first gets followed first.  Returns ``(username, score)`` or None.
        """
        row = self.conn.execute(
            """
            SELECT username, score FROM users
            WHERE status = 'NEW'
              AND score >= ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (threshold,),
        ).fetchone()
        return row

    def get_mutual_follow_users(self):
        """Return usernames for all confirmed mutual-follow users (FOLLOWBACK).

        These are users who followed us back after we followed them.
        """
        rows = self.conn.execute(
            """
            SELECT username FROM users
            WHERE status = 'FOLLOWBACK'
            ORDER BY followed_at ASC
            """
        ).fetchall()
        return [row[0] for row in rows]

    def mark_unfollowed_after_mutual(self, username):
        """Mark a user who unfollowed us after a mutual follow.

        Sets status to ``UNFOLLOWED_AFTER_MUTUAL_FOLLOW``.
        Does not overwrite ``followed_at`` (preserves original follow date).
        """
        self.conn.execute(
            """
            UPDATE users
            SET status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'
            WHERE username = ?
            """,
            (username,),
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

    # --------------------------------------------------
    # ML — training data & predictions
    # --------------------------------------------------

    def get_user_profile_json(self, username):
        """Return the cached full GitHub API response for *username*, or None."""
        row = self.conn.execute(
            "SELECT github_profile_json FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def get_training_users(self):
        """Return users for ML training with their labels.

        Positive examples (label=1): users with status FOLLOWBACK.
        Negative examples (label=0):
          - FOLLOWED for more than 7 days (no mutual follow),
          - UNFOLLOWED_AFTER_MUTUAL_FOLLOW.

        Only includes users who have been scored and have repo data.

        Returns list of (username, label).
        """
        rows = self.conn.execute(
            """
            SELECT username,
                   CASE
                       WHEN status = 'FOLLOWBACK' THEN 1
                       ELSE 0
                   END AS label
            FROM users
            WHERE (
                status = 'FOLLOWBACK'
                OR (
                    status = 'FOLLOWED'
                    AND followed_at IS NOT NULL
                    AND followed_at < datetime('now', '-7 days')
                )
                OR status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'
            )
            AND score IS NOT NULL
            AND score > 0
            AND EXISTS (
                SELECT 1 FROM repositories WHERE user_id = users.username
            )
            ORDER BY username
            """
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def get_user_repo_aggregates(self, username):
        """Return aggregated repository statistics for *username*.

        Returns a dict with keys: repo_count, sum_stars, avg_stars,
        max_stars, sum_forks, avg_forks, max_forks, avg_watchers,
        archived_count, fork_count, avg_repo_age_days,
        days_since_last_push, has_description_ratio.
        All values are 0 if the user has no repos.
        """
        row = self.conn.execute(
            """
            SELECT
                COUNT(*)                                                   AS repo_count,
                COALESCE(SUM(stars), 0)                                   AS sum_stars,
                COALESCE(AVG(stars), 0)                                   AS avg_stars,
                COALESCE(MAX(stars), 0)                                   AS max_stars,
                COALESCE(SUM(forks), 0)                                   AS sum_forks,
                COALESCE(AVG(forks), 0)                                   AS avg_forks,
                COALESCE(MAX(forks), 0)                                   AS max_forks,
                COALESCE(AVG(watchers), 0)                                AS avg_watchers,
                COALESCE(SUM(CASE WHEN is_archived THEN 1 ELSE 0 END), 0) AS archived_count,
                COALESCE(SUM(CASE WHEN is_fork THEN 1 ELSE 0 END), 0)     AS fork_count,
                COALESCE(
                    AVG(
                        julianday('now') - julianday(repository_created_at)
                    ), 0
                )                                                          AS avg_repo_age_days,
                COALESCE(
                    julianday('now') - julianday(MAX(repository_updated_at)),
                    9999
                )                                                          AS days_since_last_push,
                COALESCE(
                    CAST(SUM(CASE WHEN description IS NOT NULL AND description != '' THEN 1 ELSE 0 END) AS REAL)
                    / NULLIF(COUNT(*), 0), 0
                )                                                          AS has_description_ratio
            FROM repositories
            WHERE user_id = ?
            """,
            (username,),
        ).fetchone()

        if not row or row[0] == 0:
            return {
                "repo_count": 0, "sum_stars": 0, "avg_stars": 0,
                "max_stars": 0, "sum_forks": 0, "avg_forks": 0,
                "max_forks": 0, "avg_watchers": 0, "archived_count": 0,
                "fork_count": 0, "avg_repo_age_days": 0,
                "days_since_last_push": 9999, "has_description_ratio": 0,
            }

        return {
            "repo_count": row[0],
            "sum_stars": row[1],
            "avg_stars": row[2],
            "max_stars": row[3],
            "sum_forks": row[4],
            "avg_forks": row[5],
            "max_forks": row[6],
            "avg_watchers": row[7],
            "archived_count": row[8],
            "fork_count": row[9],
            "avg_repo_age_days": row[10],
            "days_since_last_push": row[11],
            "has_description_ratio": row[12],
        }

    def get_global_language_frequencies(self, top_n):
        """Return the *top_n* most-used languages across all repos.

        Returns list of (language_name, frequency).
        """
        rows = self.conn.execute(
            """
            SELECT l.name, COUNT(DISTINCT rl.repository_id) AS freq
            FROM repository_languages rl
            JOIN languages l ON l.id = rl.language_id
            GROUP BY l.name
            ORDER BY freq DESC
            LIMIT ?
            """,
            (top_n,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def get_global_topic_frequencies(self, top_n):
        """Return the *top_n* most-used topics across all repos.

        Returns list of (topic_name, frequency).
        """
        rows = self.conn.execute(
            """
            SELECT t.name, COUNT(DISTINCT rt.repository_id) AS freq
            FROM repository_topics rt
            JOIN topics t ON t.id = rt.topic_id
            GROUP BY t.name
            ORDER BY freq DESC
            LIMIT ?
            """,
            (top_n,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def save_ml_prediction(self, username, prediction):
        """Store the ML model's prediction (0 or 1) for *username*."""
        self.conn.execute(
            "UPDATE users SET ml_follow_prediction = ? WHERE username = ?",
            (prediction, username),
        )
        self.conn.commit()

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

    # --------------------------------------------------
    # Job runs (dashboard)
    # --------------------------------------------------

    def create_job_run(self, mode):
        """Insert a new job_run row.  Returns the new job id."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO job_runs (mode, status, created_at) VALUES (?, 'PENDING', ?)",
            (mode, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_job_running(self, job_id, pid):
        """Mark a job as RUNNING with its OS pid."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE job_runs SET status = 'RUNNING', pid = ?, started_at = ? WHERE id = ?",
            (pid, now, job_id),
        )
        self.conn.commit()

    def finish_job(self, job_id, exit_code, error=None):
        """Mark a job as SUCCESS or FAILED based on its exit code."""
        now = datetime.now(timezone.utc).isoformat()
        status = "SUCCESS" if exit_code == 0 else "FAILED"
        self.conn.execute(
            """
            UPDATE job_runs
            SET status = ?, finished_at = ?, exit_code = ?, error = ?
            WHERE id = ?
            """,
            (status, now, exit_code, error, job_id),
        )
        self.conn.commit()

    def _job_row_to_dict(self, row):
        """Map a job_runs row to a dict.

        Column names are read once per connection via PRAGMA table_info so
        the mapping stays in sync with the schema even if a future migration
        adds a column to job_runs.
        """
        if not hasattr(self, "_job_columns"):
            self._job_columns = [
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(job_runs)"
                ).fetchall()
            ]
        return dict(zip(self._job_columns, row))

    def get_job(self, job_id):
        """Return a single job_run row as a dict, or None."""
        row = self.conn.execute(
            "SELECT * FROM job_runs WHERE id = ?", (job_id,),
        ).fetchone()
        return self._job_row_to_dict(row) if row else None

    def list_jobs(self, limit=50):
        """Return the most recent job runs, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [self._job_row_to_dict(r) for r in rows]

    def running_jobs(self):
        """Return all job runs currently in RUNNING/PENDING state."""
        rows = self.conn.execute(
            "SELECT * FROM job_runs WHERE status IN ('RUNNING', 'PENDING')"
        ).fetchall()
        return [self._job_row_to_dict(r) for r in rows]
