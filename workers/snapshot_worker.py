"""Background worker — daily historical snapshots of mutable profile data.

Once a day (at ``SNAPSHOT_HOUR`` — noon by default, server local time)
it records a snapshot of the owner's follower count, following count,
public repo count and other changing profile fields into the
``user_snapshots`` table.

The snapshot logic is generic (``_snapshot_user`` works for any GitHub
username), so accumulating snapshots for *every* user later only means
changing ``_snapshot_targets`` — no schema or UI changes are needed.

Usage in main.py::

    from workers.snapshot_worker import SnapshotWorker
    worker = SnapshotWorker(shutdown_event)
    worker.start()

One-shot run (manual trigger)::

    from workers.snapshot_worker import SnapshotWorker
    SnapshotWorker.take_snapshot_now(db, github)
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from core.config import SNAPSHOT_HOUR, SNAPSHOT_ON_START
from core.github_client import GitHubAuthError, GitHubNetworkError, GitHubRateLimitError
from core.logger import get_logger

log = get_logger(__name__)

# Fields from the GitHub profile that change over time — captured in
# extra_json so future analytics don't need to re-fetch the API.
_MUTABLE_FIELDS = (
    "name", "company", "bio", "location", "blog", "email",
    "twitter_username", "hireable", "public_gists", "created_at",
    "updated_at",
)


class SnapshotWorker(threading.Thread):
    """Daemon thread that records the daily profile snapshot.

    Creates its own Database and GithubClient instances to avoid
    SQLite thread-safety issues.

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    snapshot_hour : int, optional
        Hour of day (server local time) for the scheduled snapshot.
        Default from config ``SNAPSHOT_HOUR`` (12 — noon).
    snapshot_on_start : bool, optional
        Record a snapshot immediately on startup as well.  Default from
        config ``SNAPSHOT_ON_START`` (True).
    """

    def __init__(self, shutdown_event, snapshot_hour=SNAPSHOT_HOUR,
                 snapshot_on_start=SNAPSHOT_ON_START):
        super().__init__(daemon=True, name="SnapshotWorker")
        self._shutdown = shutdown_event
        self._hour = snapshot_hour
        self._on_start = snapshot_on_start

    def run(self):
        from core.database import Database
        from core.github_client import GithubClient

        db = Database()
        github = GithubClient()

        log.info(
            "Snapshot worker started (hour=%02d:00, on_start=%s)",
            self._hour, self._on_start,
        )

        # Initial snapshot so today has a data point even when the bot
        # starts outside the noon window.
        if self._on_start:
            self._snapshot_cycle(db, github)

        while not self._shutdown.is_set():
            if not self._sleep_until_next_run():
                break
            if self._shutdown.is_set():
                break
            self._snapshot_cycle(db, github)

        db.conn.close()
        log.info("Snapshot worker stopped.")

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _sleep_until_next_run(self):
        """Sleep (interruptibly) until the next scheduled snapshot hour.

        Scheduling uses **UTC** to match the rest of the codebase and the
        Docker containers (which default to UTC — "noon" is 12:00 UTC).

        Returns False if interrupted by a shutdown request.
        """
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=self._hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        seconds = (next_run - now).total_seconds()
        log.info(
            "Snapshot worker — next snapshot at %s UTC (in %ds)",
            next_run.strftime("%Y-%m-%d %H:%M"), int(seconds),
        )

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._shutdown.is_set():
            time.sleep(1)
        return not self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Snapshot logic (generic — reusable for any user)
    # ------------------------------------------------------------------

    def _snapshot_targets(self, db):
        """Usernames to snapshot in this cycle.

        Today: only the owner.  Later, extend to all tracked users —
        e.g. ``SELECT username FROM users WHERE status != 'DELETED'`` —
        everything else (schema, writer, chart) already supports it.
        """
        owner = db.get_owner()
        return [owner] if owner else []

    def _snapshot_cycle(self, db, github):
        """Record snapshots for every target for today."""
        for username in self._snapshot_targets(db):
            if self._shutdown.is_set():
                return
            try:
                self._snapshot_user(db, github, username)
            except Exception:
                log.exception("Snapshot failed for %s", username)

    def _snapshot_user(self, db, github, username):
        """Fetch the current profile for *username* and store today's snapshot."""
        try:
            info = github.user(username)
        except GitHubAuthError as exc:
            log.error(
                "Snapshot: auth failure (%s) for %s — token invalid, skipping.",
                exc.status_code, username,
            )
            return
        except GitHubNetworkError as exc:
            log.warning("Snapshot: network error for %s: %s — skipping", username, exc)
            return
        except GitHubRateLimitError as exc:
            log.warning(
                "Snapshot: rate limit (%s) for %s — skipping", exc.status_code, username,
            )
            return

        if not info:
            log.warning("Snapshot: no profile data returned for %s — skipping", username)
            return

        extra = {key: info.get(key) for key in _MUTABLE_FIELDS if key in info}
        db.store_user_snapshot(
            username,
            followers=info.get("followers"),
            following=info.get("following"),
            public_repos=info.get("public_repos"),
            public_gists=info.get("public_gists"),
            extra=extra,
        )
        log.info(
            "Snapshot saved for %s: followers=%s following=%s repos=%s",
            username,
            info.get("followers"), info.get("following"), info.get("public_repos"),
        )

    # ------------------------------------------------------------------
    # Manual one-shot
    # ------------------------------------------------------------------


def take_snapshot_now(db, github):
    """Record a snapshot for every target immediately.

    Used by ``python main.py --snapshot``.  Also handy for testing.
    """
    worker = SnapshotWorker.__new__(SnapshotWorker)
    worker._shutdown = threading.Event()
    worker._snapshot_cycle(db, github)
