"""Silent-mode runner — collect repos + score users with human-like delays.

Designed to look like individual user browsing, not a mass crawl.
All delays are configurable in config.py (SILENT_* settings).
"""

import random
import threading
import time
from datetime import datetime, timezone

from config import (
    DAILY_FOLLOW_LIMIT,
    ML_ENABLED,
    OWNER_FOLLOWER_SCAN_INTERVAL,
    READ_HEAVY_FORK_LANGUAGES,
    SILENT_DELAY_BETWEEN_USERS,
    SILENT_DELAY_BETWEEN_REPOS,
    SILENT_DELAY_BETWEEN_REQUESTS,
    SILENT_DELAY_BETWEEN_SCORES,
    SILENT_DELAY_BETWEEN_FOLLOWS,
    SILENT_FOLLOW_SCORE_THRESHOLD,
    SILENT_PRIORITIZE_SMALL,
    SILENT_REPO_CHECK_FRESHNESS_DAYS,
    SKIP_FORK_LANGUAGES,
    SKIP_LANGS_FORK_SIZE_KB,
    SKIP_LANGS_MAX_SIZE_KB,
)
from collector import Collector
from github_client import (
    GitHubAuthError,
    GitHubNetworkError,
    GitHubNotModified,
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
            except GitHubNetworkError as exc:
                if self._handle_network_error(exc) == "shutdown":
                    return
                continue
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

            # ── Store full profile for ML ──
            self.db.store_full_profile(username, info)

            # ── Fetch repos (ETag conditional — 304 = unchanged, free) ──
            repos_etag = self.db.get_repos_etag(username)
            try:
                repos = self.github.repos(username, etag=repos_etag)
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return
            except GitHubNetworkError as exc:
                if self._handle_network_error(exc) == "shutdown":
                    return
                continue
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on repos for %s", exc.status_code, username)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return
                continue
            except GitHubNotModified:
                # Repo list unchanged since last fetch — nothing new to
                # collect.  Backfill any repos whose languages were missed
                # in a previous aborted run, then refresh the user-level
                # TTL so the user leaves the queue.
                log.debug("[%d/%d] Repos for %s unchanged (304) — skipping", idx, total, username)
                print(f"[{idx}/{total}] {username} (repos unchanged — skipped)")
                self._backfill_missing_languages(username)
                self.db.mark_repos_fetched(username)
                return

            # Persist the collection ETag for the next (free) 304.
            if self.github.last_etag:
                self.db.set_repos_etag(username, self.github.last_etag)

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
                #    READ_HEAVY_FORK_LANGUAGES in config.py.  When
                #    SKIP_FORK_LANGUAGES is True (default) /languages is
                #    skipped for ALL forks (API_using.md §7.2).
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
                        langs = self.github.repo_languages(username, repo_name, etag=lang_etag)
                    except GitHubAuthError as exc:
                        self._abort_on_auth_error(exc)
                        return
                    except GitHubNetworkError as exc:
                        if self._handle_network_error(exc) == "shutdown":
                            return
                        break
                    except GitHubRateLimitError as exc:
                        log.warning("Rate limit (%s) on langs %s/%s", exc.status_code, username, repo_name)
                        print(f"  ⚠ Rate limit ({exc.status_code})")
                        if self._handle_rate_limit(exc) == "shutdown":
                            return
                        # Rate-limited before completing this repo: leave it
                        # unmarked so it gets re-checked on the next run.
                        break
                    except GitHubNotModified:
                        # Languages unchanged — cached copy is still valid.
                        log.debug("Silent: %s/%s languages unchanged (304) — cached", username, repo_name)
                        print(f"      ⏭  Languages unchanged (304)")
                        langs = None
                    else:
                        # Persist the fresh ETag so the next run is a free 304.
                        if self.github.last_etag:
                            self.db.set_languages_etag(repo_id, self.github.last_etag)

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
                if self._handle_network_error(exc) == "shutdown":
                    return
                break
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) on langs %s/%s", exc.status_code, username, repo_name)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return
                break
            except GitHubNotModified:
                langs = None
            else:
                if self.github.last_etag:
                    self.db.set_languages_etag(repo_id, self.github.last_etag)
            if langs:
                if self.db.save_repository_languages(repo_id, langs):
                    print(f"      🔤 Languages saved: {', '.join(langs.keys())}")
            # Repo verified now (real fetch or free 304) — start per-repo TTL.
            # Note: size is not stored in the DB, so this backfill may also
            # fetch languages for heavy non-fork repos (>500 MB) that the
            # size filter would normally skip — rare, and it marks them
            # checked so it runs at most once per freshness window.
            self.db.mark_repo_checked(repo_id)
            # Keep the same stealth cadence as the normal per-repo loop.
            if not self._sleep(SILENT_DELAY_BETWEEN_REQUESTS):
                return

    # ------------------------------------------------------------------
    # Scoring (one user at a time)
    # ------------------------------------------------------------------

    def _score_user(self, username, owner_langs=None, owner_topics=None,
                    ml_model=None, ml_meta=None):
        """Score a single user with optional owner similarity data.

        Uses cached profile info when available to avoid API calls.
        Stores the full profile JSON only when fetched from the API.
        Runs ML inference when *ml_model* and *ml_meta* are provided.

        Returns the score (int) on success, or None on failure.
        """
        fetched_from_api = False

        # ── Try cached info first ──
        info = self.db.get_user_cached_info(username)

        if info and info.get("public_repos") is not None and info.get("followers") is not None:
            log.debug("Silent: using cached info for %s", username)
        else:
            # Cache miss — fetch from API
            try:
                info = self.github.user(username)
                fetched_from_api = True
            except GitHubAuthError as exc:
                self._abort_on_auth_error(exc)
                return None
            except GitHubNetworkError as exc:
                log.warning("Network error (%s) fetching user %s for scoring", exc, username)
                print(f"  🌐 Network error fetching {username}")
                if self._handle_network_error(exc) == "shutdown":
                    return None
                try:
                    info = self.github.user(username)
                    fetched_from_api = True
                except Exception:
                    log.warning("Still failing to fetch %s after network retries", username)
                    print(f"    ⚠ Cannot fetch profile for {username} — skipping score")
                    return None
            except GitHubRateLimitError as exc:
                log.warning("Rate limit (%s) fetching user %s", exc.status_code, username)
                print(f"  ⚠ Rate limit ({exc.status_code})")
                if self._handle_rate_limit(exc) == "shutdown":
                    return None
                info = self.github.user(username)
                fetched_from_api = True

            if not info:
                log.warning("No profile data for %s", username)
                print(f"    ⚠ No profile data for {username}")
                return None

        # ── Store full profile for ML (only when freshly fetched) ──
        if fetched_from_api:
            self.db.store_full_profile(username, info)

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

        # ── ML inference (model loaded once in run()) ──
        if ml_model is not None and ml_meta is not None:
            try:
                from ml_service.inference import predict_single
                ml_pred = predict_single(ml_model, ml_meta, self.db, username)
                if ml_pred is not None:
                    label = "👍" if ml_pred == 1 else "👎"
                    print(f"    🤖 ML: {label} (followback {'likely' if ml_pred == 1 else 'unlikely'})")
                    log.debug("ML prediction for %s: %d", username, ml_pred)
            except Exception:
                log.exception("ML inference error for %s", username)

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

        try:
            if self.github.already_following(username):
                # Mark as FOLLOWED so they don't stay in the queue forever.
                now = datetime.now(timezone.utc).isoformat()
                self.db.conn.execute(
                    """UPDATE users
                       SET status = 'FOLLOWED', followed_at = ?
                       WHERE username = ?""",
                    (now, username),
                )
                self.db.conn.commit()
                log.debug("Already following %s — marked FOLLOWED", username)
                print(f"    👤 Already following {username}")
                return False
        except GitHubNetworkError as exc:
            log.warning("Network error checking follow for %s: %s — skipping", username, exc)
            print(f"    🌐 Network error checking {username} — skipping follow")
            return False

        try:
            if self.github.follow(username):
                self.db.mark_followed(username)
                log.info("Followed %s (score %d)", username, score)
                print(f"    🤝 Followed {username} (score {score}) [{done_today + 1}/{DAILY_FOLLOW_LIMIT}]")
                return True
            else:
                log.warning("Failed to follow %s", username)
                print(f"    ❌ Failed to follow {username}")
                return False
        except GitHubNetworkError as exc:
            log.warning("Network error following %s: %s — skipping", username, exc)
            print(f"    🌐 Network error following {username} — skipping")
            return False

    # ------------------------------------------------------------------
    # Follow queue drain (FIFO)
    # ------------------------------------------------------------------

    def _drain_follow_queue(self):
        """Follow queued users (FIFO) while daily limit allows.

        Picks the earliest ``NEW`` user whose score meets the threshold
        and attempts to follow them.  Loops until the queue is empty or
        the daily limit is exhausted.

        Note: ``_follow_user`` now marks already-followed users as
        ``FOLLOWED`` internally, so they won't be re-queued.
        """
        while not self._shutdown.is_set():
            row = self.db.get_next_queued_user(SILENT_FOLLOW_SCORE_THRESHOLD)
            if not row:
                return  # queue empty

            username, score = row
            if self._follow_user(username, score):
                if not self._sleep(SILENT_DELAY_BETWEEN_FOLLOWS):
                    return
            else:
                # Limit exhausted, error, or already-following (handled
                # inside _follow_user).  Stop draining regardless.
                return

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

        rows = self.db.users_for_silent_processing(
            prioritize_small=SILENT_PRIORITIZE_SMALL,
        )

        total = len(rows)
        print(f"Processing {total} users (silent) ...")
        log.info("Silent processing: %d users", total)

        # ── Load ML model ONCE before the main loop ──
        ml_model = None
        ml_meta = None
        if ML_ENABLED:
            try:
                from ml_service.inference import load_model
                ml_model, ml_meta = load_model()
                if ml_model is not None:
                    log.info("ML model loaded for silent inference.")
                else:
                    log.debug("No ML model available — inference disabled.")
            except Exception:
                log.exception("Failed to load ML model")

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
                            pu_score = self._score_user(
                                pu, owner_langs, owner_topics,
                                ml_model=ml_model, ml_meta=ml_meta,
                            )
                            if (
                                pu_score is not None
                                and pu_score > SILENT_FOLLOW_SCORE_THRESHOLD
                                and not self._shutdown.is_set()
                            ):
                                self._follow_user(pu, pu_score)
                                if not self._sleep(SILENT_DELAY_BETWEEN_FOLLOWS):
                                    break
                            # Always drain queue after each owner follower
                            # (harmless if limit exhausted — _follow_user checks)
                            self._drain_follow_queue()
            owner_scan_counter += 1

            # --- Collect repos for this user (with TTL skip) ---
            self._collect_user_repos(username, idx, total)

            # --- Immediately score this user ---
            if not self._shutdown.is_set():
                score = self._score_user(
                    username, owner_langs, owner_topics,
                    ml_model=ml_model, ml_meta=ml_meta,
                )

                # --- Auto-follow if score is high enough ---
                if (
                    score is not None
                    and score > SILENT_FOLLOW_SCORE_THRESHOLD
                    and not self._shutdown.is_set()
                ):
                    self._follow_user(username, score)
                    if not self._sleep(SILENT_DELAY_BETWEEN_FOLLOWS):
                        break
                # Drain queue before moving to next user (FIFO)
                # (harmless if limit exhausted — _follow_user checks internally)
                self._drain_follow_queue()

            if not self._sleep(SILENT_DELAY_BETWEEN_USERS):
                break

        # ── Final drain: follow any remaining queued users ──
        if not self._shutdown.is_set():
            self._drain_follow_queue()

        if self._shutdown.is_set():
            log.info("Silent mode interrupted by shutdown.")
        else:
            log.info("Silent mode complete.")
            print("\nDone.")
