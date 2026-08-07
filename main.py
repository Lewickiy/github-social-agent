"""GitHub Social Agent — CLI entry point.

Usage (single working mode):
    python main.py --silent            MAIN mode: collect + score (follows handled
                                       by a dedicated FollowWorker thread).
                                       Self-sustaining — waits for the first
                                       followers / owner data, and grows the
                                       follower graph via a calm background
                                       worker (≤500 req/h).
    python main.py --migrate           Apply pending database migrations
    python main.py --migrate-status    Show migration status
    python main.py --top               Show top 50 unscored users
    python main.py --profile USER      Show developer profile for USER
    python main.py --snapshot          Record today's profile snapshot now

Force / backfill modes (usually not needed — --silent covers all of them):
    python main.py --collect-self      Collect owner's repos/languages/topics
    python main.py --collect           Discover users & fetch repos (both phases)
    python main.py --collect-users     Phase 1: discover new users from follower graph
    python main.py --collect-users-rep Phase 2: fetch repos/languages for all users
    python main.py --score             Score / re-score users
"""

import os
import signal
import sys
import threading

from core.config import MY_USERNAME, SILENT_WORKERS
from core.database import Database
from core.github_client import GithubClient
from core.logger import get_logger
from services.collector import Collector
from services.scorer import Scorer
from services.silent import SilentRunner
from workers.company_worker import CompanyWorker
from workers.follow_worker import FollowWorker
from workers.followback_check_worker import FollowbackCheckWorker
from workers.graph_discovery_worker import GraphDiscoveryWorker
from workers.ml_trainer import MLTrainerWorker
from workers.snapshot_worker import SnapshotWorker, take_snapshot_now

log = get_logger(__name__)

# Global event shared between signal handlers and the collector.
_shutdown = threading.Event()


def _handle_sigint(signum, frame):
    """First Ctrl+C → graceful shutdown; second → force exit."""
    if _shutdown.is_set():
        log.warning("Force exit (second Ctrl+C).")
        print("\n\nForce exit.")
        os._exit(1)
    log.info("SIGINT received — initiating graceful shutdown.")
    print("\n\nGraceful shutdown — finishing current user ...")
    _shutdown.set()


def _handle_sigterm(signum, frame):
    """SIGTERM → same graceful shutdown as first Ctrl+C."""
    log.info("SIGTERM received — initiating graceful shutdown.")
    print("\n\nGraceful shutdown — finishing current user ...")
    _shutdown.set()


def _setup_signals():
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigterm)


def _print_profile(profile):
    """Pretty-print a developer profile."""
    langs = profile["languages"]
    topics = profile.get("topics", [])

    print()
    print(f"Username:       {profile['username']}")
    print()
    print(f"Repositories:   {profile['repo_count']}")
    print()
    if langs:
        print("Languages:")
        for name, pct in langs:
            bar = "#" * int(pct / 5)
            print(f"  {name:<12} {pct:>5.1f}%  {bar}")
        print()
    print(f"Followers:      {profile['followers']}")
    if profile.get("bio"):
        print(f"Bio:            {profile['bio']}")
    if profile.get("company"):
        print(f"Company:        {profile['company']}")
    if topics:
        print(f"Topics:         {' '.join(topics)}")
    companies = profile.get("companies", [])
    if companies:
        print(f"Companies:")
        for login, name, ctype in companies:
            label = name or login
            print(f"  @{login:<20} {label} ({ctype or '?'})")
    print()
    print(f"Score:          {profile['score']}")
    print()


def _report_job_outcome(job_id, error=None):
    """Persist this process's real job outcome if it was dashboard-spawned.

    The API normally records the outcome via a watcher thread, but if the
    API restarted mid-job the watcher is gone — the bot process itself is
    the only one that knows whether it actually completed its work.
    Uses a fresh Database connection (the main one is being torn down).
    """
    if not job_id:
        return
    try:
        db = Database()
        try:
            job_id = int(job_id)
            current = db.get_job(job_id)
            if not current or current["status"] == "SUCCESS":
                return
            # This runs in the bot's finally, BEFORE the process exits, so
            # the watcher (proc.wait() → finish_job) can never have written
            # yet.  A FAILED row at this point can only be a cleanup
            # force-mark from an API restart — the bot's own verdict is the
            # source of truth, so overwrite it (a long-running job that
            # eventually completed must show SUCCESS).
            # exit_code 0 → SUCCESS, -1 → FAILED (with the real error).
            db.finish_job(job_id, -1 if error else 0, error=error)
        finally:
            db.conn.close()
    except Exception:
        log.exception("Failed to self-report job %s outcome", job_id)


