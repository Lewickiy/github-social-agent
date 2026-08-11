"""Background worker — unfollows inactive, non-interacting follows.

The system grows its following to attract follow-backs, but not every
follow responds.  This worker cleans up: it picks users who have been
followed for ``UNFOLLOW_AFTER_DAYS`` (7) or more with status still
FOLLOWED (they never followed us back), verifies live against GitHub
that they still don't follow us AND have never interacted with the
owner's profile or any of the owner's repositories (star / fork / issue
/ PR / comment / review / push / … — see ``services/interactions``),
then unfollows them.

Guarantees:
  * **Same pacing as follows** — a random 20–30 min human-like pause
    between unfollows (``FOLLOW_INTERVAL_MIN/MAX_SECONDS``).
  * **Shared daily budget** — follows and unfollows draw from one pool
    (``DAILY_FOLLOW_LIMIT``, 50 actions/day combined); the check-then-act
    sequence runs under the shared ``SOCIAL_ACTION_LOCK`` so the two
    workers can never overspend the day together.
  * **Never unfollows a mutual** — ``does_user_follow_us`` is re-checked
    live first; a user who follows us now is promoted to FOLLOWBACK and
    kept, never unfollowed.
  * **Keeps interactors** — a user who interacted with the owner's repos
    / profile is skipped (their interaction check is not persisted, so it
    is re-evaluated each time they surface as a candidate).
  * **Toggleable** — enabled/paused from the Management tab like every
    other worker (worker_status row seeded by migration 028).
  * **Fresh status** — the candidate is re-selected from the DB before
    every unfollow, and its current status re-checked right before acting.

Usage in main.py::

    from workers.unfollow_worker import UnfollowWorker
    worker = UnfollowWorker(shutdown_event)
    worker.start()
"""

import random
import threading
import time
from datetime import datetime, timezone

