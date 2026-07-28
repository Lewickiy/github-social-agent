"""Discover new users by traversing follower graphs and collect
repository / language data for every known user.

Phase 2 (--collect-users-rep) includes retry logic for GitHub
rate-limit / auth errors (HTTP 401/403):

  - 3 retries with delays of 5, 10, and 15 minutes
  - After 3 failed retries, a 2-hour cooldown before resuming

All phases support graceful shutdown via Ctrl+C — the current user
is finished, all data is committed to the database, and the process
exits cleanly.
"""

import threading
import time

from config import MY_USERNAME
from github_client import GitHubRateLimitError
from logger import get_logger
from logger import get_logger

log = get_logger(__name__)

# Retry schedule: 3 attempts with delays of 5, 10, 15 minutes
_RETRY_DELAYS = [5 * 60, 10 * 60, 15 * 60]   # seconds
_COOLDOWN_DELAY = 2 * 60 * 60                  # 2 hours


class Collector:
    """Discover new users by traversing follower graphs.

    Parameters
    ----------
    db : Database
    github : GithubClient
    shutdown_event : threading.Event, optional
        When set, the collector finishes the current user and stops.
    """

    def __init__(self, db, github, shutdown_event=None):
        self.db = db
        self.github = github
        self._shutdown = shutdown_event or threading.Event()

    # ------------------------------------------------------------------
    # Interruptible sleep
    # ------------------------------------------------------------------

    def _sleep(self, seconds):
        """Sleep for *seconds*, waking early if shutdown is requested.

        Returns True if the full sleep completed, False if interrupted
        by a shutdown signal.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._shutdown.is_set():
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(1.0, remaining))
        return not self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Phase 1 — graph discovery
    # ------------------------------------------------------------------

    def _discover(self):
        print("Collecting graph...")
        log.info("Phase 1 started: collecting follower graph for %s", MY_USERNAME)

        try:
            people = self.github.followers(MY_USERNAME)
        except GitHubRateLimitError as exc:
            log.warning("Rate limit (%s) while fetching own followers", exc.status_code)
            print(f"\n  ⚠ Rate limit ({exc.status_code}) while fetching own followers")
            result = self._handle_rate_limit(exc)
            if result == "shutdown":
                return
            people = self.github.followers(MY_USERNAME)  # retry once

        for person in people:
            if self._shutdown.is_set():
                log.info("Shutdown requested — stopping graph discovery.")
                print("\nShutdown requested — stopping graph discovery.")
                break

            source = person["login"]
            print("scan", source)
            log.debug("Scanning followers of %s", source)

            try:
                followers = self.github.followers(source)
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on followers of %s", exc.status_code, source)
                print(f"\n  ⚠ Rate limit ({exc.status_code}) on followers of {source}")
                result = self._handle_rate_limit(exc)
                if result == "shutdown":
                    return
                continue  # skip this source, move to next

            for user in followers:
                username = user["login"]

                if username != MY_USERNAME:
                    self.db.add_user(username, source)

            time.sleep(1)

        if self._shutdown.is_set():
            log.info("Phase 1 interrupted by shutdown.")
        else:
            log.info("Phase 1 finished: graph discovery complete.")

    # ------------------------------------------------------------------
    # Phase 2 — repository & language collection (with retry)
    # ------------------------------------------------------------------

    def _collect_repos(self):
        """Fetch repos and language data for all known users.

        On HTTP 401/403 (rate-limit) retries the failing request up to
        3 times with delays of 5, 10 and 15 minutes.  If the request
        still fails, pauses all collection for 2 hours, then resumes.
        """
        rows = self.db.conn.execute(
            "SELECT username FROM users"
        ).fetchall()

        total = len(rows)
        log.info("Phase 2 started: fetching repos for %d users", total)

        for idx, (username,) in enumerate(rows, 1):
            if self._shutdown.is_set():
                log.info("Shutdown requested — stopping repo collection.")
                print("\nShutdown requested — stopping repo collection.")
                break

            self._fetch_user_repos(username, idx, total)

        if self._shutdown.is_set():
            log.info("Phase 2 interrupted by shutdown.")
        else:
            log.info("Phase 2 finished: repo collection complete.")

    def _fetch_user_repos(self, username, idx, total):
        """Fetch repos for a single user with rate-limit retry logic."""
        while True:  # outer loop: 2-hour cooldown retries
            if self._shutdown.is_set():
                return

            print(f"[{idx}/{total}] Fetching repos for {username} ...")
            log.debug("[%d/%d] Fetching repos for %s", idx, total, username)

            try:
                repos = self.github.repos(username)
            except GitHubRateLimitError as exc:
                log.warning(
                    "Rate limit (%s) fetching repos for %s — entering retry cycle",
                    exc.status_code, username,
                )
                print(f"\n  ⚠ Rate limit ({exc.status_code}) on repos for {username}")
                result = self._handle_rate_limit(exc)
                if result == "shutdown":
                    return
                # result == "cooldown_done" → retry the same user
                continue

            for repo in repos:
                if self._shutdown.is_set():
                    return

                repo_id, _ = self.db.save_repository(username, repo)

                try:
                    langs = self.github.repo_languages(username, repo["name"])
                except GitHubRateLimitError as exc:
                    log.warning(
                        "Rate limit (%s) fetching languages for %s/%s",
                        exc.status_code, username, repo["name"],
                    )
                    print(f"\n  ⚠ Rate limit ({exc.status_code}) on languages for {username}/{repo['name']}")
                    result = self._handle_rate_limit(exc)
                    if result == "shutdown":
                        return
                    # Rate limit on languages — skip remaining languages for
                    # this user but keep the already-saved repos.
                    break

                if langs:
                    self.db.save_repository_languages(repo_id, langs)

                topics = repo.get("topics", [])
                if topics:
                    self.db.save_repository_topics(repo_id, topics)

            if self._shutdown.is_set():
                return

            self._sleep(1)
            break  # user done, move to next

    def _handle_rate_limit(self, exc):
        """Run the 3-retry + 2-hour-cooldown cycle.

        Returns
        -------
        "shutdown"
            Graceful shutdown was requested during the wait.
        "cooldown_done"
            The 2-hour cooldown finished — caller should retry.
        """
        # --- 3 retries with 5 / 10 / 15-minute delays ---
        for attempt, delay in enumerate(_RETRY_DELAYS, 1):
            log.warning(
                "Retry %d/3 — waiting %d minutes (HTTP %s on %s)",
                attempt, delay // 60, exc.status_code, exc.url,
            )
            print(
                f"  ↻ Retry {attempt}/3 in {delay // 60} minutes "
                f"(status {exc.status_code}) ..."
            )
            if not self._sleep(delay):
                return "shutdown"

        # --- 2-hour cooldown ---
        log.warning(
            "All 3 retries exhausted for %s — cooling down for 2 hours",
            exc.url,
        )
        print("  ⏳ All retries exhausted. Cooling down for 2 hours ...")
        if not self._sleep(_COOLDOWN_DELAY):
            return "shutdown"

        log.info("2-hour cooldown finished — resuming collection.")
        print("  ▶ Cooldown finished — resuming collection.\n")
        return "cooldown_done"

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def collect_users(self):
        """Phase 1: discover new users by traversing follower graphs."""
        self._discover()

    def collect_repos(self):
        """Phase 2: fetch repos and language data for all known users."""
        self._collect_repos()

    def collect_self(self):
        """Collect repos, languages, and topics for the owner (MY_USERNAME)."""
        self.db.add_user(MY_USERNAME, "self", owner=True)
        self.db.set_owner(MY_USERNAME)
        print(f"Collecting data for owner: {MY_USERNAME}")
        log.info("Collecting owner data for %s", MY_USERNAME)
        self._fetch_user_repos(MY_USERNAME, 1, 1)
        log.info("Owner data collection complete.")
        print("Owner data collected.")

    def run(self):
        """Run both phases: discover users then collect repos."""
        self._discover()
        self._collect_repos()
