"""Background worker — answers attention with attention (issue #24).

The attention worker (#21) records who stars / forks / opens issues or
PRs on our repositories.  A user who interacts with our content is the
strongest possible lead, and a follow — or a star on their repository —
sent back is a natural, human-like reciprocal signal that measurably
raises the chance of a mutual follow.

This worker reacts to NEW interactors (deliberately OPTIONAL and separate
from #21: it changes the bot's social behaviour, so it can be toggled
independently from the Management tab):

  * **follow** them — respecting the shared daily follow+unfollow budget
    and ``SOCIAL_ACTION_LOCK`` (the same pool the FollowWorker and
    UnfollowWorker draw from);
  * **star** their most relevant repository (highest stars / matching
    owner topics), fetched from our DB first, live API as a fallback;
  * record every reciprocal action on the interactions
    (``we_followed_back`` / ``we_starred``) — required so ML feature
    extraction can separate correlation from causation (#25).

Actions are paced with the same random 20-30 min interval as follows, so
reciprocity never looks like a burst.  All GitHub calls go through the
existing rate-limit / auth handling (mirrors the FollowWorker).

Usage in main.py::

    from workers.reciprocal_worker import ReciprocalWorker
    worker = ReciprocalWorker(shutdown_event)
    worker.start()
"""

import random
import threading
import time

from core.config import (
    DAILY_FOLLOW_LIMIT,
    FOLLOW_INTERVAL_MAX_SECONDS,
    FOLLOW_INTERVAL_MIN_SECONDS,
    RECIPROCAL_POLL_INTERVAL_SECONDS,
)
from core.github_client import (
    LONG_HINT_THRESHOLD,
    GitHubAuthError,
    GitHubNetworkError,
    GitHubRateLimitError,
    first_wait_from_headers,
)
from core.logger import get_logger
from workers.runtime import (
    SOCIAL_ACTION_LOCK,
    is_enabled,
    mark_action,
    mark_error,
    mark_started,
    mark_stopped,
    sleep_interruptible,
    touch_heartbeat,
    wait_until_enabled,
)

log = get_logger(__name__)

# Key of this worker in the worker_status table (dashboard toggle/status).
WORKER_KEY = "reciprocal"

# How many pages of the interactor's repos to fetch when nothing is in
# our DB yet — 300 repos is plenty to find a worthwhile star target
# without burning the hourly budget on a power user.
_MAX_REPO_PAGES = 3

# After this many consecutive rate-limit hits the worker sleeps a long
# cooldown (mirrors FollowWorker._RATE_LIMIT_RETRIES / _COOLDOWN_SECONDS).
_RATE_LIMIT_RETRIES = 3
_COOLDOWN_SECONDS = 2 * 60 * 60


