"""Background worker — calm, rate-limited follower-graph discovery.

Grows the network by walking the follower graph (the logic of the old
``--collect-users`` / ``Collector._discover``) at a conservative, constant
rate instead of in bursts.  Runs as a daemon thread alongside silent mode,
so silent's workers are free to collect + score + follow while this thread
quietly adds new users to the queue.

Rate control: each pass walks at most ``DISCOVERY_PASS_MAX_USERS`` users
(~1 profile request each, plus one followers request *per page* when
their count grew), paced to at most ``DISCOVERY_RATE_LIMIT_PER_HOUR``
requests/hour.  The pacing is per user, so a pass can legitimately span
several hours — the *sustained* request rate, not the pass total, is
what stays within the hourly budget.  After a pass the worker sleeps out
the rest of its hour window before starting the next one.

Each pass ALSO runs the repo-content discovery pass
(``Collector._discover_repo_fans``, issue #22): the owner's own
repositories are mined for stargazers (+ optionally contributors), with
the same pacing, budget and ``discovery_runs`` reporting as the graph
walk.  1 request ≈ up to 100 candidates — far more throughput than graph
walking for the same budget.

Usage in main.py::

    from workers.graph_discovery_worker import GraphDiscoveryWorker
    worker = GraphDiscoveryWorker(shutdown_event)
    worker.start()
"""

import threading
import time
from datetime import datetime, timezone

from core.config import (
    DISCOVERY_PASS_MAX_USERS,
    DISCOVERY_RATE_LIMIT_PER_HOUR,
    DISCOVERY_REPO_FANS_ENABLED,
    DISCOVERY_REPO_FANS_MAX_SEEDS,
)
from core.logger import get_logger
from workers.runtime import (
    is_enabled,
    mark_action,
    mark_error,
    mark_started,
    mark_stopped,
    sleep_interruptible,
    touch_heartbeat,
    wait_until_enabled,
)

log = get_logger(__name__)

# Key of this worker in the worker_status table (dashboard toggle/status).
WORKER_KEY = "graph_discovery"

# One hourly budget window in seconds.
_HOUR_SECONDS = 3600


class _RequestCounter:
    """Wraps a GithubClient and counts every real API request it makes.

    The shared ``github_api_requests`` table also contains silent-mode
    traffic, so it can't isolate this worker's usage — the counter is the
    only accurate per-pass budget meter.  ``followers()`` paginates, so
    each page is counted (the per-page ``pacer`` is the hook).
    """

    def __init__(self, github):
        self._gh = github
        self.requests = 0

    def user(self, *args, **kwargs):
        self.requests += 1
        return self._gh.user(*args, **kwargs)

    def followers(self, *args, **kwargs):
        pacer = kwargs.get("pacer")

        def _pacer():
            self.requests += 1  # every pagination page is a request
            if pacer is not None:
                pacer()

        kwargs["pacer"] = _pacer
        return self._gh.followers(*args, **kwargs)

    def stargazers(self, *args, **kwargs):
        pacer = kwargs.get("pacer")

        def _pacer():
            self.requests += 1
            if pacer is not None:
                pacer()

        kwargs["pacer"] = _pacer
        return self._gh.stargazers(*args, **kwargs)

    def contributors(self, *args, **kwargs):
        pacer = kwargs.get("pacer")

        def _pacer():
            self.requests += 1
            if pacer is not None:
                pacer()

        kwargs["pacer"] = _pacer
        return self._gh.contributors(*args, **kwargs)


def discovery_request_delay(rate_per_hour):
    """Inter-request delay (seconds) that keeps requests ≤ *rate_per_hour*.

    Returns 1 s for a non-positive rate (no effective limit — the fallback
    pacing used by the legacy full walk).
    """
    if rate_per_hour <= 0:
        return 1.0
    return _HOUR_SECONDS / rate_per_hour