def _run_silent(db, github, shutdown_event):
    """Run silent mode, optionally with several parallel workers.

    When ``SILENT_WORKERS`` > 1, each worker gets its own Database and
    GithubClient (SQLite connections are not thread-safe) and processes
    a disjoint slice of the user queue, so the total GitHub request rate
    scales with the number of workers while per-user pacing is unchanged.
    """
    n = SILENT_WORKERS
    if n <= 1:
        SilentRunner(db, github, shutdown_event=shutdown_event).run()
        return

    log.info("Silent mode: starting %d parallel workers", n)

    def _worker(i):
        wdb = Database()
        try:
            wgh = GithubClient()
            SilentRunner(wdb, wgh, shutdown_event=shutdown_event).run(
                worker_index=i, worker_count=n,
            )
        except Exception:
            log.exception("Silent worker %d failed unexpectedly", i)
        finally:
            wdb.conn.close()

    threads = [
        threading.Thread(target=_worker, args=(i,), daemon=True,
                         name=f"SilentWorker-{i}")
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def main():
    _setup_signals()

    # Job launched from the dashboard — the API passes its job_run id so
    # the bot can record the true outcome on exit (survives API restarts).
    job_id = os.environ.get("GITHUB_SOCIAL_JOB_ID")

    # Ensure migrations have been applied before any DB operation
    if "--migrate" not in sys.argv and "--migrate-status" not in sys.argv and "--help" not in sys.argv:
        try:
            db = Database()
            db.conn.execute("SELECT 1 FROM users LIMIT 1")
        except Exception:
            msg = "Database not initialised. Run:  python main.py --migrate"
            log.error(msg)
            print(f"Error: {msg}")
            sys.exit(1)
    else:
        db = Database()

    github = GithubClient()

    # ── Sync owner profile on startup (TTL-based) ───────────────
    # Skipped for modes that don't interact with the GitHub API.
    skip_sync = {"--migrate", "--migrate-status", "--help", "--collect-self", "--top", "--profile"}
    if not skip_sync & set(sys.argv):
        log.info("Startup: syncing owner profile for %s", MY_USERNAME)
        collector = Collector(db, github, shutdown_event=_shutdown)
        collector.sync_owner()                  # repos — only if stale (> 14 days)
        collector.scan_owner_followers()        # followers — always (FOLLOWBACK detection)

    # ── Start background workers for long-running modes ──
    long_running = {"--silent", "--collect", "--collect-users", "--collect-users-rep", "--score"}
    company_worker = None
    followback_worker = None
    follow_worker = None
    ml_worker = None
    snapshot_worker = None
    if long_running & set(sys.argv):
        # All background workers start unconditionally — whether each one
        # actually does work is controlled at runtime by the Management-tab
        # toggle (worker_status.enabled, all active by default; see
        # migration 027 and workers/runtime.py).  A paused worker simply
        # waits, so it can be resumed from the UI without a restart.
        company_worker = CompanyWorker(_shutdown)
        company_worker.start()
        followback_worker = FollowbackCheckWorker(_shutdown)
        followback_worker.start()
        # The ONLY follow executor in the system — silent and the other
        # workers never subscribe; this thread drains the follow queue at
        # its own human-like pace, independent of the analysis pipeline.
        # NOTE: assumes a single bot process — a dashboard-launched
        # `silent` job alongside the docker bot would start a second
        # FollowWorker (the daily budget is shared via the actions table).
        follow_worker = FollowWorker(_shutdown)
        follow_worker.start()
        ml_worker = MLTrainerWorker(_shutdown)
        ml_worker.start()
        snapshot_worker = SnapshotWorker(_shutdown)
        snapshot_worker.start()

    # ── Graph growth for the main working mode ──
    # A separate calm, rate-limited background worker walks the follower
    # graph (≤ DISCOVERY_RATE_LIMIT_PER_HOUR requests/hour) and adds new
    # users to the queue — silent itself only collects + scores (follows
    # are handled by the FollowWorker thread above).
    discovery_worker = None
    if "--silent" in sys.argv:
        # Started for the main working mode; paused/resumed via the
        # Management-tab toggle (worker_status.enabled, default active).
        discovery_worker = GraphDiscoveryWorker(_shutdown)
        discovery_worker.start()

    job_error = None
    try:
        if "--collect-self" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).collect_self()

        elif "--silent" in sys.argv:
            _run_silent(db, github, _shutdown)

        elif "--collect-users-rep" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).collect_repos()

        elif "--collect-users" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).collect_users()

        elif "--collect" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).run()

        elif "--score" in sys.argv:
            Scorer(db, github).run()

        elif "--top" in sys.argv:
            for user, score in db.top_users(50):
                print(user, score)

        elif "--profile" in sys.argv:
            idx = sys.argv.index("--profile")
            if idx + 1 >= len(sys.argv):
                print("Usage: python main.py --profile USERNAME")
                sys.exit(1)
            username = sys.argv[idx + 1]
            profile = db.developer_profile(username)
            if profile is None:
                print(f"User '{username}' not found.")
                sys.exit(1)
            _print_profile(profile)

        elif "--snapshot" in sys.argv:
            print("Recording today's profile snapshot ...")
            take_snapshot_now(db, github)
            print("Snapshot cycle finished.")

        elif "--migrate" in sys.argv:
            from migrations.runner import migrate
            migrate()

        elif "--migrate-status" in sys.argv:
            from migrations.runner import status
            status()

        else:
            print(
                """\
Usage (single working mode):
    python main.py --silent            MAIN mode: collect + score (follows handled
                                       by a dedicated FollowWorker thread).
                                       Self-sustaining — waits for the first
                                       followers / owner data, and grows the
                                       follower graph via a calm background
                                       worker (≤500 req/h).
    python main.py --migrate           Apply pending database migrations
    python main.py --migrate-status    Show migration status
    python main.py --top               Show top 50 unscored users
    python main.py --profile USER      Show developer profile for USER
    python main.py --snapshot          Record today's profile snapshot now

Force / backfill modes (usually not needed — --silent covers all of them):
    python main.py --collect-self      Collect owner's repos/languages/topics
    python main.py --collect           Discover users & fetch repos (both phases)
    python main.py --collect-users     Phase 1: discover new users from follower graph
    python main.py --collect-users-rep Phase 2: fetch repos/languages for all users
    python main.py --score             Score / re-score users\
"""
            )
    except Exception as exc:
        job_error = f"{type(exc).__name__}: {exc}"
        log.critical("Unhandled exception — %s: %s", type(exc).__name__, exc, exc_info=True)
        print(f"\n❌ Unexpected error: {exc}")
        print("   Shutting down gracefully...")
    finally:
        log.info("Shutting down — committing and closing database.")
        db.conn.commit()
        db.conn.close()
        _report_job_outcome(job_id, error=job_error)


if __name__ == "__main__":
    main()
