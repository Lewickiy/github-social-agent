"""Background worker — periodic mutual-follow health check.

Runs once per hour and verifies that confirmed mutual-follow users
(``FOLLOWBACK``) haven't unfollowed us.

Usage in main.py::

    from workers.followback_check_worker import FollowbackCheckWorker
    worker = FollowbackCheckWorker(shutdown_event)
    worker.start()
"""

import threading
import time

from core.config import FOLLOWBACK_CHECK_INTERVAL_HOURS, MY_USERNAME
from core.logger import get_logger

log = get_logger(__name__)


class FollowbackCheckWorker(threading.Thread):
    """Daemon thread that detects users who unfollowed after a mutual follow.

    Creates its own Database and GithubClient instances to avoid
    SQLite thread-safety issues.

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    check_interval_hours : int, optional
        Hours between check cycles.  Default from config
        ``FOLLOWBACK_CHECK_INTERVAL_HOURS`` (1 hour).
    """

    def __init__(self, shutdown_event, check_interval_hours=FOLLOWBACK_CHECK_INTERVAL_HOURS):
        super().__init__(daemon=True, name="FollowbackCheckWorker")
        self._shutdown = shutdown_event
        self._interval = check_interval_hours * 3600

    def run(self):
        # Import inside thread to avoid circular imports at module level
        from core.database import Database
        from core.github_client import GithubClient

        db = Database()
        github = GithubClient()

        log.info(
            "Followback check worker started (interval=%ds)", self._interval,
        )

        while not self._shutdown.is_set():
            try:
                self._check_all(db, github)
            except Exception:
                log.exception("Followback check worker error")

            # Interruptible sleep
            deadline = time.monotonic() + self._interval
            while time.monotonic() < deadline and not self._shutdown.is_set():
                time.sleep(1)

        db.conn.close()
        log.info("Followback check worker stopped.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_all(self, db, github):
        """Walk every mutual-follow user and detect unfollowers."""
        from core.github_client import GitHubNetworkError, GitHubRateLimitError

        users = db.get_mutual_follow_users()
        if not users:
            log.debug("No mutual-follow users to check.")
            return

        log.info("Checking %d mutual-follow user(s) ...", len(users))
        unfollowed = 0

        for username in users:
            if self._shutdown.is_set():
                return

            # ── Check: does the user still follow us? ──
            try:
                they_follow_us = github.does_user_follow_us(
                    username, MY_USERNAME,
                )
            except GitHubNetworkError as exc:
                log.warning(
                    "Network error checking %s → us: %s — skipping",
                    username, exc,
                )
                continue
            except GitHubRateLimitError as exc:
                log.warning(
                    "Rate limit (%s) checking %s → us — pausing 60s",
                    exc.status_code, username,
                )
                if self._shutdown.is_set():
                    return
                time.sleep(60)
                continue

            if not they_follow_us:
                # User unfollowed us — update status.
                db.mark_unfollowed_after_mutual(username)
                unfollowed += 1
                log.info(
                    "UNFOLLOWED_AFTER_MUTUAL_FOLLOW: %s unfollowed us.",
                    username,
                )
                # No console print — background workers stay silent.
                continue

            # ── Check: do we still follow them? ──
            try:
                we_follow_them = github.already_following(username)
            except GitHubNetworkError as exc:
                log.warning(
                    "Network error checking us → %s: %s — skipping",
                    username, exc,
                )
                continue
            except GitHubRateLimitError as exc:
                log.warning(
                    "Rate limit (%s) checking us → %s — pausing 60s",
                    exc.status_code, username,
                )
                if self._shutdown.is_set():
                    return
                time.sleep(60)
                continue

            if not we_follow_them:
                # We unfollowed them — mutual follow broken, but the
                # requirement only covers the "user unfollowed us" case.
                # Log and skip — no status change.
                log.info(
                    "Mutual follow broken: we unfollowed %s (no status change).",
                    username,
                )
                time.sleep(1)
                continue

            # Both checks passed — mutual follow holds.
            log.debug("%s — mutual follow intact", username)

            # Small delay between API calls to avoid secondary rate limits
            if self._shutdown.is_set():
                return
            time.sleep(1)

        if unfollowed:
            log.info("Followback check: %d unfollow(s) detected.", unfollowed)
        else:
            log.debug("Followback check: all mutual follows intact.")
