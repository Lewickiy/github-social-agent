"""Silent-mode runner — the bot's single working mode.

Collects repos + scores users with human-like delays, and is designed to
look like individual user browsing, not a mass crawl.  All delays are
configurable in config.py (SILENT_* settings).

Following is **not** done inline anymore: the dedicated FollowWorker
daemon thread (``workers/follow_worker.py``) drains the follow queue
independently at its own pace, so silent stays a pure collect + score
pipeline.

On top of the legacy one-shot behaviour it now:
  * waits for first-run readiness (``SILENT_GATES_ENABLED``) — at least
    one follower and enough owner repo data — instead of finishing with
    "Processing 0 users";
  * when ``SILENT_CONTINUOUS`` is True (default) it never exits: after
    draining its queue it polls for new owner followers and waits for new
    work instead of stopping.  Follower-graph growth (new users beyond
    the owner's followers) is handled by a separate calm background
    worker (``workers/graph_discovery_worker.py``, ≤ DISCOVERY_*
    requests/hour), so ``--collect*`` / ``--score`` are only needed as
    one-off force/backfill modes.
"""

import threading
import time

from core.config import (
    ML_ENABLED,
    OWNER_FOLLOWER_SCAN_INTERVAL,
    READ_HEAVY_FORK_LANGUAGES,
    SILENT_CONTINUOUS,
    SILENT_DELAY_BETWEEN_USERS,
    SILENT_DELAY_BETWEEN_REPOS,
    SILENT_DELAY_BETWEEN_REQUESTS,
    SILENT_DELAY_BETWEEN_SCORES,
    SILENT_GATE_CHECK_INTERVAL_SECONDS,
    SILENT_GATES_ENABLED,
    SILENT_MIN_OWNER_REPOS,
    SILENT_PRIORITIZE_SMALL,
    SILENT_REPO_CHECK_FRESHNESS_DAYS,
    SKIP_FORK_LANGUAGES,
    SKIP_LANGS_FORK_SIZE_KB,
    SKIP_LANGS_MAX_SIZE_KB,
)
from core.github_client import (
    GitHubAuthError,
    GitHubNetworkError,
    GitHubNotModified,
    GitHubRateLimitError,
    LONG_HINT_THRESHOLD,
    first_wait_from_headers,
    should_fetch_languages,
)
from core.logger import get_logger
from core.stealth import jitter

from .collector import Collector
from .scorer import Scorer