class ReciprocalWorker(threading.Thread):
    """Daemon thread that follows and stars new interactors.

    Creates its own Database and GithubClient instances to avoid SQLite
    thread-safety issues (same pattern as the other workers).

    One interactor per cycle, then a random 20-30 min pause — the same
    human-like cadence as the FollowWorker.  When nobody needs a
    reciprocal action it waits a short poll interval and re-checks.

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    poll_interval : int, optional
        Re-check cadence when the queue is empty (seconds).  Default from
        config ``RECIPROCAL_POLL_INTERVAL_SECONDS``.
    """

    def __init__(self, shutdown_event, poll_interval=RECIPROCAL_POLL_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="ReciprocalWorker")
        self._shutdown = shutdown_event
        self._poll_interval = poll_interval
        self._rate_limit_streak = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        # Import inside the thread to avoid circular imports at module level.
        from core.database import Database
        from core.github_client import GithubClient

        db = Database()
        github = GithubClient()
        self._db = db  # heartbeat source for _sleep (kept alive during waits)

        log.info(
            "Reciprocal worker started (interval=%d–%ds, daily follow "
            "limit=%d)",
            FOLLOW_INTERVAL_MIN_SECONDS, FOLLOW_INTERVAL_MAX_SECONDS,
            DAILY_FOLLOW_LIMIT,
        )
        mark_started(db, WORKER_KEY)

        try:
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("Reciprocal worker paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue

                try:
                    outcome = self._drain_one(db, github)
                except Exception as exc:
                    mark_error(db, WORKER_KEY, f"{type(exc).__name__}: {exc}")
                    log.exception("Reciprocal worker error")
                    outcome = "empty"

                if self._shutdown.is_set():
                    break

                if outcome == "exhausted":
                    log.info(
                        "Daily follow+unfollow budget reached (%d/%d) — "
                        "next reciprocal follow after local midnight.",
                        db.today_social_actions(), DAILY_FOLLOW_LIMIT,
                    )
                    if not sleep_interruptible(
                        self._shutdown, self._seconds_until_midnight(db),
                        db=db, key=WORKER_KEY,
                    ):
                        break
                else:
                    if not sleep_interruptible(
                        self._shutdown, self._poll_interval,
                        db=db, key=WORKER_KEY,
                    ):
                        break
        finally:
            mark_stopped(db, WORKER_KEY)
            db.conn.close()
            log.info("Reciprocal worker stopped.")

    # ------------------------------------------------------------------
    # One interactor per cycle
    # ------------------------------------------------------------------

    def _drain_one(self, db, github):
        """Process the oldest interactor still needing a reciprocal action.

        Returns ``\"exhausted\"`` when the daily budget stopped the follow,
        ``\"empty\"`` when nobody needs an action, ``\"done\"`` after a
        successful cycle, ``\"paused\"`` / ``\"stopped\"`` otherwise.
        """
        username = db.next_interactor_needing_reciprocation()
        if not username:
            return "empty"

        touch_heartbeat(db, WORKER_KEY)

        # ── 1) Follow back (respects the shared daily budget + lock) ──
        follow_outcome = self._follow_interactor(db, github, username)
        if follow_outcome == "no_budget":
            return "exhausted"
        if follow_outcome in ("shutdown", "auth_fatal"):
            return "stopped"
        if follow_outcome == "rate_limited":
            # Handled inside (already waited) — retry next cycle, backing
            # off into a long cooldown after repeated hits.
            self._rate_limit_streak += 1
            if self._rate_limit_streak >= _RATE_LIMIT_RETRIES:
                log.warning(
                    "Reciprocal worker: %d consecutive rate limits — "
                    "cooling down %ds.",
                    self._rate_limit_streak, _COOLDOWN_SECONDS,
                )
                if not self._sleep(_COOLDOWN_SECONDS):
                    return "stopped"
                self._rate_limit_streak = 0
            return "done"

        # ── 2) Star their most relevant repository ──
        self._star_interactor(db, github, username)

        self._rate_limit_streak = 0
        mark_action(db, WORKER_KEY)

        # Random human-like pause between reciprocal actions (20–30 min).
        wait = random.uniform(
            FOLLOW_INTERVAL_MIN_SECONDS, FOLLOW_INTERVAL_MAX_SECONDS,
        )
        if not self._sleep(wait):
            return "stopped"
        return "done"

    # ------------------------------------------------------------------
    # Follow back
    # ------------------------------------------------------------------

    def _follow_interactor(self, db, github, username):
        """Follow *username* back if we don't already, budget permitting.

        Serialised via the shared ``SOCIAL_ACTION_LOCK`` with the
        FollowWorker / UnfollowWorker so the combined daily budget can
        never be overspent.  Returns ``\"followed\"``, ``\"already\"``,
        ``\"no_budget\"``, ``\"rate_limited\"``, ``\"skipped\"``,
        ``\"shutdown\"`` / ``\"auth_fatal\"``.
        """
        with SOCIAL_ACTION_LOCK:
            return self._follow_interactor_unlocked(db, github, username)

    def _follow_interactor_unlocked(self, db, github, username):
        """Follow logic — run with ``SOCIAL_ACTION_LOCK`` already held."""
        done_today = db.today_social_actions()
        remaining = DAILY_FOLLOW_LIMIT - done_today
        if remaining <= 0:
            log.info(
                "Reciprocal follow skipped for %s — daily budget reached "
                "(%d/%d).",
                username, done_today, DAILY_FOLLOW_LIMIT,
            )
            return "no_budget"

        # ── Already following? Nothing to do — just record it ──
        try:
            already = github.already_following(username)
        except GitHubNetworkError as exc:
            log.warning(
                "Network error checking follow for %s: %s — skipping",
                username, exc,
            )
            return "skipped"
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) checking follow for %s — waiting",
                exc.status_code, username,
            )
            if not self._wait_rate_limit(exc):
                return "shutdown"
            return "rate_limited"
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
            return "auth_fatal"

        if already:
            log.info("Reciprocal: already following %s — recording follow-back", username)
            db.mark_user_reciprocal(username, "follow")
            return "already"

        # ── Follow ──
        try:
            if github.follow(username):
                db.mark_followed(username)
                db.mark_user_reciprocal(username, "follow")
                log.info(
                    "Reciprocal: followed interactor %s [%d/%d today]",
                    username, db.today_social_actions(), DAILY_FOLLOW_LIMIT,
                )
                return "followed"
            log.warning("Reciprocal: failed to follow %s", username)
            return "skipped"
        except GitHubNetworkError as exc:
            log.warning(
                "Network error following %s: %s — skipping", username, exc,
            )
            return "skipped"
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) following %s — waiting",
                exc.status_code, username,
            )
            if not self._wait_rate_limit(exc):
                return "shutdown"
            return "rate_limited"
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
            return "auth_fatal"

    # ------------------------------------------------------------------
    # Star back
    # ------------------------------------------------------------------

    def _star_interactor(self, db, github, username):
        """Star *username*'s most relevant repository, if one is found.

        Relevance = matching the owner's topics first, then highest stars.
        Our DB is preferred (free); the live API is the fallback (capped
        at ``_MAX_REPO_PAGES`` pages).  Records ``we_starred`` on their
        interactions when a star actually happened.  Never raises — all
        GitHub errors are logged and treated as \"no star this cycle\"
        (the follow-back was already recorded, so the user is answered).
        """
        try:
            owner = db.get_owner()
            repo_name = self._pick_repo_to_star(db, github, username, owner)
        except Exception as exc:
            log.warning(
                "Reciprocal: could not pick a repository to star for %s — "
                "%s: %s", username, type(exc).__name__, exc,
            )
            return

        if not repo_name:
            log.info(
                "Reciprocal: no suitable public repository found for %s — "
                "skipping star (follow already recorded)", username,
            )
            return

        try:
            if github.star_repository(username, repo_name):
                db.mark_user_reciprocal(username, "star")
                log.info(
                    "Reciprocal: starred %s/%s (interactor)",
                    username, repo_name,
                )
            else:
                log.warning(
                    "Reciprocal: star of %s/%s returned failure",
                    username, repo_name,
                )
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) starring %s/%s — star retried next cycle",
                exc.status_code, username, repo_name,
            )
            if not self._wait_rate_limit(exc):
                return
        except GitHubNetworkError as exc:
            log.warning(
                "Network error starring %s/%s: %s — star retried next cycle",
                username, repo_name, exc,
            )

    def _pick_repo_to_star(self, db, github, username, owner):
        """Pick the interactor's most relevant repository to star.

        Preference: a repository whose topics intersect the owner's topics
        (niche relevance), otherwise the highest-starred repository.  Our
        own DB is consulted first (already collected); when the user has
        no repos collected yet, the live API is fetched (capped pages).
        Returns the repo short name, or None.
        """
        owner_topics = set(db.user_topics(owner)) if owner else set()

        # ── DB first: repos already collected for this user ──
        rows = db.conn.execute(
            "SELECT name, stars FROM repositories WHERE user_id = ? "
            "ORDER BY stars DESC",
            (username,),
        ).fetchall()
        if rows:
            # Find topic matches among the collected repos.
            for name, stars in rows:
                if self._repo_matches_topics(db, name, username, owner_topics):
                    return name
            return rows[0][0]  # highest stars

        # ── Live fallback: fetch their repos (capped) ──
        from core.github_client import _paginate

        try:
            repos = _paginate(
                github, f"/users/{username}/repos",
                max_pages=_MAX_REPO_PAGES,
            )
        except (GitHubAuthError, GitHubNetworkError, GitHubRateLimitError) as exc:
            log.warning(
                "Reciprocal: fetching repos for %s failed (%s) — no star",
                username, exc,
            )
            return None

        # Topic match first, then highest stars.
        for repo in repos:
            repo_topics = set(repo.get("topics") or [])
            if repo_topics & owner_topics:
                return repo.get("name")
        if repos:
            return max(
                (r for r in repos if r.get("name")),
                key=lambda r: r.get("stargazers_count", 0),
            ).get("name")
        return None

    @staticmethod
    def _repo_matches_topics(db, repo_name, username, owner_topics):
        """True when one of *repo_name*'s topics matches the owner's."""
        if not owner_topics:
            return False
        rows = db.conn.execute(
            """
            SELECT t.name
            FROM repository_topics rt
            JOIN repositories r ON r.id = rt.repository_id
            JOIN topics t ON t.id = rt.topic_id
            WHERE r.user_id = ? AND r.name = ?
            """,
            (username, repo_name),
        ).fetchall()
        return any(r[0] in owner_topics for r in rows)

    # ------------------------------------------------------------------
    # Rate-limit / auth handling
    # ------------------------------------------------------------------

    def _wait_rate_limit(self, exc):
        """Sleep out a reciprocal-action rate limit (mirrors FollowWorker).

        Returns False when shutdown was requested while waiting.
        """
        raw = getattr(exc, "retry_after", None)
        if raw is None and getattr(exc, "reset_at", None) is not None:
            raw = exc.reset_at - int(time.time())

        if raw is not None and raw >= LONG_HINT_THRESHOLD:
            wait = _COOLDOWN_SECONDS
            log.warning(
                "Reciprocal rate-limited (%s) — long block, cooling down %ds.",
                exc.verdict, wait,
            )
        else:
            wait = first_wait_from_headers(exc, fallback_seconds=5 * 60)
            log.warning(
                "Reciprocal rate-limited (%s) — waiting %ds before retrying",
                exc.verdict, wait,
            )
        return self._sleep(wait)

    def _abort_on_auth_error(self, exc):
        """Log a loud fatal message and request graceful shutdown."""
        log.error(
            "AUTH FAILURE (%s) on %s — token revoked/expired. Aborting.",
            exc.status_code, exc.url,
        )
        print(
            "\n❌ AUTH FAILURE — GitHub rejected the token (revoked/expired).\n"
            "   Refresh GITHUB_TOKEN in your .env and restart.\n"
        )
        self._shutdown.set()

    # ------------------------------------------------------------------
    # Sleep helpers
    # ------------------------------------------------------------------

    def _sleep(self, seconds):
        """Sleep *seconds*, waking early on shutdown.

        Returns True if the full sleep completed, False on shutdown.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._shutdown.is_set():
                return False
            touch_heartbeat(getattr(self, "_db", None), WORKER_KEY)
            remaining = deadline - time.monotonic()
            time.sleep(max(0.0, min(1.0, remaining)))
        return not self._shutdown.is_set()

    @staticmethod
    def _seconds_until_midnight(db):
        """Seconds until the user's local midnight (daily budget reset)."""
        from core.tz import local_midnight_utc
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        next_midnight = local_midnight_utc(db, days_ago=-1)
        return max(60.0, (next_midnight - now).total_seconds())
