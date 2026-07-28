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

from config import MY_USERNAME, OWNER_SYNC_DAYS
from github_client import GitHubRateLimitError
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
    # Rate-limit handling
    # ------------------------------------------------------------------

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
    # Owner profile sync (TTL-based)
    # ------------------------------------------------------------------

    def sync_owner(self):
        """Re-sync owner repos if the last fetch is older than OWNER_SYNC_DAYS.

        Always runs — but skips the expensive API calls when data is
        still fresh.  Updates repos_fetched_at on success.
        """
        if self.db.is_repos_fresh(MY_USERNAME, days=OWNER_SYNC_DAYS):
            log.info(
                "Owner repos are fresh — skipping re-collection."
            )
            return

        self.db.add_user(MY_USERNAME, "self", owner=True)
        self.db.set_owner(MY_USERNAME)
        print(f"Syncing owner data for {MY_USERNAME} ...")
        log.info("Collecting owner repos for %s", MY_USERNAME)
        self._fetch_user_repos(MY_USERNAME, 1, 1)
        self.db.mark_repos_fetched(MY_USERNAME)
        log.info("Owner data collection complete.")
        print("Owner data collected.")

    # ------------------------------------------------------------------
    # Owner followers — FOLLOWBACK detection (runs every time)
    # ------------------------------------------------------------------

    def scan_owner_followers(self):
        """Check who is following the owner right now.

        * New followers (not in DB) are added as organic discoveries.
        * Existing followers with status FOLLOWED → marked FOLLOWBACK.
        * Total count stored for incremental future scans.
        """
        owner = self.db.get_owner()
        if not owner:
            log.info("No owner set — skipping owner follower scan.")
            return

        print("Checking owner's followers ...")
        log.info("Scanning followers of owner: %s", owner)

        try:
            people = self.github.followers(owner)
        except GitHubRateLimitError as exc:
            log.warning("Rate limit (%s) fetching owner followers", exc.status_code)
            print(f"  ⚠ Rate limit ({exc.status_code}) — skipping owner follower check")
            return

        added = 0
        followbacks = 0

        for person in people:
            username = person["login"]
            existing = self.db.get_user_cached_info(username)

            if existing is None:
                # New follower — discovered organically
                self.db.add_user(username, "owner_followers")
                added += 1
                continue

            # FOLLOWBACK detection:
            # We followed this person AND they are now following us back
            if existing.get("status") == "FOLLOWED":
                self.db.mark_followback(username)
                followbacks += 1
                log.info("FOLLOWBACK detected: %s", username)
                print(f"  🔄 Followback: {username}")

        # Store follower count for incremental detection
        self.db.store_followers_count(owner, len(people))

        log.info(
            "Owner follower scan: %d total, %d new, %d followbacks",
            len(people), added, followbacks,
        )
        print(
            f"  Followers: {len(people)} total, {added} new, {followbacks} followbacks"
        )

    # ------------------------------------------------------------------
    # Phase 1 — incremental graph discovery
    # ------------------------------------------------------------------

    def _discover(self):
        """Discover new users from the follower graph.

        For each known non-owner user whose stored follower count is
        less than the current API count, fetch followers and add new
        ones.  Stops pagination early when a full page is already known.
        """
        print("Incremental follower discovery ...")
        log.info("Phase 1 started: incremental follower discovery")

        rows = self.db.conn.execute(
            """
            SELECT username, followers_count FROM users
            WHERE owner = 0 AND status != 'DELETED' AND repos_fetched_at IS NOT NULL
            """
        ).fetchall()

        total = len(rows)
        new_total = 0

        for idx, (username, stored_count) in enumerate(rows, 1):
            if self._shutdown.is_set():
                log.info("Shutdown requested — stopping discovery.")
                print("\nShutdown requested — stopping discovery.")
                break

            # ── Fetch current follower count (1 API call) ──
            try:
                info = self.github.user(username)
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on user %s", exc.status_code, username)
                print(f"\n  ⚠ Rate limit ({exc.status_code}) on {username}")
                result = self._handle_rate_limit(exc)
                if result == "shutdown":
                    return
                continue

            if not info:
                log.warning("User %s returned no data — skipping", username)
                continue

            current_count = info.get("followers", 0)

            # ── Skip if follower count hasn't grown ──
            if stored_count and current_count <= stored_count:
                log.debug(
                    "Skipping %s: followers %d ≤ stored %d",
                    username, current_count, stored_count,
                )
                continue

            # ── Follower count grew — fetch and process new followers ──
            print(f"[{idx}/{total}] {username} (+{current_count - (stored_count or 0)} followers)")
            log.debug(
                "Scanning followers of %s (stored=%d, current=%d)",
                username, stored_count, current_count,
            )

            try:
                followers = self.github.followers(username)
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on followers of %s", exc.status_code, username)
                print(f"\n  ⚠ Rate limit ({exc.status_code}) on followers of {username}")
                result = self._handle_rate_limit(exc)
                if result == "shutdown":
                    return
                continue

            new_count = 0
            for user in followers:
                uname = user["login"]
                if uname == MY_USERNAME:
                    continue

                existing = self.db.conn.execute(
                    "SELECT 1 FROM users WHERE username = ?", (uname,)
                ).fetchone()

                if existing is None:
                    self.db.add_user(uname, username)
                    new_count += 1

            # Update stored count + scan timestamp
            self.db.store_followers_count(username, current_count)

            if new_count:
                print(f"  → {new_count} new users from {username}")
                new_total += new_count
            else:
                log.debug("No new followers for %s", username)

            self._sleep(1)

        if self._shutdown.is_set():
            log.info("Phase 1 interrupted by shutdown.")
        else:
            log.info("Phase 1 finished: %d new users discovered.", new_total)
            print(f"\nDiscovery complete: {new_total} new users.")



    # ------------------------------------------------------------------
    # Phase 2 — repository & language collection (with retry)
    # ------------------------------------------------------------------

    def _collect_repos(self):
        """Fetch repos and language data for all users needing update.

        Only processes users whose repos_fetched_at is NULL or older
        than REPO_FRESHNESS_DAYS.

        On HTTP 401/403 (rate-limit) retries the failing request up to
        3 times with delays of 5, 10 and 15 minutes.  If the request
        still fails, pauses all collection for 2 hours, then resumes.
        """
        rows = self.db.users_for_repo_collection()

        total = len(rows)
        log.info("Phase 2 started: fetching repos for %d users (stale/never-fetched)", total)

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
        """Fetch repos for a single user with rate-limit retry logic.

        Skips if repos were fetched recently (TTL check).
        """
        # ── Check if repos are fresh ──
        if self.db.is_repos_fresh(username):
            log.debug("[%d/%d] Repos for %s are fresh — skipping", idx, total, username)
            return

        while True:  # outer loop: 2-hour cooldown retries
            if self._shutdown.is_set():
                return

            print(f"[{idx}/{total}] Fetching repos for {username} ...")
            log.debug("[%d/%d] Fetching repos for %s", idx, total, username)

            # ── Check user exists on GitHub ──
            try:
                info = self.github.user(username)
            except GitHubRateLimitError as exc:
                log.warning(
                    "Rate limit (%s) checking user %s",
                    exc.status_code, username,
                )
                print(f"\n  ⚠ Rate limit ({exc.status_code}) on {username}")
                result = self._handle_rate_limit(exc)
                if result == "shutdown":
                    return
                continue

            if info is None:
                self.db.conn.execute(
                    "UPDATE users SET status = 'DELETED' WHERE username = ?",
                    (username,),
                )
                self.db.conn.commit()
                log.info("User %s not found — marked DELETED", username)
                return

            # ── Fetch repos ──
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

            # ── Mark repos as fetched (TTL) ──
            self.db.mark_repos_fetched(username)

            self._sleep(1)
            break  # user done, move to next

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def collect_users(self):
        """Phase 1: discover new users by traversing follower graphs."""
        self._discover()

    def collect_repos(self):
        """Phase 2: fetch repos and language data for all users needing update."""
        self._collect_repos()

    def collect_self(self):
        """Collect repos, languages, and topics for the owner (MY_USERNAME).

        Always fetches fresh data (ignores TTL) — used with --collect-self.
        """
        self.db.add_user(MY_USERNAME, "self", owner=True)
        self.db.set_owner(MY_USERNAME)
        print(f"Collecting data for owner: {MY_USERNAME}")
        log.info("Collecting owner data for %s", MY_USERNAME)
        self._fetch_user_repos(MY_USERNAME, 1, 1)
        self.db.mark_repos_fetched(MY_USERNAME)
        log.info("Owner data collection complete.")
        print("Owner data collected.")

    def run(self):
        """Run both phases: discover users then collect repos."""
        self._discover()
        self._collect_repos()
