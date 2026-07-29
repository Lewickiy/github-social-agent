"""Silent-mode runner — collect repos + score users with human-like delays.

Designed to look like individual user browsing, not a mass crawl.
All delays are configurable in config.py (SILENT_* settings).
"""

import random
import threading
import time

from config import (
    DAILY_FOLLOW_LIMIT,
    OWNER_FOLLOWER_SCAN_INTERVAL,
    READ_HEAVY_FORK_LANGUAGES,
    SILENT_DELAY_BETWEEN_USERS,
    SILENT_DELAY_BETWEEN_REPOS,
    SILENT_DELAY_BETWEEN_REQUESTS,
    SILENT_DELAY_BETWEEN_SCORES,
    SILENT_DELAY_BETWEEN_FOLLOWS,
    SILENT_FOLLOW_SCORE_THRESHOLD,
    SILENT_REPO_CHECK_FRESHNESS_DAYS,
    SKIP_LANGS_FORK_SIZE_KB,
    SKIP_LANGS_MAX_SIZE_KB,
)
from collector import Collector
from github_client import (
    GitHubAuthError,
    GitHubRateLimitError,
    LONG_HINT_THRESHOLD,
    first_wait_from_headers,
    should_fetch_languages,
)
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
            time.sleep(max(0.0, min(1.0, remaining)))
        return not self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Rate-limit handling (same logic as Collector)
    # ------------------------------------------------------------------

    def _handle_rate_limit(self, exc):
        """3 retries (header-driven first delay) then 2-hour cooldown.

        The first wait is derived from the GitHub ``Retry-After`` /
        ``X-RateLimit-Reset`` header captured by
        :class:`GitHubRateLimitError`.  If the hint is long
        (≥ 30 min) we bypass the retry loop and go straight to
        cooldown — retries would just hammer a still-blocked endpoint.
        Subsequent waits double the previous one — capped at 15 min —
        so we back off even if the first hint was very small.
        """
        first_wait = first_wait_from_headers(exc, fallback_seconds=5 * 60)

        if first_wait >= LONG_HINT_THRESHOLD:
            log.warning(
                "Silent: Retry-After=%ds ≥ cooldown — skipping retry loop.",
                first_wait,
            )
            print(
                f"  ⏭  Retry-After {first_wait}s ≥ cooldown; skipping retries."
            )
            if not self._sleep(2 * 60 * 60):
                return "shutdown"
            print("  ▶ Resuming.\n")
            return "cooldown_done"

        delays = [
            first_wait,
            min(first_wait * 2, 15 * 60),
            min(first_wait * 4, 15 * 60),
        ]
        for attempt, delay in enumerate(delays, 1):
            log.warning(
                "Silent retry %d/3 — waiting %ds (HTTP %s on %s, verdict=%s)",
                attempt, int(delay), exc.status_code, exc.url,
                getattr(exc, "verdict", "unknown"),
            )
            print(
                f"  ↻ Retry {attempt}/3 waiting {int(delay)}s "
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
    # Auth error (fatal — token invalid/expired)
    # ------------------------------------------------------------------

    def _abort_on_auth_error(self, exc):
        """Log a loud fatal message and signal graceful shutdown.

        GitHubAuthError is non-recoverable (revoked/expired token),
        so we don't loop — we just stop and let the user rotate
        GITHUB_TOKEN before re-launching.
        """
        log.error(
            "AUTH FAILURE (%s) on %s — token revoked/expired. Aborting run.",
            exc.status_code, exc.url,
        )
        print("\n❌ AUTH FAILURE — GitHub rejected the token (revoked/expired).")
        print("   Refresh GITHUB_TOKEN in your .env and restart.\n")
        self._shutdown.set()

    # ------------------------------------------------------------------
    # Repo collection (one user at a time, stealth delays)
    # ------------------------------------------------------------------

    def _collect_user_repos(self, username, idx, total):
        """Fetch repos + languages + topics for one user.

        Skips if repos were fetched recently (TTL check).
        Skips if user is deleted.
        """
        # ── TTL check — skip if repos are fresh ──
        if self.db.is_repos_fresh(username):
            log.debug("[%d/%d] Repos for %s are fresh — skipping", idx, total, username)
            print(f"[{idx}/{total}] {username} (repos fresh — skipped)")
            return

        while True:
            if self._shutdown.is_set():
                return

            print(f"[{idx}/{total}] {username}")
            log.debug("[%d/%d] Fetching repos for %s", idx, total, username)

            # ── Check user exists ──
            try:
                info = self.github.user(username)
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) checking user %s", exc.status_code, username)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return
                continue

            if info is None:
                self.db.conn.execute(
                    "UPDATE users SET status = 'DELETED' WHERE username = ?",
                    (username,),
                )
                self.db.conn.commit()
                log.info("User %s not found — marked DELETED", username)
                print(f"  👻 {username} — deleted, skipped")
                return

            # ── Fetch repos ──
            try:
                repos = self.github.repos(username)
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on repos for %s", exc.status_code, username)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return
                continue

            for repo in repos:
                if self._shutdown.is_set():
                    return

                repo_name = repo["name"]
                repo_id, repo_changed = self.db.save_repository(username, repo)
                if repo_changed:
                    print(f"    📦 Repo: {repo_name}")
                else:
                    print(f"    📦 Repo: {repo_name} (unchanged)")

                # ── Per-repo TTL check: skip the /languages call if we
                #    already verified this repo within the freshness window.
                #    `last_checked_at` is NULL on first sight, so brand-new
                #    repos always pass this check.
                if self.db.is_repo_fresh(repo_id, SILENT_REPO_CHECK_FRESHNESS_DAYS):
                    log.debug(
                        "Silent: repo %s/%s checked within %d days — skipping",
                        username, repo_name, SILENT_REPO_CHECK_FRESHNESS_DAYS,
                    )
                    print(
                        f"      ⏭  Skipped (checked within "
                        f"{SILENT_REPO_CHECK_FRESHNESS_DAYS} days)"
                    )
                    # NOTE: do NOT call mark_repo_checked here — that would
                    # push a stale repo's window into the future and
                    # silently hide any genuine update from the API.
                    continue

                # ── Heavy / fork filter: skip /languages for heavyweight
                #    repos / large forks to avoid tripping GitHub's secondary
                #    rate limit (abuse detection).  Controlled by
                #    READ_HEAVY_FORK_LANGUAGES in config.py.
                _should_lang = should_fetch_languages(
                    repo,
                    fork_threshold_kb=SKIP_LANGS_FORK_SIZE_KB,
                    max_threshold_kb=SKIP_LANGS_MAX_SIZE_KB,
                    read_heavy_forks=READ_HEAVY_FORK_LANGUAGES,
                )

                if _should_lang:
                    # Languages
                    try:
                        langs = self.github.repo_languages(username, repo_name)
                    except GitHubAuthError as exc:
                        self._abort_on_auth_error(exc)
                        return
                    except GitHubRateLimitError as exc:
                        log.warning("Rate limit (%s) on langs %s/%s", exc.status_code, username, repo_name)
                        print(f"  ⚠ Rate limit ({exc.status_code})")
                        if self._handle_rate_limit(exc) == "shutdown":
                            return
                        # Rate-limited before completing this repo: leave it
                        # unmarked so it gets re-checked on the next run.
                        break
                    if langs:
                        if self.db.save_repository_languages(repo_id, langs):
                            print(f"      🔤 Languages saved: {', '.join(langs.keys())}")

                    if not self._sleep(SILENT_DELAY_BETWEEN_REQUESTS):
                        return
                else:
                    size_kb = repo.get("size", 0) or 0
                    is_fork = bool(repo.get("fork", False))
                    log.debug(
                        "Silent: %s/%s heavy/fork — no /languages call "
                        "(size=%sKB, fork=%s)",
                        username, repo_name, size_kb, is_fork,
                    )
                    print(
                        f"      ⏭  Skipped langs (size={size_kb}KB, "
                        f"fork={is_fork})"
                    )
                    # NOTE: do NOT call mark_repo_checked below — flipping
                    # READ_HEAVY_FORK_LANGUAGES=True should re-fetch on next
                    # run, so we leave last_checked_at untouched.

                # Topics — always processed (free, comes from /users/.../repos)
                topics = repo.get("topics", [])
                if topics:
                    if self.db.save_repository_topics(repo_id, topics):
                        print(f"      🏷  Topics saved: {', '.join(topics)}")

                # ── Mark repo as freshly checked (per-repo TTL) ──
                # Only after a real /languages round-trip — skipped repos
                # remain "unchecked" so READ_HEAVY_FORK_LANGUAGES toggle
                # backfills them on the next run.
                if _should_lang:
                    self.db.mark_repo_checked(repo_id)

                if not self._sleep(SILENT_DELAY_BETWEEN_REPOS):
                    return

            # ── Mark repos as fetched (user-level TTL pre-filter) ──
            self.db.mark_repos_fetched(username)

            break  # user done

    # ------------------------------------------------------------------
    # Scoring (one user at a time)
    # ------------------------------------------------------------------

    def _score_user(self, username, owner_langs=None, owner_topics=None):
        """Score a single user with optional owner similarity data.

        Uses cached profile info when available to avoid API calls.
        Returns the score (int) on success, or None on failure.
        """
        # ── Try cached info first ──
        info = self.db.get_user_cached_info(username)

        if info and info.get("public_repos") is not None and info.get("followers") is not None:
            log.debug("Silent: using cached info for %s", username)
        else:
            # Cache miss — fetch from API
            try:
                info = self.github.user(username)
            except GitHubAuthError as exc:
                # Non-recoverable — abort already set the shutdown event,
                # the outer run() loop will exit cleanly on the next tick.
                self._abort_on_auth_error(exc)
                return None
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) fetching user %s", exc.status_code, username)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return None
                info = self.github.user(username)

            if not info:
                log.warning("No profile data for %s", username)
                print(f"    ⚠ No profile data for {username}")
                return None

        # ── Parse @-mentions from company field ──
        company_text = info.get("company")
        if company_text:
            self.db.extract_and_save_companies(username, company_text)

        lang_list = self.db.user_languages(username)
        topic_list = self.db.user_topics(username)
        repo_days = self.db.user_repo_recency(username)
        print(f"    📊 Calculating score for {username} ...")
        score = Scorer.calculate(
            info, lang_list, topic_list,
            owner_langs=owner_langs,
            owner_topics=owner_topics,
            repo_days=repo_days,
        )
        self.db.update_score(username, score, info)
        print(f"    ✅ Score: {score}")
        return score

    # ------------------------------------------------------------------
    # Auto-follow high-score users
    # ------------------------------------------------------------------

    def _follow_user(self, username, score):
        """Follow *username* if daily budget allows.  Returns True on success."""
        done_today = self.db.today_follows()
        remaining = DAILY_FOLLOW_LIMIT - done_today
        if remaining <= 0:
            log.info("Daily follow limit reached (%d/%d).", done_today, DAILY_FOLLOW_LIMIT)
            print(f"    ⛔ Daily follow limit reached ({done_today}/{DAILY_FOLLOW_LIMIT})")
            return False

        if self.github.already_following(username):
            log.debug("Already following %s — skipped", username)
            print(f"    👤 Already following {username}")
            return False

        if self.github.follow(username):
            self.db.mark_followed(username)
            log.info("Followed %s (score %d)", username, score)
            print(f"    🤝 Followed {username} (score {score}) [{done_today + 1}/{DAILY_FOLLOW_LIMIT}]")
            return True
        else:
            log.warning("Failed to follow %s", username)
            print(f"    ❌ Failed to follow {username}")
            return False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self):
        """Collect repos + score per user — all with stealth delays.

        Also periodically checks for new owner followers and processes
        them with priority.
        """
        log.info("Silent mode started.")

        # Load owner data once for the similarity comparison
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

        rows = self.db.users_for_silent_processing()

        total = len(rows)
        print(f"Processing {total} users (silent) ...")
        log.info("Silent processing: %d users", total)

        # ── Periodic owner follower scanner ──
        periodic_collector = Collector(self.db, self.github, self._shutdown)
        owner_scan_counter = 0

        for idx, (username,) in enumerate(rows, 1):
            if self._shutdown.is_set():
                print("\nShutdown requested.")
                break

            # ═══════════════════════════════════════════════════════════
            # PERIODIC SCAN: check for new owner followers every N users
            # ═══════════════════════════════════════════════════════════
            if owner_scan_counter >= OWNER_FOLLOWER_SCAN_INTERVAL:
                owner_scan_counter = 0
                new_followers = periodic_collector.scan_owner_followers(verbose=False)
                if new_followers:
                    log.info(
                        "Silent: %d new owner follower(s) discovered — processing immediately",
                        len(new_followers),
                    )
                    print(f"  ★ {len(new_followers)} new owner follower(s) — processing now")
                    for pu_idx, pu in enumerate(new_followers, 1):
                        if self._shutdown.is_set():
                            break
                        # Process new owner follower with priority display
                        self._collect_user_repos(pu, pu_idx, len(new_followers))
                        if not self._shutdown.is_set():
                            pu_score = self._score_user(pu, owner_langs, owner_topics)
                            if (
                                pu_score is not None
                                and pu_score > SILENT_FOLLOW_SCORE_THRESHOLD
                                and not self._shutdown.is_set()
                            ):
                                self._follow_user(pu, pu_score)
                                if not self._sleep(SILENT_DELAY_BETWEEN_FOLLOWS):
                                    break
            owner_scan_counter += 1

            # --- Collect repos for this user (with TTL skip) ---
            self._collect_user_repos(username, idx, total)

            # --- Immediately score this user ---
            if not self._shutdown.is_set():
                score = self._score_user(username, owner_langs, owner_topics)

                # --- Auto-follow if score is high enough ---
                if (
                    score is not None
                    and score > SILENT_FOLLOW_SCORE_THRESHOLD
                    and not self._shutdown.is_set()
                ):
                    self._follow_user(username, score)
                    if not self._sleep(SILENT_DELAY_BETWEEN_FOLLOWS):
                        break

            if not self._sleep(SILENT_DELAY_BETWEEN_USERS):
                break

        if self._shutdown.is_set():
            log.info("Silent mode interrupted by shutdown.")
        else:
            log.info("Silent mode complete.")
            print("\nDone.")