class GraphDiscoveryWorker(threading.Thread):
    """Daemon thread that incrementally walks the follower graph.

    Creates its own Database and GithubClient instances to avoid SQLite
    thread-safety issues.

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    rate_per_hour : int, optional
        Request budget per hour.  Default from config
        ``DISCOVERY_RATE_LIMIT_PER_HOUR`` (500).
    pass_max_users : int, optional
        Users walked per pass.  Default from config
        ``DISCOVERY_PASS_MAX_USERS`` (500 — one hour's budget).
    """

    def __init__(
        self,
        shutdown_event,
        rate_per_hour=DISCOVERY_RATE_LIMIT_PER_HOUR,
        pass_max_users=DISCOVERY_PASS_MAX_USERS,
    ):
        super().__init__(daemon=True, name="GraphDiscoveryWorker")
        self._shutdown = shutdown_event
        self._rate_per_hour = rate_per_hour
        self._pass_max_users = pass_max_users

    def run(self):
        # Import inside the thread to avoid circular imports at module level.
        from core.database import Database
        from core.github_client import GithubClient

        db = Database()
        github = GithubClient()

        delay = discovery_request_delay(self._rate_per_hour)
        log.info(
            "Graph discovery worker started (≤%d req/h, %d users/pass, "
            "%.1fs pacing).",
            self._rate_per_hour, self._pass_max_users, delay,
        )
        mark_started(db, WORKER_KEY)

        try:
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("Graph discovery worker paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue

                # One hourly budget window: a pass sized to the budget, then
                # sleep out the remainder of the hour so the rate never
                # exceeds DISCOVERY_RATE_LIMIT_PER_HOUR even when the pass
                # finished early (e.g. fewer eligible users than the cap).
                start = time.monotonic()
                try:
                    self._run_pass(db, github, delay)
                    mark_action(db, WORKER_KEY)
                except Exception as exc:
                    mark_error(
                        db, WORKER_KEY, f"{type(exc).__name__}: {exc}",
                    )
                    log.exception("Graph discovery worker error")

                remaining = start + _HOUR_SECONDS - time.monotonic()
                if not sleep_interruptible(
                    self._shutdown, max(0.0, remaining),
                    db=db, key=WORKER_KEY,
                ):
                    break
        finally:
            mark_stopped(db, WORKER_KEY)
            db.conn.close()
            log.info("Graph discovery worker stopped.")

    def _heartbeat_loop(self, stop_event):
        """Keep the dashboard's "Running" state honest during a pass.

        ``_run_pass`` spends hours in plain ``time.sleep`` calls inside
        the collector, so without this companion thread the worker_status
        heartbeat would go stale within 5 minutes and the UI would show
        the worker as offline while it is actually working.

        Uses its own Database connection — SQLite connections are not
        thread-safe, and the main worker thread is writing through the
        shared ``db`` for the whole pass.
        """
        from core.database import Database

        hb_db = Database()
        try:
            while not stop_event.is_set():
                touch_heartbeat(hb_db, WORKER_KEY)
                time.sleep(30)
        finally:
            hb_db.conn.close()

    def _run_pass(self, db, github, delay):
        """Walk one bounded slice of the follower graph and record it.

        The pass stats (users walked, new users, real request count,
        duration) are persisted to ``discovery_runs`` so the dashboard
        can show the worker's activity — including actual budget usage
        against ``DISCOVERY_RATE_LIMIT_PER_HOUR``.
        """
        from services.collector import Collector

        # Heartbeat companion: the pass sleeps with plain time.sleep()
        # inside the collector, so a daemon thread keeps touching the
        # worker_status heartbeat (→ dashboard "Running").
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop_heartbeat,),
            daemon=True,
            name="GraphDiscoveryHeartbeat",
        )
        heartbeat.start()

        counter = _RequestCounter(github)
        collector = Collector(db, counter, shutdown_event=self._shutdown)
        started_at = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()
        stats = {"users_walked": 0, "new_users": 0, "seeds_mined": 0}
        try:
            # Follower-graph pass …
            collector._discover(
                max_users=self._pass_max_users,
                sleep_between_users=delay,
                stats=stats,
            )
            # … then the repo-content pass (stargazers/contributors of the
            # owner's own repositories) — same budget, same run record.
            if DISCOVERY_REPO_FANS_ENABLED:
                collector._discover_repo_fans(
                    max_seeds=DISCOVERY_REPO_FANS_MAX_SEEDS,
                    sleep_between_users=delay,
                    stats=stats,
                )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=5)
            duration = time.monotonic() - start
            db.record_discovery_run(
                {
                    "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "users_walked": stats["users_walked"],
                    "new_users": stats["new_users"],
                    "seeds_mined": stats["seeds_mined"],
                    "requests": counter.requests,
                    "duration_seconds": round(duration, 1),
                }
            )
        log.info(
            "Graph discovery pass finished: %d user(s) walked, %d seed(s) "
            "mined, %d new user(s), %d request(s) in %.0fs.",
            stats["users_walked"], stats["seeds_mined"],
            stats["new_users"], counter.requests, duration,
        )
