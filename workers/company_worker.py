"""Background worker for GitHub company/org discovery and enrichment.

Runs as a daemon thread alongside the main application.  Every 5 minutes
it picks one company from the queue and fetches (or refreshes) its data
via the GitHub API.

The queue is implicit: companies with api_fetched_at = NULL have the
highest priority, followed by those whose github_updated_at is newer
than our local updated_at.

Usage in main.py::

    from workers.company_worker import CompanyWorker
    worker = CompanyWorker(shutdown_event)
    worker.start()
"""

import threading

from core.logger import get_logger
from workers.runtime import (
    is_enabled,
    mark_action,
    mark_error,
    mark_started,
    mark_stopped,
    sleep_interruptible,
    wait_until_enabled,
)

log = get_logger(__name__)

# Key of this worker in the worker_status table (dashboard toggle/status).
WORKER_KEY = "company"

# Re-export for backward compatibility
from core.database import extract_companies  # noqa: F401

COMPANY_FETCH_INTERVAL = 300  # 5 minutes between fetches


# ── Background thread ───────────────────────────────────────────────────

class CompanyWorker(threading.Thread):
    """Daemon thread that enriches company data via GitHub API.

    Creates its own Database and GithubClient instances to avoid
    SQLite thread-safety issues.

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    fetch_interval : int, optional
        Seconds between fetch cycles.  Default 300 (5 min).
    """

    def __init__(self, shutdown_event, fetch_interval=COMPANY_FETCH_INTERVAL):
        super().__init__(daemon=True, name="CompanyWorker")
        self._shutdown = shutdown_event
        self._interval = fetch_interval

    def run(self):
        # Import inside thread to avoid circular imports at module level
        from core.database import Database
        from core.github_client import GithubClient

        db = Database()
        github = GithubClient()

        log.info("Company worker started (interval=%ds)", self._interval)
        mark_started(db, WORKER_KEY)

        try:
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("Company worker paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue
                try:
                    self._process_one(db, github)
                except Exception as exc:
                    mark_error(db, WORKER_KEY, f"{type(exc).__name__}: {exc}")
                    log.exception("Company worker error")

                if not sleep_interruptible(
                    self._shutdown, self._interval, db=db, key=WORKER_KEY,
                ):
                    break
        finally:
            mark_stopped(db, WORKER_KEY)
            db.conn.close()
            log.info("Company worker stopped.")

    def _process_one(self, db, github):
        """Fetch data for one company from the queue."""
        company = db.get_next_company_to_fetch()
        if not company:
            log.debug("Company queue empty — nothing to do.")
            return

        comp_id, login, local_updated_at = company

        log.info("Fetching company data: %s", login)
        api_data = github.user(login)

        if api_data is None:
            # 404 or network error — mark as fetched to avoid infinite retries
            db.mark_company_fetched(comp_id)
            mark_action(db, WORKER_KEY)
            log.warning("Company %s returned no data — skipping", login)
            return

        db.update_company_data(comp_id, api_data)
        mark_action(db, WORKER_KEY)
        log.info("Company %s updated.", login)