from core.config import (
    DAILY_FOLLOW_LIMIT,
    FOLLOW_INTERVAL_MAX_SECONDS,
    FOLLOW_INTERVAL_MIN_SECONDS,
    MY_USERNAME,
    UNFOLLOW_AFTER_DAYS,
    UNFOLLOW_WORKER_POLL_INTERVAL_SECONDS,
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
WORKER_KEY = "unfollow"

# After this many consecutive rate-limit hits the worker stops retrying
# and sleeps a long cooldown (mirrors FollowWorker / SilentRunner).
_RATE_LIMIT_RETRIES = 3
_COOLDOWN_SECONDS = 2 * 60 * 60

# "Kept" candidates (interactors / mutuals) are NOT unfollowed but still
# cost 1–2 GETs per check.  A 3 s delay would loop at ~2400 req/h while
# any interactor stays in the pool, so kept decisions pace on the poll
# interval instead — at most ~2 requests/min, calm like the rest of the
# system.  Real unfollows are paced by the 20–30 min interval.


class UnfollowWorker(threading.Thread):
    """Daemon thread that unfollows long-stale, non-interacting follows.

    Creates its own Database and GithubClient instances to avoid SQLite
    thread-safety issues (same pattern as the other workers).

    Each cycle picks one random eligible user (FOLLOWED for
    ``UNFOLLOW_AFTER_DAYS`` or more), verifies live that they do not
    follow us back and have not interacted with the owner, and unfollows
    them — sleeping a random 20–30 min between unfollows.  When no
    eligible users exist it waits a short poll interval, and when the
    shared daily budget is exhausted it sleeps until local midnight.
    """

    def __init__(
        self,
        shutdown_event,
        min_age_days=UNFOLLOW_AFTER_DAYS,
        poll_interval=UNFOLLOW_WORKER_POLL_INTERVAL_SECONDS,
    ):
        super().__init__(daemon=True, name="UnfollowWorker")
        self._shutdown = shutdown_event
        self._min_age_days = min_age_days
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
            "Unfollow worker started (min age=%dd, interval=%d–%ds, "
            "shared daily budget=%d)",
            self._min_age_days,
            FOLLOW_INTERVAL_MIN_SECONDS, FOLLOW_INTERVAL_MAX_SECONDS,
            DAILY_FOLLOW_LIMIT,
        )
        mark_started(db, WORKER_KEY)

        try:
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("Unfollow worker paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue

                try:
                    outcome = self._drain_unfollow_queue(db, github)
                except Exception as exc:
                    mark_error(db, WORKER_KEY, f"{type(exc).__name__}: {exc}")
                    log.exception("Unfollow worker error")
                    outcome = "empty"

                if self._shutdown.is_set():
                    break

                if outcome == "exhausted":
                    log.info(
                        "Daily follow+unfollow budget reached (%d/%d) — "
                        "next attempt after local midnight.",
                        db.today_social_actions(), DAILY_FOLLOW_LIMIT,
                    )
                    if not sleep_interruptible(
                        self._shutdown, self._seconds_until_midnight(db),
                        db=db, key=WORKER_KEY,
                    ):
                        break
                elif outcome != "paused":
                    if not sleep_interruptible(
                        self._shutdown, self._poll_interval,
                        db=db, key=WORKER_KEY,
                    ):
                        break
        finally:
            mark_stopped(db, WORKER_KEY)
            db.conn.close()
            log.info("Unfollow worker stopped.")

    # ------------------------------------------------------------------
    # Queue drain
    # ------------------------------------------------------------------

    def _drain_unfollow_queue(self, db, github):
        """Unfollow eligible users while the shared daily limit allows.

        Returns ``"exhausted"`` when the drain stopped because the daily
        budget is spent, ``"empty"`` when no eligible users exist,
        ``"paused"`` on toggle-off, ``"stopped"`` on shutdown / errors.
        """
        while not self._shutdown.is_set():
            # Toggled off mid-drain — stop and let run() pause.
            if not is_enabled(db, WORKER_KEY):
                return "paused"

            # Fresh selection before every unfollow — statuses and the
            # follow queue keep changing, so re-read the pool each time.
            username = db.get_unfollow_candidates(self._min_age_days)
            if not username:
                return "empty"

            # The candidate may have left the FOLLOWED state since the
            # query (followback detected, deleted, already unfollowed …) —
            # skip to the next one (cheap DB-only loop; brief pause keeps
            # it from spinning if a whole batch changed at once).
            if db.current_status(username) != "FOLLOWED":
                if not self._sleep(1):
                    return "stopped"
                continue

            outcome = self._unfollow_user(db, github, username)

            if outcome == "unfollowed":
                self._rate_limit_streak = 0
                # Same human-like random pause as subscriptions (20–30 min).
                wait = random.uniform(
                    FOLLOW_INTERVAL_MIN_SECONDS,
                    FOLLOW_INTERVAL_MAX_SECONDS,
                )
                if not self._sleep(wait):
                    return "stopped"
            elif outcome == "kept":
                # User interacts or follows us — leave them alone.  Each
                # check costs 1–2 GETs, so pace kept decisions on the poll
                # interval (not seconds): at most ~2 requests/min even
                # when the whole pool is interactors.
                self._rate_limit_streak = 0
                if not self._sleep(self._poll_interval):
                    return "stopped"
                continue
            elif outcome == "rate_limited":
                # Handled inside _unfollow_user (already waited) — retry
                # on the next iteration, but back off into a long cooldown
                # after repeated hits (permanently exhausted limit).
                self._rate_limit_streak += 1
                if self._rate_limit_streak >= _RATE_LIMIT_RETRIES:
                    log.warning(
                        "Unfollow worker: %d consecutive rate limits — "
                        "cooling down %ds.",
                        self._rate_limit_streak, _COOLDOWN_SECONDS,
                    )
                    if not self._sleep(_COOLDOWN_SECONDS):
                        return "stopped"
                    self._rate_limit_streak = 0
                continue
            else:  # "no_budget" | "skipped" | "shutdown"
                return "exhausted" if outcome == "no_budget" else "stopped"

        return "stopped"

    def _unfollow_user(self, db, github, username):
        """Unfollow *username* if the shared daily budget allows.

        Serialised via ``SOCIAL_ACTION_LOCK`` so the combined follow+
        unfollow budget can never be overspent by the two workers racing.
        Returns one of ``"unfollowed"``, ``"kept"``, ``"no_budget"``,
        ``"rate_limited"``, ``"skipped"``, ``"shutdown"``.
        """
        with SOCIAL_ACTION_LOCK:
            return self._unfollow_user_unlocked(db, github, username)

    def _unfollow_user_unlocked(self, db, github, username):
        """Unfollow logic — run with ``SOCIAL_ACTION_LOCK`` already held."""
        done_today = db.today_social_actions()
        remaining = DAILY_FOLLOW_LIMIT - done_today
        if remaining <= 0:
            log.info(
                "Daily follow+unfollow limit reached (%d/%d).",
                done_today, DAILY_FOLLOW_LIMIT,
            )
            return "no_budget"

        # ── Safety 1: do they follow us now? ──
        # Status can be stale between owner-follower scans; never unfollow
        # a user who actually follows us — promote them to FOLLOWBACK.
        try:
            they_follow_us = github.does_user_follow_us(username, MY_USERNAME)
        except GitHubNetworkError as exc:
            log.warning(
                "Network error checking %s → us: %s — keeping", username, exc,
            )
            return "kept"
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) checking %s → us — waiting", exc.status_code,
                username,
            )
            if not self._wait_rate_limit(exc):
                return "shutdown"
            return "rate_limited"
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
            return "shutdown"

        if they_follow_us:
            db.mark_followback(username)
            log.info(
                "FOLLOWBACK detected during unfollow check: %s follows us — "
                "keeping them", username,
            )
            return "kept"

        # ── Safety 2: did they interact with the owner profile/repos? ──
        from services.interactions import user_has_interacted_with_owner

        owner_repo_names = db.owner_repository_names()
        try:
            interacted = user_has_interacted_with_owner(
                github, username, MY_USERNAME, owner_repo_names,
            )
        except GitHubNetworkError as exc:
            log.warning(
                "Network error checking %s events: %s — keeping", username, exc,
            )
            return "kept"
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) checking %s events — waiting",
                exc.status_code, username,
            )
            if not self._wait_rate_limit(exc):
                return "shutdown"
            return "rate_limited"
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
            return "shutdown"

        if interacted:
            log.info(
                "%s interacted with the owner — keeping (no unfollow)", username,
            )
            return "kept"

        # ── Unfollow ──
        try:
            if github.unfollow(username):
                db.mark_unfollowed_no_interaction(username)
                mark_action(db, WORKER_KEY)
                log.info(
                    "Unfollowed %s (followed %dd+, no interaction) "
                    "[%d/%d today]",
                    username, self._min_age_days,
                    db.today_social_actions(), DAILY_FOLLOW_LIMIT,
                )
                return "unfollowed"
            log.warning("Failed to unfollow %s", username)
            return "skipped"
        except GitHubNetworkError as exc:
            log.warning(
                "Network error unfollowing %s: %s — keeping", username, exc,
            )
            return "kept"
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) unfollowing %s — waiting",
                exc.status_code, username,
            )
            if not self._wait_rate_limit(exc):
                return "shutdown"
            return "rate_limited"
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
            return "shutdown"

    # ------------------------------------------------------------------
    # Rate-limit / auth handling
    # ------------------------------------------------------------------

    def _wait_rate_limit(self, exc):
        """Sleep out a follow-related rate limit (mirrors FollowWorker)."""
        raw = getattr(exc, "retry_after", None)
        if raw is None and getattr(exc, "reset_at", None) is not None:
            raw = exc.reset_at - int(time.time())

        if raw is not None and raw >= LONG_HINT_THRESHOLD:
            wait = _COOLDOWN_SECONDS
            log.warning(
                "Unfollow rate-limited (%s) — long block, cooling down %ds.",
                exc.verdict, wait,
            )
        else:
            wait = first_wait_from_headers(exc, fallback_seconds=5 * 60)
            log.warning(
                "Unfollow rate-limited (%s) — waiting %ds before retrying",
                exc.verdict, wait,
            )
        return self._sleep(wait)

    def _abort_on_auth_error(self, exc):
        """Log a loud fatal message and request graceful shutdown.

        GitHubAuthError is non-recoverable (revoked/expired token) — the
        whole bot is dead, so we signal the shared shutdown event exactly
        like FollowWorker / SilentRunner.
        """
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
            # Keep the liveness heartbeat fresh even during long waits.
            touch_heartbeat(getattr(self, "_db", None), WORKER_KEY)
            remaining = deadline - time.monotonic()
            time.sleep(max(0.0, min(1.0, remaining)))
        return not self._shutdown.is_set()

    @staticmethod
    def _seconds_until_midnight(db):
        """Seconds until the user's local midnight (daily budget reset)."""
        from core.tz import local_midnight_utc

        now = datetime.now(timezone.utc)
        next_midnight = local_midnight_utc(db, days_ago=-1)
        return max(60.0, (next_midnight - now).total_seconds())
