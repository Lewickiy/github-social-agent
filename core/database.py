import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from core.config import (
    DATABASE,
    CURRENT_SCORE_VERSION,
    ML_FOLLOW_GATE_ENABLED,
    ML_FOLLOW_THRESHOLD,
    REPO_FRESHNESS_DAYS,
    SCORE_FRESHNESS_DAYS,
    SILENT_REPO_CHECK_FRESHNESS_DAYS,
)
from core.tz import get_timezone, local_midnight_utc

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
        # Several bot threads write to the same DB concurrently (parallel
        # silent workers + background workers).  Without a busy timeout,
        # a write collision on WAL raises "database is locked" instantly.
        self.conn.execute("PRAGMA busy_timeout=5000")

    # --------------------------------------------------
    # App settings (key/value user preferences)
    # --------------------------------------------------

    def get_setting(self, key, default=None):
        """Return the value of app setting *key*, or *default* when unset."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,),
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        """Upsert app setting *key* → *value*."""
        self.conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        self.conn.commit()

    # --------------------------------------------------
    # Worker status — dashboard toggle + liveness per worker
    # --------------------------------------------------
    # One row per background worker (workers/*.py).  ``enabled`` is the
    # Management-tab pause/resume toggle; started/stopped/last action/last
    # error/heartbeat drive the worker-activity card.  All methods degrade
    # gracefully when the worker_status table does not exist yet (migrations
    # not applied): status writes become no-ops and ``worker_enabled``
    # defaults to True, so the bot never breaks on an un-migrated database.

    def _worker_status_cols(self):
        """Column names of worker_status (schema sync like _job_row_to_dict).

        Only the *found* result is cached — a process that starts before
        the migration runs re-checks on every call (a cheap sqlite_master
        lookup) and picks the table up as soon as it exists.
        """
        if not hasattr(self, "_worker_status_columns"):
            self._worker_status_columns = None
        if self._worker_status_columns is None and self._table_exists("worker_status"):
            self._worker_status_columns = [
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(worker_status)"
                ).fetchall()
            ]
        return self._worker_status_columns

    def _update_worker_status(self, key, **fields):
        """Upsert worker *key*'s row setting the given columns.

        No-op when the worker_status table does not exist (pre-migration),
        so workers and the API keep working on older databases.
        """
        if not self._worker_status_cols():
            return
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
        self.conn.execute(
            f"INSERT INTO worker_status (name, {cols}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(name) DO UPDATE SET {updates}",
            (key, *fields.values()),
        )
        self.conn.commit()

    def worker_enabled(self, key, default=True):
        """True when worker *key* is toggled on (default when table/row missing)."""
        if not self._worker_status_cols():
            return default
        row = self.conn.execute(
            "SELECT enabled FROM worker_status WHERE name = ?", (key,),
        ).fetchone()
        return bool(row[0]) if row else default

    def set_worker_enabled(self, key, enabled):
        """Persist the Management-tab toggle for worker *key*."""
        self._update_worker_status(key, enabled=1 if enabled else 0)

    def mark_worker_started(self, key):
        """Record that worker *key* just started (clears the stopped marker)."""
        now = datetime.now(timezone.utc).isoformat()
        self._update_worker_status(
            key, started_at=now, stopped_at=None, heartbeat_at=now,
        )

    def mark_worker_stopped(self, key):
        """Record that worker *key* stopped cleanly (heartbeat cleared → offline)."""
        now = datetime.now(timezone.utc).isoformat()
        self._update_worker_status(key, stopped_at=now, heartbeat_at=None)

    def mark_worker_action(self, key):
        """Record that worker *key* just completed a unit of work."""
        now = datetime.now(timezone.utc).isoformat()
        self._update_worker_status(key, last_action_at=now)

    def mark_worker_error(self, key, error):
        """Record the last error of worker *key* (message + timestamp)."""
        now = datetime.now(timezone.utc).isoformat()
        self._update_worker_status(key, last_error_at=now, last_error=error)

    def touch_worker_heartbeat(self, key):
        """Record a liveness tick for worker *key*."""
        now = datetime.now(timezone.utc).isoformat()
        self._update_worker_status(key, heartbeat_at=now)

    def worker_status(self, key):
        """Return worker *key*'s row as a dict, or {} when absent."""
        cols = self._worker_status_cols()
        if not cols:
            return {}
        row = self.conn.execute(
            "SELECT * FROM worker_status WHERE name = ?", (key,),
        ).fetchone()
        return dict(zip(cols, row)) if row else {}

    def worker_statuses(self):
        """Return all worker_status rows as dicts (empty pre-migration)."""
        cols = self._worker_status_cols()
        if not cols:
            return []
        rows = self.conn.execute("SELECT * FROM worker_status").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # --------------------------------------------------
    # Users
    # --------------------------------------------------

    def add_user(self, username, source, owner=False):
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO users
                (username, discovered_from, created_at, owner)
            VALUES (?, ?, ?, ?)
            """,
            (username, source, datetime.now(timezone.utc).isoformat(), 1 if owner else 0),
        )
        if cur.rowcount:
            # Initial transition: NEW at discovery time (keeps the
            # user_status_history log complete for users added after the
            # 020 backfill ran).  INSERT OR IGNORE means an existing user
            # (re-discovery) is skipped — their NEW entry already exists.
            self._log_status_change(username, "NEW")
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

    def owner_repository_names(self):
        """Return the owner's repository *short* names (from ``repositories``).

        Used by the unfollow worker to decide whether a user interacted
        with any of the owner's repos (GitHub events carry ``owner/repo``
        full names, built from these).  Empty when the owner has no repos
        collected yet — callers fall back to a live API fetch.
        """
        owner = self.get_owner()
        if not owner:
            return []
        rows = self.conn.execute(
            "SELECT name FROM repositories WHERE user_id = ?",
            (owner,),
        ).fetchall()
        return [r[0] for r in rows]

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
        # NULL scores (users without collected repos) are excluded — they
        # carry no meaningful ranking.  "Current status = NEW" (no
        # lifecycle transition yet) is derived from user_status_history —
        # the single store of statuses.
        rows = self.conn.execute(
            """
            SELECT u.username, u.score
            FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE COALESCE(c.status, 'NEW') = 'NEW'
              AND u.score IS NOT NULL
            ORDER BY u.score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows

    def unscored_users(self):
        """Return usernames that need (re-)scoring.

        Only users whose repositories were actually collected
        (``repos_fetched_at IS NOT NULL``) are eligible — a score computed
        without repo data is untrustworthy, so users still awaiting repo
        collection are left for the silent-mode queue instead of being
        scored blind.

        A user needs scoring when any of these is true:
          - never scored  (score = 0 AND scored_at IS NULL),  OR
          - score_version is behind the current algorithm version,  OR
          - has no repository rows (0 public repos),  OR
          - repos were collected AFTER the last scoring (stale score).

        The owner and deleted users are excluded.
        """
        rows = self.conn.execute(
            """
            SELECT u.username FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE u.owner = 0
              AND COALESCE(c.status, 'NEW') != 'DELETED'
              AND u.repos_fetched_at IS NOT NULL
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
            within SCORE_FRESHNESS_DAYS **and whose repos were actually
            collected** (repos_fetched_at IS NOT NULL).  A score stamped
            without repo data (repos_fetched_at IS NULL) is not
            trustworthy, so such users stay in the queue.

        Ordered by:
          1. Owner followers (discovered_from = 'owner_followers') — priority,
          2. Then by followers ASC/DESC depending on *prioritize_small*.
             False (default) = popular first (richer data, better scoring);
             True              = small accounts first (more follow-backs).
        """
        direction = "ASC" if prioritize_small else "DESC"
        # "Not deleted" via the materialised user_current_status table
        # (NOT IN over the tiny deleted set) so the wide users rows are
        # never scanned for the join.
        rows = self.conn.execute(
            f"""
            SELECT u.username
            FROM users u
            WHERE u.owner = 0
              AND u.username NOT IN (
                  SELECT username FROM user_current_status WHERE status = 'DELETED'
              )
              AND (
                  u.repos_fetched_at IS NULL
                  OR u.repos_fetched_at < datetime('now', ?)
              )
              AND NOT (
                  u.score_version = ?
                  AND u.scored_at IS NOT NULL
                  AND u.scored_at >= datetime('now', ?)
                  AND u.repos_fetched_at IS NOT NULL
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

    def count_silent_processing_queue(self):
        """Count users awaiting silent-mode collection + scoring.

        Mirrors the WHERE clause of :meth:`users_for_silent_processing`
        so the dashboard's "awaiting processing" KPI matches exactly the
        queue the silent runner will work through.
        """
        row = self.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM users u
            WHERE u.owner = 0
              AND u.username NOT IN (
                  SELECT username FROM user_current_status WHERE status = 'DELETED'
              )
              AND (
                  u.repos_fetched_at IS NULL
                  OR u.repos_fetched_at < datetime('now', ?)
              )
              AND NOT (
                  u.score_version = ?
                  AND u.scored_at IS NOT NULL
                  AND u.scored_at >= datetime('now', ?)
                  AND u.repos_fetched_at IS NOT NULL
              )
            """,
            (
                f"-{REPO_FRESHNESS_DAYS} days",
                CURRENT_SCORE_VERSION,
                f"-{SCORE_FRESHNESS_DAYS} days",
            ),
        ).fetchone()
        return row[0]

    def users_for_repo_collection(self):
        """Return users that need repo re-collection.

        Includes users whose repos_fetched_at is NULL (never collected)
        or older than REPO_FRESHNESS_DAYS.  Excludes deleted users.
        """
        rows = self.conn.execute(
            """
            SELECT u.username FROM users u
            WHERE u.username NOT IN (
                SELECT username FROM user_current_status WHERE status = 'DELETED'
            )
              AND (
                  u.repos_fetched_at IS NULL
                  OR u.repos_fetched_at < datetime('now', ?)
              )
            ORDER BY u.repos_fetched_at ASC NULLS FIRST
            """,
            (f"-{REPO_FRESHNESS_DAYS} days",),
        ).fetchall()
        return rows

    # --------------------------------------------------
    # Activity feed — one row per lifecycle event
    # --------------------------------------------------

    def _log_action(self, username, action):
        """Append one event to the actions feed (UTC timestamped).

        The feed powers the Overview "Recent activity" block.  Only
        lifecycle *transitions* are logged (guarded by the caller's
        ``_append_status`` guard), so each user produces at most one row
        per event type.
        """
        self.conn.execute(
            "INSERT INTO actions (username, action, created_at) VALUES (?, ?, ?)",
            (username, action, datetime.now(timezone.utc).isoformat()),
        )

    def _upsert_current_status(self, username, status, changed_at, followed_at=None):
        """Maintain the denormalised ``user_current_status`` row for *username*.

        ``user_status_history`` stays the source of truth; this table is
        its incremental projection so dashboard reads don't recompute the
        latest transition.  ``followed_at`` (time of the last FOLLOWED
        transition) is only set by a FOLLOWED append and is preserved
        across later transitions.
        """
        self.conn.execute(
            """
            INSERT INTO user_current_status (username, status, changed_at, followed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                status     = excluded.status,
                changed_at = excluded.changed_at,
                followed_at = COALESCE(excluded.followed_at,
                                       user_current_status.followed_at)
            """,
            (username, status, changed_at, followed_at),
        )

    def _log_status_change(self, username, status, changed_at=None):
        """Append one row to the ``user_status_history`` transition log.

        Records that *username* entered *status* at *changed_at* (defaults
        to now, UTC).  Used for the initial NEW transition in :meth:`add_user`;
        later transitions go through :meth:`_append_status` (which guards
        against duplicates).  Also updates the ``user_current_status``
        projection table.
        """
        changed_at = changed_at or datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO user_status_history (username, status, changed_at) "
            "VALUES (?, ?, ?)",
            (username, status, changed_at),
        )
        self._upsert_current_status(
            username, status, changed_at,
            changed_at if status == "FOLLOWED" else None,
        )

    def _append_status(self, username, status, changed_at=None):
        """Append a status transition unless it repeats the current one.

        ``user_status_history`` is the single store of user statuses — the
        current status is simply its latest row (see the
        ``user_current_status`` view).  The INSERT is one atomic statement
        that no-ops when the user's latest transition already has *status*,
        so concurrent workers cannot create duplicate transitions.  Returns
        True when a row was actually appended.
        """
        changed_at = changed_at or datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO user_status_history (username, status, changed_at)
            SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM user_status_history
                WHERE username = ?
                  AND id = (SELECT MAX(id) FROM user_status_history WHERE username = ?)
                  AND status = ?
            )
            """,
            (username, status, changed_at, username, username, status),
        )
        if cur.rowcount:
            self._upsert_current_status(
                username, status, changed_at,
                changed_at if status == "FOLLOWED" else None,
            )
        return cur.rowcount > 0

    def mark_followed(self, username):
        """Mark *username* as FOLLOWED and log a FOLLOW event.

        Appends a FOLLOWED transition to ``user_status_history`` (the
        single status store) and a FOLLOW event to the activity feed.  The
        append is atomic and skipped when the user is already FOLLOWED, so
        the event stream (and the daily follow counter derived from it)
        stays free of duplicates.
        """
        if self._append_status(username, "FOLLOWED"):
            self._log_action(username, "FOLLOW")
        self.conn.commit()

    def mark_followed_existing(self, username):
        """Mark *username* as FOLLOWED on the "already following" path.

        Same transition as :meth:`mark_followed` but without a FOLLOW
        event: the silent runner re-confirms an existing follow rather than
        issuing a new one, so the daily-follow counter (``today_follows``)
        and the Recent-activity feed stay accurate.
        """
        self._append_status(username, "FOLLOWED")
        self.conn.commit()

    def mark_followback(self, username):
        """Mark a user as FOLLOWBACK and log a FOLLOWBACK event.

        They followed us after we followed them.  The append is atomic and
        skipped when the user is already FOLLOWBACK, so repeated
        owner-follower scans don't spam the feed.
        """
        if self._append_status(username, "FOLLOWBACK"):
            self._log_action(username, "FOLLOWBACK")
        self.conn.commit()

    def mark_deleted(self, username):
        """Soft-delete *username* (GitHub account no longer exists).

        The ``users`` row is kept — status history and the activity feed
        must stay intact — but the account's footprint is stripped so a
        deleted user can never be scored, followed, trained on or shown
        with live-looking data:

          * score / public_repos / followers / followers_count → 0,
          * bio / company → NULL, ``scored_at`` → now,
          * ML prediction → NULL,
          * repositories (and their languages/topics links) and company
            links are removed,
          * the cached GitHub profile JSON keeps its static fields
            (avatar, name, location, …) but its mutable counts
            (followers / following / public_repos) are zeroed.

        Logs a DELETED event on the first transition so the activity feed
        shows account deletions without repeating them on later runs.
        Idempotent — safe to call again on an already-deleted user.
        """
        if self._append_status(username, "DELETED"):
            self._log_action(username, "DELETED")

        now = datetime.now(timezone.utc).isoformat()

        # Sanitize the cached profile: zero the mutable counts while
        # keeping static fields (avatar, name, location, …) so the
        # dashboard can still render the row without stale numbers.
        row = self.conn.execute(
            "SELECT github_profile_json FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row and row[0]:
            try:
                data = json.loads(row[0])
                if isinstance(data, dict):
                    for key in ("followers", "following", "public_repos"):
                        data[key] = 0
                    self.conn.execute(
                        "UPDATE users SET github_profile_json = ? WHERE username = ?",
                        (json.dumps(data, ensure_ascii=False), username),
                    )
            except (json.JSONDecodeError, TypeError):
                pass  # corrupt cache — nothing left to sanitize

        self.conn.execute(
            """
            UPDATE users SET
                score        = 0,
                public_repos = 0,
                followers    = 0,
                followers_count = 0,
                bio          = NULL,
                company      = NULL,
                scored_at    = ?,
                ml_follow_prediction = NULL
            WHERE username = ?
            """,
            (now, username),
        )

        # Strip repositories together with their language/topic links
        # (no ON DELETE CASCADE between these tables) and company links.
        # Subquery IN keeps one statement per table and is immune to
        # SQLite's placeholder-count limit even for very repo-heavy users.
        self.conn.execute(
            "DELETE FROM repository_languages WHERE repository_id IN "
            "(SELECT id FROM repositories WHERE user_id = ?)",
            (username,),
        )
        self.conn.execute(
            "DELETE FROM repository_topics WHERE repository_id IN "
            "(SELECT id FROM repositories WHERE user_id = ?)",
            (username,),
        )
        self.conn.execute(
            "DELETE FROM repositories WHERE user_id = ?", (username,),
        )
        self.conn.execute(
            "DELETE FROM user_companies WHERE username = ?", (username,),
        )
        self.conn.commit()

    # --------------------------------------------------
    # Cached profile info (avoid redundant API calls)
    # --------------------------------------------------

    def get_user_cached_info(self, username):
        """Return cached user profile from the users table, or None.

        ``public_repos`` / ``followers`` are returned as ``None`` when no
        reliable profile data exists yet, so callers fall back to an API
        fetch instead of trusting possibly-stale summary columns.

        A full ``github_profile_json`` (written by ``store_full_profile``
        after a real API round-trip) is the source of truth: when present,
        its ``public_repos`` / ``followers`` take precedence.  When absent
        the summary columns are NOT trusted — a previous scoring pass may
        have stamped zeros into them without ever fetching the user.
        """
        row = self.conn.execute(
            """
            SELECT u.username, u.public_repos, u.followers, u.bio, u.company,
                   u.score, u.scored_at, COALESCE(c.status, 'NEW'),
                   u.github_profile_json
            FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE u.username = ?
            """,
            (username,),
        ).fetchone()

        if not row:
            return None

        result = {
            "login": row[0],
            "public_repos": row[1],
            "followers": row[2],
            "bio": row[3],
            "company": row[4],
            "score": row[5],
            "scored_at": row[6],
            "status": row[7],
        }

        # Only a full github_profile_json (from a real API round-trip) is
        # trustworthy.  Without it the summary columns may have been
        # stamped 0 by an earlier unreliable scoring pass, so signal
        # "no data" (None) and let callers re-fetch from the API.
        if not row[8]:
            result["public_repos"] = None
            result["followers"] = None
            return result

        try:
            full = json.loads(row[8])
            # The JSON is the raw API response — its values win.
            for key in (
                "public_repos", "followers", "following", "public_gists",
                "blog", "location", "email", "hireable", "name", "type",
                "twitter_username", "created_at",
            ):
                if key in full:
                    result[key] = full[key]
        except (json.JSONDecodeError, TypeError):
            # Corrupt JSON — treat as no reliable profile data.
            result["public_repos"] = None
            result["followers"] = None

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

        ``snapshot_date`` is the **user's local** date (see ``core.tz``) —
        the midnight snapshot belongs to the local day it records, so the
        followers chart stays aligned with the user's calendar.
        """
        today = datetime.now(get_timezone(self)).date().isoformat()
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

    def prune_api_requests(self, older_than_hours=24 * 31):
        """Delete request-log rows older than *older_than_hours*.

        Retention covers the longest Overview interval (30 days) so the
        interval-scoped "GitHub API requests" card stays truthful; the
        per-hour meter itself only needs the last few hours.  Called
        opportunistically by the API server.
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
        """Count FOLLOW events since the start of the local day.

        The day boundary comes from the user's timezone (``core.tz``), so
        the daily follow budget resets at the user's local midnight rather
        than at UTC midnight.
        """
        since = local_midnight_utc(self).isoformat()
        return self.conn.execute(
            """
            SELECT COUNT(*)
            FROM actions
            WHERE action = 'FOLLOW'
              AND created_at >= ?
            """,
            (since,),
        ).fetchone()[0]

    def today_unfollows(self):
        """Count UNFOLLOW events since the start of the local day.

        Mirrors :meth:`today_follows` — the active-unfollow worker logs an
        UNFOLLOW event per unfollow, so the shared daily budget can count
        both directions.
        """
        since = local_midnight_utc(self).isoformat()
        return self.conn.execute(
            """
            SELECT COUNT(*)
            FROM actions
            WHERE action = 'UNFOLLOW'
              AND created_at >= ?
            """,
            (since,),
        ).fetchone()[0]

    def today_social_actions(self):
        """Count today's follows + unfollows (the shared daily budget).

        The daily follow limit is a combined cap: follows (FollowWorker)
        and unfollows (UnfollowWorker) draw from the same 50-action pool,
        so both workers must check this — never ``today_follows`` alone.
        """
        since = local_midnight_utc(self).isoformat()
        return self.conn.execute(
            """
            SELECT COUNT(*)
            FROM actions
            WHERE action IN ('FOLLOW', 'UNFOLLOW')
              AND created_at >= ?
            """,
            (since,),
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
        """Return the highest-scored NEW user with score >= *threshold*.

        Ordered by ``score DESC`` so the best candidate is subscribed to
        first, tie-broken by ``created_at ASC`` (equally-scored users in
        the order they entered the system).  Returns ``(username, score)``
        or None.  Deleted users are excluded by the ``NEW`` status filter.
        """
        row = self.conn.execute(
            """
            SELECT u.username, u.score FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE COALESCE(c.status, 'NEW') = 'NEW'
              AND u.score >= ?
            ORDER BY u.score DESC, u.created_at ASC
            LIMIT 1
            """,
            (threshold,),
        ).fetchone()
        return row

    def get_unfollow_candidates(self, min_age_days=7):
        """Return a random user eligible for an inactive-unfollow, or None.

        Eligible = current status FOLLOWED (we follow them, they never
        followed back) and followed more than *min_age_days* ago.  Random
        selection rotates through the pool so a single candidate that is
        re-checked (e.g. one who did interact) isn't hit every cycle.

        The caller re-verifies everything against the live GitHub API
        before actually unfollowing (status can change in between).
        """
        row = self.conn.execute(
            """
            SELECT c.username
            FROM user_current_status c
            JOIN users u ON u.username = c.username
            WHERE u.owner = 0
              AND c.status = 'FOLLOWED'
              AND c.followed_at IS NOT NULL
              AND c.followed_at <= datetime('now', ?)
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (f"-{min_age_days} days",),
        ).fetchone()
        return row[0] if row else None

    def current_status(self, username):
        """Return *username*'s current lifecycle status ('NEW' when none yet)."""
        row = self.conn.execute(
            "SELECT status FROM user_current_status WHERE username = ?",
            (username,),
        ).fetchone()
        return row[0] if row else "NEW"

    def get_mutual_follow_users(self):
        """Return usernames for all confirmed mutual-follow users (FOLLOWBACK).

        These are users who followed us back after we followed them.
        """
        rows = self.conn.execute(
            """
            SELECT u.username
            FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE COALESCE(c.status, 'NEW') = 'FOLLOWBACK'
            ORDER BY (
                SELECT h.changed_at FROM user_status_history h
                WHERE h.username = u.username AND h.status = 'FOLLOWED'
                ORDER BY h.id DESC LIMIT 1
            ) ASC
            """
        ).fetchall()
        return [row[0] for row in rows]

    def mark_unfollowed_after_mutual(self, username):
        """Mark a user who unfollowed us after a mutual follow.

        Appends an UNFOLLOWED_AFTER_MUTUAL_FOLLOW transition and logs a
        UNFOLLOWED event (once per user) so the activity feed reflects it.
        The original follow time stays available as the FOLLOWED transition
        in the history log.
        """
        if self._append_status(username, "UNFOLLOWED_AFTER_MUTUAL_FOLLOW"):
            self._log_action(username, "UNFOLLOWED")
        self.conn.commit()

    def mark_unfollowed_no_interaction(self, username):
        """Mark *username* as unfollowed for never interacting with us.

        The UnfollowWorker calls this after successfully unfollowing a
        user who was followed 7+ days, never followed us back, and never
        interacted with the owner's profile or repos.  Appends an
        UNFOLLOWED_NO_INTERACTION transition and logs an UNFOLLOW event
        (once per user) so the activity feed and the shared daily-budget
        counter (``today_unfollows``) reflect it.
        """
        if self._append_status(username, "UNFOLLOWED_NO_INTERACTION"):
            self._log_action(username, "UNFOLLOW")
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
    # Discovery sources & interactions (issue #19)
    # --------------------------------------------------
    # Schema groundwork for the repo-content features: ``discovery_sources``
    # tracks which repositories have already been mined for stargazers /
    # contributors (seed rotation), and ``interactions`` persists every
    # detected interaction with the owner's profile or repositories.  All
    # methods degrade gracefully when the migration has not been applied
    # (``_table_exists`` guard), mirroring the worker_status pattern.

    def add_discovery_source(self, repo_full_name, source_type):
        """Register a seed repository for repo-content discovery.

        Idempotent — the UNIQUE (repo_full_name, source_type) pair makes
        a re-registration a no-op.
        """
        if not self._table_exists("discovery_sources"):
            return None
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO discovery_sources (repo_full_name, source_type) "
            "VALUES (?, ?)",
            (repo_full_name, source_type),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def seed_discovery_sources(self, source_types=("stargazers", "contributors")):
        """Register the owner's own repositories as discovery seeds.

        The seed set is the owner's repository list (already in the
        ``repositories`` table).  Idempotent — already-registered
        (repo, source_type) pairs are skipped via ``INSERT OR IGNORE``, so
        calling this every pass is cheap and keeps the seed list in sync
        as the owner publishes new repositories.  Returns the number of
        seed rows newly registered.
        """
        if not self._table_exists("discovery_sources"):
            return 0
        owner = self.get_owner()
        if not owner:
            return 0
        rows = self.conn.execute(
            "SELECT name FROM repositories WHERE user_id = ?",
            (owner,),
        ).fetchall()
        added = 0
        for (name,) in rows:
            full = f"{owner}/{name}"
            for st in source_types:
                if self.add_discovery_source(full, st):
                    added += 1
        return added

    def get_discovery_sources(self):
        """All discovery_sources rows as dicts (empty pre-migration)."""
        if not self._table_exists("discovery_sources"):
            return []
        rows = self.conn.execute(
            "SELECT id, repo_full_name, source_type, last_checked_at, last_count "
            "FROM discovery_sources ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "repo_full_name": r[1],
                "source_type": r[2],
                "last_checked_at": r[3],
                "last_count": r[4],
            }
            for r in rows
        ]

    def mark_discovery_source_checked(self, repo_full_name, source_type, last_count=None):
        """Stamp a seed as mined now, with the last known result size.

        The stale-seed rotation (``next_discovery_source``) uses
        ``last_checked_at`` to pick the oldest un-mined repository.
        """
        if not self._table_exists("discovery_sources"):
            return
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE discovery_sources SET last_checked_at = ?, last_count = ? "
            "WHERE repo_full_name = ? AND source_type = ?",
            (now, last_count, repo_full_name, source_type),
        )
        self.conn.commit()

    def next_discovery_source(self, source_type=None):
        """The least-recently-checked seed, or None when none exists.

        Never-mined seeds (``last_checked_at IS NULL``) come first, then
        the oldest check — so consecutive passes rotate through the seed
        list instead of re-fetching the same repository.  *source_type*
        narrows the pool to ``'stargazers'`` / ``'contributors'``.
        """
        if not self._table_exists("discovery_sources"):
            return None
        sql = "SELECT repo_full_name, source_type FROM discovery_sources WHERE 1 = 1"
        params = []
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += (
            " ORDER BY last_checked_at IS NOT NULL, last_checked_at ASC "
            "LIMIT 1"
        )
        row = self.conn.execute(sql, params).fetchone()
        return {"repo_full_name": row[0], "source_type": row[1]} if row else None

    def record_interaction(
        self, username, event_type, repo_full_name, event_id, created_at,
        seen_at=None,
    ):
        """Persist one interaction, deduped on ``event_id``.

        ``created_at`` is the REAL event timestamp from the GitHub event
        object (never the ingestion time) — required for temporal hygiene
        in ML feature extraction.  Returns True when a new row was
        inserted, False when the event was already recorded (or the table
        does not exist yet).
        """
        if not self._table_exists("interactions"):
            return False
        seen_at = seen_at or datetime.now(timezone.utc).isoformat()
        # Normalise the REAL event timestamp to the same ISO format the rest
        # of the DB uses (``...+00:00`` instead of GitHub's ``...Z``).  The
        # as-of ML queries string-compare created_at against follow
        # timestamps, and ``"Z"`` vs ``"+00:00"`` would sort wrongly at the
        # boundary second ('Z' > '+') — normalising keeps the comparison
        # exact regardless of which format a caller passes.
        created_at = (created_at or "").replace("Z", "+00:00")
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO interactions
                (username, event_type, repo_full_name, event_id, created_at, seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, event_type, repo_full_name, event_id, created_at, seen_at),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def user_interactions_before(self, username, ts):
        """Interactions of *username* with ``created_at <= ts`` (as-of query).

        Used by the ML feature extractor: features must come only from
        interactions that happened BEFORE the follow timestamp — anything
        after the follow would leak the label.  Strictly ``<`` (created_at
        is normalised to the same ISO format at insert time, so the string
        comparison is exact).
        """
        if not self._table_exists("interactions"):
            return []
        rows = self.conn.execute(
            "SELECT username, event_type, repo_full_name, event_id, created_at "
            "FROM interactions WHERE username = ? AND created_at < ? "
            "ORDER BY created_at",
            (username, ts),
        ).fetchall()
        return [
            {
                "username": r[0],
                "event_type": r[1],
                "repo_full_name": r[2],
                "event_id": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def user_has_persisted_interaction(self, username):
        """True when *username* has at least one recorded interaction.

        The unfollow worker consults this before unfollowing: a user with
        a persisted interaction must never be unfollowed.
        """
        if not self._table_exists("interactions"):
            return False
        row = self.conn.execute(
            "SELECT 1 FROM interactions WHERE username = ? LIMIT 1",
            (username,),
        ).fetchone()
        return row is not None

    def mark_reciprocal_action(self, event_id, kind):
        """Record a reciprocal action on an interaction (issue #24).

        *kind* is ``'star'`` (we starred the actor's repo) or ``'follow'``
        (we followed the actor) — mapped to ``we_starred`` /
        ``we_followed_back``.  The columns exist from day one so #24 needs
        no schema change.
        """
        if kind not in ("star", "follow"):
            return
        if not self._table_exists("interactions"):
            return
        col = "we_starred" if kind == "star" else "we_followed_back"
        self.conn.execute(
            f"UPDATE interactions SET {col} = 1 WHERE event_id = ?",
            (event_id,),
        )
        self.conn.commit()

    def next_interactor_needing_reciprocation(self):
        """Username of the oldest interactor still needing a reciprocal action.

        An interactor "needs reciprocation" while any of their recorded
        interactions has ``we_followed_back = 0`` or ``we_starred = 0``
        (issue #24).  Deleted users are excluded (nothing to answer).
        Returns None when everyone has been answered.
        """
        if not self._table_exists("interactions"):
            return None
        row = self.conn.execute(
            """
            SELECT i.username
            FROM interactions i
            LEFT JOIN user_current_status c ON c.username = i.username
            WHERE (i.we_followed_back = 0 OR i.we_starred = 0)
              AND COALESCE(c.status, 'NEW') != 'DELETED'
            GROUP BY i.username
            ORDER BY MIN(i.seen_at) ASC, MIN(i.id) ASC
            LIMIT 1
            """,
        ).fetchone()
        return row[0] if row else None

    def mark_user_reciprocal(self, username, kind):
        """Record a reciprocal action on ALL of *username*'s interactions.

        *kind* is ``'star'`` or ``'follow'`` — mapped to ``we_starred`` /
        ``we_followed_back`` (issue #24).  The reciprocal action applies to
        the person, so every interaction they produced is stamped at once.
        """
        if kind not in ("star", "follow"):
            return
        if not self._table_exists("interactions"):
            return
        col = "we_starred" if kind == "star" else "we_followed_back"
        self.conn.execute(
            f"UPDATE interactions SET {col} = 1 WHERE username = ?",
            (username,),
        )
        self.conn.commit()

    def list_interactions(self, limit=12, actor=None, event_type=None):
        """Most recent interactions, newest first (issue #23 dashboard feed).

        *actor* / *event_type* optionally narrow the feed (exact match —
        the dashboard filter dropdown sends the raw values).  Returns an
        empty list when the migration has not been applied.
        """
        if not self._table_exists("interactions"):
            return []
        sql = (
            "SELECT username, event_type, repo_full_name, event_id, "
            "created_at, seen_at, we_starred, we_followed_back "
            "FROM interactions WHERE 1 = 1"
        )
        params = []
        if actor:
            sql += " AND username = ?"
            params.append(actor)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY seen_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "username": r[0],
                "event_type": r[1],
                "repo_full_name": r[2],
                "event_id": r[3],
                "created_at": r[4],
                "seen_at": r[5],
                "we_starred": r[6],
                "we_followed_back": r[7],
            }
            for r in rows
        ]

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

        Label scheme (simple 7-day reciprocity rule), derived from the
        ``user_status_history`` log (via the ``user_current_status`` view):

          * label=1 — user followed us back in response to our follow
            (current status FOLLOWBACK), including users who followed back
            and later unfollowed us (UNFOLLOWED_AFTER_MUTUAL_FOLLOW — they
            DID reciprocate at least once).  Positives are not time-boxed —
            any user who reciprocated is label=1.
          * label=0 — user was followed by us more than 7 days ago and
            never followed back (current status FOLLOWED with the FOLLOWED
            transition older than 7 days), plus users we actively
            unfollowed for inactivity (UNFOLLOWED_NO_INTERACTION — they
            never followed back either; keeping them labelled preserves
            the negative pool as the unfollow worker cleans up).  The
            7-day observation window applies to the negative class only.

        Only includes users who have been scored and have repo data.

        Returns list of (username, label).
        """
        rows = self.conn.execute(
            """
            SELECT u.username,
                   CASE
                       WHEN c.status IN ('FOLLOWBACK', 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW') THEN 1
                       ELSE 0
                   END AS label
            FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE (
                c.status = 'FOLLOWBACK'
                OR (
                    c.status = 'FOLLOWED'
                    AND c.changed_at IS NOT NULL
                    AND c.changed_at < datetime('now', '-7 days')
                )
                OR c.status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'
                OR c.status = 'UNFOLLOWED_NO_INTERACTION'
            )
            AND u.score IS NOT NULL
            AND u.score > 0
            AND EXISTS (
                SELECT 1 FROM repositories WHERE user_id = u.username
            )
            ORDER BY u.username
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

    def ml_prediction(self, username):
        """Return the stored ML prediction (0 or 1) for *username*, or None."""
        row = self.conn.execute(
            "SELECT ml_follow_prediction FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return row[0] if row else None

    # --------------------------------------------------
    # ML follow gate — runtime "strictness" settings
    # --------------------------------------------------
    # The gate is controlled from the Management tab (no restart): the
    # master switch and the decision threshold live in the settings table,
    # seeded by the ML_FOLLOW_* config defaults on a fresh install.

    def get_ml_follow_threshold(self):
        """Effective ML follow threshold (0–1), from settings or config."""
        raw = self.get_setting("ml_follow_threshold")
        if raw is None:
            return ML_FOLLOW_THRESHOLD
        try:
            return float(raw)
        except (TypeError, ValueError):
            return ML_FOLLOW_THRESHOLD

    def get_ml_follow_gate_enabled(self):
        """True when the FollowWorker's ML gate is on (settings or config)."""
        raw = self.get_setting("ml_follow_gate_enabled")
        if raw is None:
            return ML_FOLLOW_GATE_ENABLED
        return str(raw).lower() in ("1", "true", "yes")

    def set_ml_follow_config(self, gate_enabled=None, threshold=None):
        """Persist the Management-tab gate switch / threshold.

        Accepts None for either field (only the given one is written).
        Stored as strings (settings is a key/value text table).
        """
        if gate_enabled is not None:
            self.set_setting("ml_follow_gate_enabled", "1" if gate_enabled else "0")
        if threshold is not None:
            self.set_setting("ml_follow_threshold", str(round(float(threshold), 4)))


    # --------------------------------------------------
    # ML training-run history (dashboard ML tab)
    # --------------------------------------------------

    def _table_exists(self, name):
        """True when table *name* exists in this connection's schema."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def record_ml_training_run(self, metadata):
        """Persist one training cycle into ``ml_training_runs``.

        Returns the new row id, or None when the table does not exist
        yet (migrations not applied) — the trainer must never break over
        this, so it degrades gracefully.
        """
        if not self._table_exists("ml_training_runs"):
            return None

        cols = [
            "version", "trained_at", "label_scheme", "num_samples",
            "num_positives", "num_negatives", "input_dim", "hidden_dim",
            "dropout", "top_languages", "top_topics", "val_accuracy",
            "val_auc", "val_precision", "val_recall", "val_loss",
            "pred1_ratio", "cv_folds", "cv_accuracy", "cv_auc",
            "cv_precision", "cv_recall", "cv_pred1_ratio", "epochs",
            "early_stopped", "device", "training_time_seconds",
        ]
        values = []
        for col in cols:
            v = metadata.get(col)
            if col in ("top_languages", "top_topics") and v is not None:
                v = json.dumps(v)
            values.append(v)
        cur = self.conn.execute(
            f"INSERT INTO ml_training_runs ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            values,
        )
        self.conn.commit()
        return cur.lastrowid

    def update_ml_training_run_recompute(self, run_id, stats):
        """Record the outcome of the background prediction recompute.

        Called by the MLRecomputeWorker thread after re-predicting the
        whole population with the freshly trained model.
        """
        if run_id is None or not self._table_exists("ml_training_runs"):
            return
        self.conn.execute(
            """
            UPDATE ml_training_runs
            SET recompute_total   = ?,
                recompute_pred_1  = ?,
                recompute_pred_0  = ?,
                recompute_failed  = ?,
                recompute_done_at = ?
            WHERE id = ?
            """,
            (
                stats.get("total"), stats.get("pred_1"), stats.get("pred_0"),
                stats.get("failed"),
                datetime.now(timezone.utc).isoformat(),
                run_id,
            ),
        )
        self.conn.commit()

    def ml_training_runs(self, limit=100):
        """Recent training runs (newest first) as dict rows.

        ``top_languages`` / ``top_topics`` are JSON columns deserialised
        back into lists; ``early_stopped`` back to a bool.  Returns an
        empty list when the migration hasn't been applied.
        """
        if not self._table_exists("ml_training_runs"):
            return []
        cols = [
            "id", "version", "trained_at", "label_scheme", "num_samples",
            "num_positives", "num_negatives", "input_dim", "hidden_dim",
            "dropout", "top_languages", "top_topics", "val_accuracy",
            "val_auc", "val_precision", "val_recall", "val_loss",
            "pred1_ratio", "cv_folds", "cv_accuracy", "cv_auc",
            "cv_precision", "cv_recall", "cv_pred1_ratio", "epochs",
            "early_stopped", "device", "training_time_seconds",
            "recompute_total", "recompute_pred_1", "recompute_pred_0",
            "recompute_failed", "recompute_done_at",
        ]
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM ml_training_runs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            for jcol in ("top_languages", "top_topics"):
                raw = d.get(jcol)
                if raw:
                    try:
                        d[jcol] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        d[jcol] = []
                else:
                    d[jcol] = []
            if d.get("early_stopped") is not None:
                d["early_stopped"] = bool(d["early_stopped"])
            out.append(d)
        return out

    # --------------------------------------------------
    # Graph-discovery worker history (dashboard card)
    # --------------------------------------------------

    def record_discovery_run(self, stats):
        """Persist one graph-discovery pass into ``discovery_runs``.

        Called by the GraphDiscoveryWorker after every pass.  Returns the
        new row id, or None when the table does not exist yet (migrations
        not applied) — the worker must never break over this.
        """
        if not self._table_exists("discovery_runs"):
            return None
        cur = self.conn.execute(
            """
            INSERT INTO discovery_runs (
                started_at, finished_at, users_walked, new_users,
                requests, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stats.get("started_at"),
                stats.get("finished_at"),
                stats.get("users_walked"),
                stats.get("new_users"),
                stats.get("requests"),
                stats.get("duration_seconds"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def discovery_runs(self, limit=20):
        """Most recent graph-discovery passes (newest first) as dict rows.

        Returns an empty list when the migration hasn't been applied.
        """
        if not self._table_exists("discovery_runs"):
            return []
        rows = self.conn.execute(
            """
            SELECT id, started_at, finished_at, users_walked, new_users,
                   requests, duration_seconds
            FROM discovery_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "finished_at": r[2],
                "users_walked": r[3],
                "new_users": r[4],
                "requests": r[5],
                "duration_seconds": r[6],
            }
            for r in rows
        ]

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
