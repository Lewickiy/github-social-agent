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

from core.config import (
    MY_USERNAME,
    OWNER_SYNC_DAYS,
    READ_HEAVY_FORK_LANGUAGES,
    SKIP_FORK_LANGUAGES,
    SKIP_LANGS_FORK_SIZE_KB,
    SKIP_LANGS_MAX_SIZE_KB,
)
from core.github_client import (
    FALLBACK_RETRY_DELAYS,
    GitHubAuthError,
    GitHubNetworkError,
    GitHubNotModified,
    GitHubRateLimitError,
    LONG_HINT_THRESHOLD,
    first_wait_from_headers,
    should_fetch_languages,
)
from core.logger import get_logger

log = get_logger(__name__)

# Cooldown after a failed retry cycle.  LONG_HINT_THRESHOLD (skip-to-cooldown
# cutoff when Retry-After is huge) and FALLBACK_RETRY_DELAYS (no-header
# schedule used as fallback_seconds) are imported from github_client above.
_COOLDOWN_DELAY = 1 * 60 * 60                  # 1 hour


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
            time.sleep(max(0.0, min(1.0, remaining)))
        return not self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Rate-limit handling
    # ------------------------------------------------------------------

    def _handle_rate_limit(self, exc):
        """Run the 3-retry + 1-hour-cooldown cycle.

        First delay is derived from the GitHub ``Retry-After`` /
        ``X-RateLimit-Reset`` header attached to *exc*.  If the server
        hint exceeds ``LONG_HINT_THRESHOLD`` (e.g. a 1-hour secondary
        ban), we skip the retry loop and go straight to cooldown to
        avoid pointless hammering.

        Returns
        -------
        "shutdown"
            Graceful shutdown was requested during the wait.
        "cooldown_done"
            The 1-hour cooldown finished — caller should retry.
        """
        first_wait = first_wait_from_headers(exc, fallback_seconds=FALLBACK_RETRY_DELAYS[0])

        # Shortcut: if GitHub told us to wait longer than the cooldown,
        # burning 3 retries will just hammer the limit again.  Skip them.
        if first_wait >= LONG_HINT_THRESHOLD:
            log.warning(
                "Retry-After=%ds exceeds %ds — bypassing retry loop, cooling down.",
                first_wait, COOLDOWN_DELAY,
            )
            print(
                f"  ⏭  Retry-After {first_wait}s ≥ cooldown; skipping retries."
            )
            if not self._sleep(_COOLDOWN_DELAY):
                return "shutdown"
            print("  ▶ Cooldown finished — resuming collection.\n")
            return "cooldown_done"

        # Exponential backoff (matches SilentRunner._handle_rate_limit).
        delays = [
            first_wait,
            min(first_wait * 2, 15 * 60),
            min(first_wait * 4, 15 * 60),
        ]
        for attempt, delay in enumerate(delays, 1):
            log.warning(
                "Retry %d/3 — waiting %ds (HTTP %s on %s, verdict=%s)",
                attempt, int(delay), exc.status_code, exc.url,
                getattr(exc, "verdict", "unknown"),
            )
            print(
                f"  ↻ Retry {attempt}/3 waiting {int(delay)}s "
                f"(status {exc.status_code}) ..."
            )
            if not self._sleep(delay):
                return "shutdown"

        # --- 1-hour cooldown ---
        log.warning(
            "All 3 retries exhausted for %s — cooling down for 1 hour",
            exc.url,
        )
        print("  ⏳ All retries exhausted. Cooling down for 1 hour ...")
        if not self._sleep(_COOLDOWN_DELAY):
            return "shutdown"

        log.info("1-hour cooldown finished — resuming collection.")
        print("  ▶ Cooldown finished — resuming collection.\n")
        return "cooldown_done"

    # ------------------------------------------------------------------
    # Auth error (fatal — token invalid/expired)
    # ------------------------------------------------------------------

    def _abort_on_auth_error(self, exc):
        """Log a fatal message and signal graceful shutdown.

        Mirrors SilentRunner._abort_on_auth_error — a 401 from GitHub
        means the token is no longer valid, so retrying is pointless.
        """
        log.error(
            "AUTH FAILURE (%s) on %s — token revoked/expired. Aborting run.",
            exc.status_code, exc.url,
        )
        print("\n❌ AUTH FAILURE — GitHub rejected the token (revoked/expired).")
        print("   Refresh GITHUB_TOKEN in your .env and restart.\n")
        self._shutdown.set()

    # ------------------------------------------------------------------
    # Network-error handling
    # ------------------------------------------------------------------

    def _handle_network_error(self, exc):
        """3 retries for transient network errors, then skip.

        Returns "shutdown" or "retry_exhausted".
        Unlike rate limits we don't do a long cooldown — network
        glitches are usually short-lived.
        """
        delays = [10, 30, 90]
        for attempt, delay in enumerate(delays, 1):
            log.warning(
                "Network error (attempt %d/3) on %s: %s — waiting %ds",
                attempt, exc.url, exc.original, delay,
            )
            print(f"  🌐 Network error — retry {attempt}/3 waiting {delay}s ...")
            if not self._sleep(delay):
                return "shutdown"
        log.warning("Network retries exhausted for %s — skipping", exc.url)
        print("  ⏭  Network retries exhausted — skipping.")
        return "retry_exhausted"

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

    def scan_owner_followers(self, verbose=True):
        """Check who is following the owner right now.

        * New followers (not in DB) are added as organic discoveries.
        * Existing followers with status FOLLOWED → marked FOLLOWBACK.
        * Total count stored for incremental future scans.

        Parameters
        ----------
        verbose : bool
            If True, print progress messages (default). Set to False for
            periodic background scans to keep console output clean.

        Returns
        -------
        list[str]
            Usernames of newly discovered followers (empty list if none or error).
        """
        owner = self.db.get_owner()
        if not owner:
            log.info("No owner set — skipping owner follower scan.")
            return []

        if verbose:
            print("Checking owner's followers ...")
        log.info("Scanning followers of owner: %s", owner)

        try:
            people = self.github.followers(owner)
        except GitHubAuthError as exc:
            self._abort_on_auth_error(exc)
            return []
        except GitHubNetworkError as exc:
            log.warning("Network error fetching owner followers: %s", exc)
            if verbose:
                print(f"  🌐 Network error — skipping owner follower check")
            return []
        except GitHubRateLimitError as exc:
            log.warning("Rate limit (%s) fetching owner followers", exc.status_code)
            if verbose:
                print(f"  ⚠ Rate limit ({exc.status_code}) — skipping owner follower check")
            return []




        added = 0
        followbacks = 0
        new_usernames = []

        for person in people:
            username = person["login"]
            existing = self.db.get_user_cached_info(username)

            if existing is None:
                # New follower — discovered organically
                self.db.add_user(username, "owner_followers")
                added += 1
                new_usernames.append(username)
                continue

            # FOLLOWBACK detection:
            # We followed this person AND they are now following us back
            if existing.get("status") == "FOLLOWED":
                self.db.mark_followback(username)
                followbacks += 1
                log.info("FOLLOWBACK detected: %s", username)
                if verbose:
                    print(f"  🔄 Followback: {username}")

        # Store follower count for incremental detection
        self.db.store_followers_count(owner, len(people))

        log.info(
            "Owner follower scan: %d total, %d new, %d followbacks",
            len(people), added, followbacks,
        )
        if verbose:
            print(
                f"  Followers: {len(people)} total, {added} new, {followbacks} followbacks"
            )

        return new_usernames

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
            SELECT u.username, u.followers_count
            FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE u.owner = 0
              AND COALESCE(c.status, 'NEW') != 'DELETED'
              AND u.repos_fetched_at IS NOT NULL
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
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubNetworkError as exc:
                result = self._handle_network_error(exc)
                if result == "shutdown":
                    return
                continue
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
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubNetworkError as exc:
                result = self._handle_network_error(exc)
                if result == "shutdown":
                    return
                continue
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
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubNetworkError as exc:
                result = self._handle_network_error(exc)
                if result == "shutdown":
                    return
                continue
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
                self.db.mark_deleted(username)
                log.info("User %s not found — marked DELETED", username)
                return

            # ── Parse @-mentions from company field ──
            company_text = info.get("company")
            if company_text:
                self.db.extract_and_save_companies(username, company_text)

            # ── Fetch repos (ETag conditional — 304 = unchanged, free) ──
            repos_etag = self.db.get_repos_etag(username)
            try:
                repos = self.github.repos(username, etag=repos_etag)
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubNetworkError as exc:
                result = self._handle_network_error(exc)
                if result == "shutdown":
                    return
                continue
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
            except GitHubNotModified:
                # Repo list unchanged since last fetch — nothing new to
                # collect.  Backfill any repos whose languages were missed
                # in a previous aborted run, then refresh the user-level
                # TTL so the user leaves the queue.
                log.debug("[%d/%d] Repos for %s unchanged (304) — skipping", idx, total, username)
                self._backfill_missing_languages(username)
                self.db.mark_repos_fetched(username)
                return

            # Persist the collection ETag for the next (free) 304.
            if self.github.last_etag:
                self.db.set_repos_etag(username, self.github.last_etag)

            for repo in repos:
                if self._shutdown.is_set():
                    return

                repo_id, _ = self.db.save_repository(username, repo)

                # Heavy / fork filter — skip /languages for heavyweight
                # repos / large forks (avoids secondary-rate-limit triggers).
                # When SKIP_FORK_LANGUAGES is True (default) /languages is
                # skipped for ALL forks (documentation/API_using.md §7.2).
                _should_lang = should_fetch_languages(
                    repo,
                    fork_threshold_kb=SKIP_LANGS_FORK_SIZE_KB,
                    max_threshold_kb=SKIP_LANGS_MAX_SIZE_KB,
                    read_heavy_forks=READ_HEAVY_FORK_LANGUAGES,
                    skip_forks=SKIP_FORK_LANGUAGES,
                )

                if _should_lang:
                    # Languages (ETag conditional — 304 = unchanged, free)
                    lang_etag = self.db.get_languages_etag(repo_id)
                    try:
                        langs = self.github.repo_languages(username, repo["name"], etag=lang_etag)
                    except GitHubAuthError as exc:
                        self._abort_on_auth_error(exc)
                        return
                    except GitHubNetworkError as exc:
                        result = self._handle_network_error(exc)
                        if result == "shutdown":
                            return
                        break
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
                    except GitHubNotModified:
                        # Languages unchanged — cached copy is still valid.
                        log.debug("Collector: %s/%s languages unchanged (304)", username, repo["name"])
                        langs = None
                    else:
                        # Persist the fresh ETag so the next run is a free 304.
                        if self.github.last_etag:
                            self.db.set_languages_etag(repo_id, self.github.last_etag)

                    if langs:
                        self.db.save_repository_languages(repo_id, langs)
                else:
                    log.debug(
                        "Collector: %s/%s heavy/fork — no /languages call "
                        "(size=%sKB, fork=%s)",
                        username, repo["name"],
                        repo.get("size", 0), repo.get("fork", False),
                    )

                # Topics — always processed (free, comes from /users/.../repos)
                topics = repo.get("topics", [])
                if topics:
                    self.db.save_repository_topics(repo_id, topics)

            if self._shutdown.is_set():
                return

            # ── Mark repos as fetched (TTL) ──
            self.db.mark_repos_fetched(username)

            self._sleep(1)
            break  # user done, move to next

    def _backfill_missing_languages(self, username):
        """Fetch languages for repos that never got them (last_checked_at NULL).

        Called on the repos-304 fast-path: when the repo list is unchanged
        we still want to complete language data for repos missed in a
        previous aborted run (rate limit / network error mid-user), so
        scoring stays accurate.  Forks are skipped when
        ``SKIP_FORK_LANGUAGES`` is True.
        """
        missing = self.db.repos_needing_languages(
            username, skip_forks=SKIP_FORK_LANGUAGES,
        )
        for repo_id, repo_name in missing:
            if self._shutdown.is_set():
                return
            lang_etag = self.db.get_languages_etag(repo_id)
            try:
                langs = self.github.repo_languages(username, repo_name, etag=lang_etag)
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubNetworkError as exc:
                result = self._handle_network_error(exc)
                if result == "shutdown":
                    return
                break
            except GitHubRateLimitError as exc:
                log.warning(
                    "Rate limit (%s) fetching languages for %s/%s",
                    exc.status_code, username, repo_name,
                )
                print(f"\n  ⚠ Rate limit ({exc.status_code}) on languages for {username}/{repo_name}")
                result = self._handle_rate_limit(exc)
                if result == "shutdown":
                    return
                break
            except GitHubNotModified:
                langs = None
            else:
                if self.github.last_etag:
                    self.db.set_languages_etag(repo_id, self.github.last_etag)
            if langs:
                self.db.save_repository_languages(repo_id, langs)
            # Repo verified now (real fetch or free 304) — start per-repo TTL.
            self.db.mark_repo_checked(repo_id)
            # Keep the same cadence as silent's backfill so a batch of
            # previously-missed repos doesn't hammer /languages back-to-back.
            if not self._sleep(1):
                return

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
