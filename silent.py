"""Silent-mode runner — collect repos + score users with human-like delays.

Designed to look like individual user browsing, not a mass crawl.
All delays are configurable in config.py (SILENT_* settings).
"""

import random
import threading
import time

from config import (
    SILENT_DELAY_BETWEEN_USERS,
    SILENT_DELAY_BETWEEN_REPOS,
    SILENT_DELAY_BETWEEN_REQUESTS,
    SILENT_DELAY_BETWEEN_SCORES,
)
from github_client import GitHubRateLimitError
from logger import get_logger
from scorer import Scorer

log = get_logger(__name__)


def _jitter(seconds, factor=0.3):
    """Add ±factor jitter to *seconds* to look more natural.

    Clamps the lower bound to at least 1 second to avoid near-zero
    delays that would defeat the stealth purpose.
    """
    lo = max(seconds * (1 - factor), 1)
    hi = seconds * (1 + factor)
    return random.uniform(lo, hi)


class SilentRunner:
    """Collect repos/languages/topics and score users with stealth delays.

    Parameters
    ----------
    db : Database
    github : GithubClient
    shutdown_event : threading.Event, optional
    """

    def __init__(self, db, github, shutdown_event=None):
        self.db = db
        self.github = github
        self._shutdown = shutdown_event or threading.Event()

    # ------------------------------------------------------------------
    # Interruptible sleep (with jitter)
    # ------------------------------------------------------------------

    def _sleep(self, seconds):
        """Sleep with jitter, waking early on shutdown."""
        actual = _jitter(seconds)
        deadline = time.monotonic() + actual
        while time.monotonic() < deadline:
            if self._shutdown.is_set():
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(1.0, remaining))
        return not self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Rate-limit handling (same logic as Collector)
    # ------------------------------------------------------------------

    def _handle_rate_limit(self, exc):
        """3 retries (5/10/15 min) then 2-hour cooldown."""
        delays = [5 * 60, 10 * 60, 15 * 60]
        for attempt, delay in enumerate(delays, 1):
            log.warning(
                "Silent retry %d/3 — waiting %d min (HTTP %s on %s)",
                attempt, delay // 60, exc.status_code, exc.url,
            )
            print(
                f"  ↻ Retry {attempt}/3 in {delay // 60} min "
                f"(HTTP {exc.status_code}) ..."
            )
            if not self._sleep(delay):
                return "shutdown"

        cooldown = 2 * 60 * 60
        log.warning("All retries exhausted for %s — cooling down 2 hours", exc.url)
        print("  ⏳ Cooling down for 2 hours ...")
        if not self._sleep(cooldown):
            return "shutdown"

        print("  ▶ Resuming.\n")
        return "cooldown_done"

    # ------------------------------------------------------------------
    # Repo collection (one user at a time, stealth delays)
    # ------------------------------------------------------------------

    def _collect_user_repos(self, username, idx, total):
        """Fetch repos + languages + topics for one user."""
        while True:
            if self._shutdown.is_set():
                return

            print(f"[{idx}/{total}] {username}")
            log.debug("[%d/%d] Fetching repos for %s", idx, total, username)

            try:
                repos = self.github.repos(username)
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on repos for %s", exc.status_code, username)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return
                continue

            for repo in repos:
                if self._shutdown.is_set():
                    return

                repo_id = self.db.save_repository(username, repo)

                # Languages
                try:
                    langs = self.github.repo_languages(username, repo["name"])
                except GitHubRateLimitError as exc:
                    log.warning("Rate limit (%s) on langs %s/%s", exc.status_code, username, repo["name"])
                    print(f"  ⚠ Rate limit ({exc.status_code})")
                    if self._handle_rate_limit(exc) == "shutdown":
                        return
                    break
                if langs:
                    self.db.save_repository_languages(repo_id, langs)

                if not self._sleep(SILENT_DELAY_BETWEEN_REQUESTS):
                    return

                # Topics
                topics = repo.get("topics", [])
                if topics:
                    self.db.save_repository_topics(repo_id, topics)

                if not self._sleep(SILENT_DELAY_BETWEEN_REPOS):
                    return

            break  # user done

    # ------------------------------------------------------------------
    # Scoring (one user at a time)
    # ------------------------------------------------------------------

    def _score_user(self, username, owner_langs=None, owner_topics=None):
        """Score a single user with optional owner similarity data."""
        info = self.github.user(username)
        if info:
            lang_list = self.db.user_languages(username)
            topic_list = self.db.user_topics(username)
            repo_days = self.db.user_repo_recency(username)
            score = Scorer.calculate(
                info, lang_list, topic_list,
                owner_langs=owner_langs,
                owner_topics=owner_topics,
                repo_days=repo_days,
            )
            self.db.update_score(username, score, info)
            return True
        log.warning("No profile data for %s", username)
        return False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self):
        """Collect repos then score — all with stealth delays."""
        log.info("Silent mode started.")

        # --- Phase 1: collect repos for all known users ---
        rows = self.db.conn.execute(
            "SELECT username FROM users"
        ).fetchall()

        total = len(rows)
        print(f"Collecting repos for {total} users (silent) ...")
        log.info("Silent collection: %d users", total)

        for idx, (username,) in enumerate(rows, 1):
            if self._shutdown.is_set():
                print("\nShutdown requested.")
                break

            self._collect_user_repos(username, idx, total)

            if not self._sleep(SILENT_DELAY_BETWEEN_USERS):
                break

        if self._shutdown.is_set():
            log.info("Silent collection interrupted by shutdown.")
            return

        log.info("Silent collection finished. Starting scoring.")

        # --- Phase 2: score unscored users ---
        owner_username = self.db.get_owner()
        owner_langs = None
        owner_topics = None
        if owner_username:
            owner_langs = {
                name: pct
                for name, pct in self.db.user_languages(owner_username)
            }
            owner_topics = self.db.user_topics(owner_username)
            log.info("Owner profile loaded for scoring: %s", owner_username)

        users = self.db.unscored_users()
        if users:
            print(f"\nScoring {len(users)} users (silent) ...")
            log.info("Silent scoring: %d users", len(users))

            for (username,) in users:
                if self._shutdown.is_set():
                    print("\nShutdown requested.")
                    break

                print(f"  Scoring {username}")
                self._score_user(username, owner_langs, owner_topics)

                if not self._sleep(SILENT_DELAY_BETWEEN_SCORES):
                    break

        if self._shutdown.is_set():
            log.info("Silent mode interrupted by shutdown.")
        else:
            log.info("Silent mode complete.")
            print("\nDone.")