log = get_logger(__name__)


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
        actual = jitter(seconds)
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
                self.db.mark_deleted(username)
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
                #    skipped for ALL forks (documentation/API_using.md §7.2).
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
    # First-run gates & self-sustaining helpers
    # ------------------------------------------------------------------

    def _load_owner_profile(self, owner_username):
        """Return (owner_langs, owner_topics) for similarity scoring."""
        if not owner_username:
            return None, None
        owner_langs = {
            name: pct
            for name, pct in self.db.user_languages(owner_username)
        }
        owner_topics = self.db.user_topics(owner_username)
        log.info("Owner profile loaded for scoring: %s", owner_username)
        return owner_langs, owner_topics

    def _load_ml_model(self):
        """Load the ML model once for the whole run.  Returns (model, meta)."""
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
        return ml_model, ml_meta

    def _queue_size(self):
        """Count users currently awaiting silent processing (DB-only)."""
        return self.db.count_silent_processing_queue()

    def _owner_data_ready(self):
        """True when the owner has enough collected data for meaningful scoring."""
        owner = self.db.get_owner()
        if not owner:
            return False
        return self.db.user_repo_count(owner) >= SILENT_MIN_OWNER_REPOS

    def _blocked_reason(self):
        """Return the first unmet gate as (reason, detail), or (None, None).

        ``reason`` is one of:
          * ``"followers"`` — nothing to process (no users at all, or the
            whole queue is drained);
          * ``"owner_data"`` — the owner lacks enough collected repos for
            the similarity half of the score to mean anything.
        """
        if self._queue_size() == 0:
            return "followers", (
                "No users to process — the network needs at least one "
                "follower.  New owner followers are picked up automatically, "
                "then the follower graph is walked for growth."
            )
        if not self._owner_data_ready():
            return "owner_data", (
                f"Owner has fewer than {SILENT_MIN_OWNER_REPOS} collected "
                f"repo(s) — not enough data for meaningful scoring (scores "
                f"would cap at 35/100).  Set SILENT_GATES_ENABLED=False in "
                f"core/config.py to score anyway."
            )
        return None, None

    def _wait_for_work(self, worker_index):
        """Block until the silent queue has work (or shutdown is requested).

        Used both before the first batch (first-run gates) and between
        batches in continuous mode.  While blocked, worker 0 polls GitHub
        on a fixed cadence:
          * new owner followers are scanned every cycle (they enter the
            queue as soon as they appear);
          * the owner profile is force re-synced only while the owner-data
            gate is the blocker and ``SILENT_GATES_ENABLED``, and only
            once the profile's ``public_repos`` suggests data may now
            exist — a full forced re-sync (profile + all repos + languages)
            every cycle would be wasteful.
        Follower-graph growth is the GraphDiscoveryWorker's job — silent
        never walks the graph itself.  Other workers only re-check the DB
        each cycle.
        """
        if self._shutdown.is_set():
            return

        reason, detail = self._blocked_reason()
        if reason is None:
            return

        if SILENT_GATES_ENABLED:
            print("\n⏸  Waiting for work:")
            print(f"  ⚠ {detail}")
            last_reason = reason
        else:
            print("  ⏳ No users to process — polling for new followers ...")
            last_reason = None

        while not self._shutdown.is_set():
            reason, detail = self._blocked_reason()
            if reason is None:
                if SILENT_GATES_ENABLED:
                    print("  ✅ Ready — resuming.\n")
                return
            if SILENT_GATES_ENABLED and reason != last_reason:
                print(f"  ⚠ {detail}")
                last_reason = reason

            if worker_index == 0:
                collector = Collector(self.db, self.github, self._shutdown)
                if SILENT_GATES_ENABLED and reason == "owner_data":
                    # Cheap check first: a full forced re-sync (profile +
                    # all repos + languages) is expensive, so only run it
                    # once the profile suggests data may now exist.
                    owner = self.db.get_owner()
                    if owner:
                        try:
                            info = self.github.user(owner)
                        except GitHubAuthError as exc:
                            self._abort_on_auth_error(exc)
                            break
                        except (GitHubNetworkError, GitHubRateLimitError):
                            info = None
                        if (
                            info is not None
                            and (info.get("public_repos") or 0)
                            >= SILENT_MIN_OWNER_REPOS
                        ):
                            collector.sync_owner(force=True)
                else:  # queue drained — poll for new owner followers
                    collector.scan_owner_followers(verbose=False)

            if not self._sleep(SILENT_GATE_CHECK_INTERVAL_SECONDS):
                break

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, worker_index=0, worker_count=1):
        """Collect repos + score per user — all with stealth delays.

        Single working mode of the bot.  When ``SILENT_GATES_ENABLED`` the
        run first waits until the account can do meaningful work (at least
        one follower and ``SILENT_MIN_OWNER_REPOS`` owner repos).  When
        ``SILENT_CONTINUOUS`` (default) the run never exits: after draining
        its queue it polls for new owner followers and waits for new work
        instead of stopping.  Follower-graph growth is handled by the
        separate GraphDiscoveryWorker (started from main.py) — so
        --collect* / --score are only needed as one-off force/backfill
        modes.

        Also periodically checks for new owner followers and processes
        them with priority.

        Parameters
        ----------
        worker_index : int, optional
            Zero-based index of this worker.  Used only when multiple
            workers share the queue (SILENT_WORKERS > 1).
        worker_count : int, optional
            Total number of parallel workers.  The queue is split as
            ``rows[worker_index::worker_count]`` — each worker handles a
            disjoint slice, so users are never processed twice.
        """
        log.info("Silent mode started (worker %d/%d).", worker_index + 1, worker_count)

        # ── First-run gates: wait until the account can do meaningful work ──
        if SILENT_GATES_ENABLED:
            self._wait_for_work(worker_index)

        # Load owner data for the similarity comparison (it may have changed
        # while the gates were waiting).
        owner_username = self.db.get_owner()
        owner_langs, owner_topics = self._load_owner_profile(owner_username)

        # ── Load ML model ONCE before the main loop ──
        ml_model, ml_meta = self._load_ml_model()

        # ── Periodic owner follower scanner (worker 0 only — other
        #    workers skip it to avoid duplicate owner scans) ──
        periodic_collector = (
            Collector(self.db, self.github, self._shutdown)
            if worker_index == 0 else None
        )

        while not self._shutdown.is_set():
            rows = self.db.users_for_silent_processing(
                prioritize_small=SILENT_PRIORITIZE_SMALL,
            )
            # Split the queue across parallel workers — disjoint slices.
            if worker_count > 1:
                rows = rows[worker_index::worker_count]

            total = len(rows)
            if total:
                print(f"Processing {total} users (silent) ...")
                log.info("Silent processing: %d users", total)
            else:
                log.debug(
                    "Silent: queue empty%s",
                    " — waiting for new work" if SILENT_CONTINUOUS else "",
                )

            owner_scan_counter = 0
            for idx, (username,) in enumerate(rows, 1):
                if self._shutdown.is_set():
                    print("\nShutdown requested.")
                    break

                # ═══════════════════════════════════════════════════════════
                # PERIODIC SCAN: check for new owner followers every N users
                # ═══════════════════════════════════════════════════════════
                if (
                    periodic_collector is not None
                    and owner_scan_counter >= OWNER_FOLLOWER_SCAN_INTERVAL
                ):
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
                                self._score_user(
                                    pu, owner_langs, owner_topics,
                                    ml_model=ml_model, ml_meta=ml_meta,
                                )
                owner_scan_counter += 1

                # --- Collect repos for this user (with TTL skip) ---
                self._collect_user_repos(username, idx, total)

                # --- Immediately score this user ---
                # (Follows are handled by the dedicated FollowWorker thread —
                #  see workers/follow_worker.py — not inline here.)
                if not self._shutdown.is_set():
                    self._score_user(
                        username, owner_langs, owner_topics,
                        ml_model=ml_model, ml_meta=ml_meta,
                    )

                if not self._sleep(SILENT_DELAY_BETWEEN_USERS):
                    break

            if self._shutdown.is_set():
                break

            if not SILENT_CONTINUOUS:
                break

            if total == 0:
                # Queue drained — wait (gates/polling) for new work instead
                # of exiting.  Graph growth is the GraphDiscoveryWorker's
                # job; silent itself only polls for new owner followers.
                self._wait_for_work(worker_index)

        if self._shutdown.is_set():
            log.info("Silent mode interrupted by shutdown.")
        else:
            log.info("Silent mode complete.")
            print("\nDone.")
