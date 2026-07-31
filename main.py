"""GitHub Social Agent — CLI entry point.

Usage:
    python main.py --collect-self      Collect owner's repos/languages/topics
    python main.py --silent            Stealth collect repos + score with human-like delays
    python main.py --collect           Discover users & fetch repos (both phases)
    python main.py --collect-users     Phase 1: discover new users from follower graph
    python main.py --collect-users-rep Phase 2: fetch repos/languages for all users
    python main.py --score             Score / re-score users
    python main.py --follow            Follow top-scored users (respects daily limit)
    python main.py --top               Show top 50 unscored users
    python main.py --profile USER      Show developer profile for USER
    python main.py --migrate           Apply pending database migrations
    python main.py --migrate-status    Show migration status
"""

import os
import signal
import sys
import threading

from collector import Collector
from company_worker import CompanyWorker
from config import MY_USERNAME
from database import Database
from follow_engine import FollowEngine
from followback_check_worker import FollowbackCheckWorker
from github_client import GithubClient
from logger import get_logger
from scorer import Scorer
from silent import SilentRunner
from workers.ml_trainer import MLTrainerWorker

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


def main():
    _setup_signals()

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
    long_running = {"--silent", "--collect", "--collect-users", "--collect-users-rep", "--score", "--follow"}
    company_worker = None
    followback_worker = None
    ml_worker = None
    if long_running & set(sys.argv):
        company_worker = CompanyWorker(_shutdown)
        company_worker.start()
        followback_worker = FollowbackCheckWorker(_shutdown)
        followback_worker.start()
        ml_worker = MLTrainerWorker(_shutdown)
        ml_worker.start()

    try:
        if "--collect-self" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).collect_self()

        elif "--silent" in sys.argv:
            SilentRunner(db, github, shutdown_event=_shutdown).run()

        elif "--collect-users-rep" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).collect_repos()

        elif "--collect-users" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).collect_users()

        elif "--collect" in sys.argv:
            Collector(db, github, shutdown_event=_shutdown).run()

        elif "--score" in sys.argv:
            Scorer(db, github).run()

        elif "--follow" in sys.argv:
            FollowEngine(db, github).run()

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

        elif "--migrate" in sys.argv:
            from migrate import migrate
            migrate()

        elif "--migrate-status" in sys.argv:
            from migrate import status
            status()

        else:
            print(
                """\
Usage:
    python main.py --collect-self      Collect owner's repos/languages/topics
    python main.py --silent            Stealth collect repos + score with human-like delays
    python main.py --collect           Discover users & fetch repos (both phases)
    python main.py --collect-users     Phase 1: discover new users from follower graph
    python main.py --collect-users-rep Phase 2: fetch repos/languages for all users
    python main.py --score             Score / re-score users
    python main.py --follow            Follow top-scored users
    python main.py --top               Show top 50 users
    python main.py --profile USER      Show developer profile
    python main.py --migrate           Apply pending migrations
    python main.py --migrate-status    Show migration status\
"""
            )
    except Exception as exc:
        log.critical("Unhandled exception — %s: %s", type(exc).__name__, exc, exc_info=True)
        print(f"\n❌ Unexpected error: {exc}")
        print("   Shutting down gracefully...")
    finally:
        log.info("Shutting down — committing and closing database.")
        db.conn.commit()
        db.conn.close()


if __name__ == "__main__":
    main()
