"""Background worker — the system's only follow executor.

Follows queued candidates in a separate daemon thread, independently of
the analysis pipeline: ``SilentRunner`` / ``Collector`` only collect and
score users into the database, while this thread subscribes to users in
order of score — from the highest to the lowest, but never below
``SILENT_FOLLOW_SCORE_THRESHOLD`` — within the daily budget
(``DAILY_FOLLOW_LIMIT``) and with a random human-like pause of 20–30
minutes between subscriptions.

Guarantees:
  * **Sole follower** — no other worker or CLI mode follows users; the
    former ``--follow`` mode (FollowEngine) was removed so the daily
    budget has exactly one consumer.
  * **Fresh queue** — the candidate is re-selected from the DB before
    every follow, so new scores/statuses are always picked up.
  * **Deleted-skip** — if a candidate was marked DELETED since the queue
    query, it is skipped and the next candidate is picked.
  * **Idle wait** — when no qualifying (scored >= threshold) users exist,
    the worker simply waits and re-polls.

Usage in main.py::

    from workers.follow_worker import FollowWorker
    worker = FollowWorker(shutdown_event)
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
    FOLLOW_WORKER_POLL_INTERVAL_SECONDS,
    SILENT_FOLLOW_SCORE_THRESHOLD,
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
WORKER_KEY = "follow"

# Serialises the follow-queue drain.  The worker is the only follower in
# the system, so the lock is a single-consumer invariant — it stays so the
# check-then-act sequence (budget → already-following → follow) can never
# be raced by a future second consumer.
_FOLLOW_LOCK = threading.RLock()

# After this many consecutive rate-limit hits the worker stops retrying
# and sleeps a long cooldown (mirrors SilentRunner._handle_rate_limit).
_RATE_LIMIT_RETRIES = 3
_COOLDOWN_SECONDS = 2 * 60 * 60

# Short courtesy pause between "already following" re-confirmations (each
# costs one GET), so a backlog of already-followed users never bursts the
# API.  Real subscriptions are paced by the 20–30 min interval instead.
_ALREADY_DELAY_SECONDS = 3


class FollowWorker(threading.Thread):
    """Daemon thread that subscribes to the best scored candidates.

    Creates its own Database and GithubClient instances to avoid SQLite
    thread-safety issues (same pattern as the other workers).

    Each cycle picks the highest-scored NEW user with ``score >=
    threshold`` from a fresh DB query and follows them (after a fresh
    status re-check).  Between subscriptions it sleeps a random 20–30
    minutes; when the queue is empty it waits a short poll interval, and
    when the daily budget is exhausted it sleeps until local midnight.
    """

    def __init__(
        self,
        shutdown_event,
        threshold=SILENT_FOLLOW_SCORE_THRESHOLD,
        poll_interval=FOLLOW_WORKER_POLL_INTERVAL_SECONDS,
    ):
        super().__init__(daemon=True, name="FollowWorker")
        self._shutdown = shutdown_event
        self._threshold = threshold
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
            "Follow worker started (threshold=%d, interval=%d–%ds, "
            "daily limit=%d)",
            self._threshold,
            FOLLOW_INTERVAL_MIN_SECONDS, FOLLOW_INTERVAL_MAX_SECONDS,
            DAILY_FOLLOW_LIMIT,
        )
        mark_started(db, WORKER_KEY)

        try:
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("Follow worker paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue

                try:
                    outcome = self._drain_follow_queue(db, github)
                except Exception as exc:
                    mark_error(db, WORKER_KEY, f"{type(exc).__name__}: {exc}")
                    log.exception("Follow worker error")
                    outcome = "empty"

                if self._shutdown.is_set():
                    break

                if outcome == "exhausted":
                    log.info(
                        "Daily follow budget reached (%d/%d) — next attempt "
                        "after local midnight.",
                        db.today_follows(), DAILY_FOLLOW_LIMIT,
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
            log.info("Follow worker stopped.")

    # ------------------------------------------------------------------
    # Queue drain (best score first — fresh query per follow)
    # ------------------------------------------------------------------

    def _drain_follow_queue(self, db, github):
        """Follow queued users (score DESC) while the daily limit allows.

        Returns ``"exhausted"`` when the drain stopped because the daily
        budget is spent, ``"empty"`` when the queue simply ran dry (no
        scored candidates yet), ``"stopped"`` on shutdown / errors.
        """
        while not self._shutdown.is_set():
            # Toggled off mid-drain — stop following and let run() pause.
            if not is_enabled(db, WORKER_KEY):
                return "paused"

            # Fresh selection before every follow — the analysis pipeline
            # keeps updating scores and statuses in the DB, so the queue
            # is always re-read with the newest data.
            row = db.get_next_queued_user(self._threshold)
            if not row:
                return "empty"  # no scored candidates — wait and re-poll

            username, score = row

            # Fresh status re-check right before subscribing: the analysis
            # pipeline may have marked this user DELETED since the queue
            # query.  Skip to the next candidate in that case.
            if db.current_status(username) == "DELETED":
                log.info(
                    "Follow worker: %s was marked DELETED meanwhile — "
                    "moving to the next candidate", username,
                )
                continue

            outcome = self._follow_user(db, github, username, score)

            if outcome == "followed":
                self._rate_limit_streak = 0
                # Random human-like pause between subscriptions (20–30 min).
                wait = random.uniform(
                    FOLLOW_INTERVAL_MIN_SECONDS,
                    FOLLOW_INTERVAL_MAX_SECONDS,
                )
                if not self._sleep(wait):
                    return "stopped"
            elif outcome == "already":
                # Re-confirmed as FOLLOWED without a new subscription —
                # keep draining, but pace re-confirmations so a backlog of
                # already-followed users doesn't burst the API.
                self._rate_limit_streak = 0
                if not self._sleep(_ALREADY_DELAY_SECONDS):
                    return "stopped"
                continue
            elif outcome == "rate_limited":
                # Handled inside _follow_user (already waited) — retry the
                # same user on the next iteration, but back off into a long
                # cooldown after repeated hits (permanently exhausted limit).
                self._rate_limit_streak += 1
                if self._rate_limit_streak >= _RATE_LIMIT_RETRIES:
                    log.warning(
                        "Follow worker: %d consecutive rate limits — "
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

    def _follow_user(self, db, github, username, score):
        """Follow *username* if the daily budget allows.

        Serialised via ``_FOLLOW_LOCK`` so the check-then-act sequence can
        never be raced.  Returns one of ``"followed"``, ``"already"``,
        ``"no_budget"``, ``"rate_limited"``, ``"skipped"``, ``"shutdown"``.
        """
        with _FOLLOW_LOCK:
            return self._follow_user_unlocked(db, github, username, score)

    def _follow_user_unlocked(self, db, github, username, score):
        """Follow logic — run with ``_FOLLOW_LOCK`` already held."""
        done_today = db.today_follows()
        remaining = DAILY_FOLLOW_LIMIT - done_today
        if remaining <= 0:
            log.info(
                "Daily follow limit reached (%d/%d).",
                done_today, DAILY_FOLLOW_LIMIT,
            )
            return "no_budget"

        # ── Already following? Re-confirm without a FOLLOW event ──
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
            return "shutdown"

        if already:
            # Mark FOLLOWED so they don't stay in the queue forever.  Uses
            # the dedicated method (no FOLLOW action — this is a
            # re-confirmation, not a new follow, so the daily-follow
            # counter and activity feed stay accurate).
            db.mark_followed_existing(username)
            mark_action(db, WORKER_KEY)
            log.debug("Already following %s — marked FOLLOWED", username)
            return "already"

        # ── Follow ──
        try:
            if github.follow(username):
                db.mark_followed(username)
                mark_action(db, WORKER_KEY)
                log.info(
                    "Followed %s (score %d) [%d/%d]",
                    username, score, db.today_follows(), DAILY_FOLLOW_LIMIT,
                )
                return "followed"
            log.warning("Failed to follow %s", username)
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
            return "shutdown"

    # ------------------------------------------------------------------
    # Rate-limit / auth handling
    # ------------------------------------------------------------------

    def _wait_rate_limit(self, exc):
        """Sleep out a follow-related rate limit.

        When the server's hint says the block will be long (≥
        ``LONG_HINT_THRESHOLD``) we skip the retry loop and go straight to
        the 2-hour cooldown — retries would just hammer a still-blocked
        endpoint (mirrors ``SilentRunner._handle_rate_limit``).  Returns
        False when shutdown was requested while waiting.
        """
        raw = getattr(exc, "retry_after", None)
        if raw is None and getattr(exc, "reset_at", None) is not None:
            raw = exc.reset_at - int(time.time())

        if raw is not None and raw >= LONG_HINT_THRESHOLD:
            wait = _COOLDOWN_SECONDS
            log.warning(
                "Follow rate-limited (%s) — long block, cooling down %ds.",
                exc.verdict, wait,
            )
        else:
            wait = first_wait_from_headers(exc, fallback_seconds=5 * 60)
            log.warning(
                "Follow rate-limited (%s) — waiting %ds before retrying",
                exc.verdict, wait,
            )
        return self._sleep(wait)

    def _abort_on_auth_error(self, exc):
        """Log a loud fatal message and request graceful shutdown.

        GitHubAuthError is non-recoverable (revoked/expired token) — the
        whole bot is dead, so we signal the shared shutdown event exactly
        like ``SilentRunner._abort_on_auth_error``.
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
            # Keep the liveness heartbeat fresh even during long waits
            # (rate-limit cooldowns, overnight sleeps).
            touch_heartbeat(getattr(self, "_db", None), WORKER_KEY)
            remaining = deadline - time.monotonic()
            time.sleep(max(0.0, min(1.0, remaining)))
        return not self._shutdown.is_set()

    @staticmethod
    def _seconds_until_midnight(db):
        """Seconds until the user's local midnight (daily budget reset).

        ``local_midnight_utc(db, days_ago=-1)`` is the start of the *next*
        local day, so the wait is always positive (DST-safe).
        """
        from core.tz import local_midnight_utc

        now = datetime.now(timezone.utc)
        next_midnight = local_midnight_utc(db, days_ago=-1)
        return max(60.0, (next_midnight - now).total_seconds())
