"""Background worker — reacts to interactions with the owner's repositories.

Closes the loop on inbound attention.  Today the system only reacts to
users following us (``scan_owner_followers``), and the interaction check
(``services/interactions.py``) is used exclusively for the unfollow
decision.  Anyone who stars / forks / opens an issue or PR on our
repositories is invisible to the system.

This worker polls the owner's event timeline once per hour
(``GithubClient.user_events`` — ``/users/{owner}/received_events``, the
inbound-attention feed: others' stars/forks/issues/PRs on the owner's
repos, plus follows of the owner), persists every interaction into the
``interactions`` table (deduped on ``event_id`` with the REAL event
timestamp), and:

  * actors not in the DB → ``add_user(actor, \"repo_interaction\")`` — they
    enter the normal pipeline (collect → score → follow);
  * actors we already follow (status FOLLOWED) → the persisted interaction
    alone is enough: the unfollow worker never unfollows a user with a
    persisted interaction.

Usage in main.py::

    from workers.attention_worker import AttentionWorker
    worker = AttentionWorker(shutdown_event)
    worker.start()
"""

import threading
import time

from core.config import ATTENTION_POLL_INTERVAL_HOURS, MY_USERNAME
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
WORKER_KEY = "attention"


class AttentionWorker(threading.Thread):
    """Daemon thread that persists interactions with the owner's repos.

    Creates its own Database and GithubClient instances to avoid SQLite
    thread-safety issues (same pattern as the other workers).

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    poll_interval_hours : int, optional
        Hours between event-timeline polls.  Default from config
        ``ATTENTION_POLL_INTERVAL_HOURS`` (1 hour).
    """

    def __init__(
        self, shutdown_event, poll_interval_hours=ATTENTION_POLL_INTERVAL_HOURS,
    ):
        super().__init__(daemon=True, name="AttentionWorker")
        self._shutdown = shutdown_event
        self._interval = poll_interval_hours * 3600

    def run(self):
        # Import inside thread to avoid circular imports at module level
        from core.database import Database
        from core.github_client import GithubClient

        db = Database()
        github = GithubClient()

        log.info(
            "Attention worker started (poll interval=%ds)", self._interval,
        )
        mark_started(db, WORKER_KEY)

        try:
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("Attention worker paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue
                try:
                    self._poll_owner_events(db, github)
                    mark_action(db, WORKER_KEY)
                except Exception as exc:
                    mark_error(db, WORKER_KEY, f"{type(exc).__name__}: {exc}")
                    log.exception("Attention worker error")

                if not sleep_interruptible(
                    self._shutdown, self._interval, db=db, key=WORKER_KEY,
                ):
                    break
        finally:
            mark_stopped(db, WORKER_KEY)
            db.conn.close()
            log.info("Attention worker stopped.")

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    def _poll_owner_events(self, db, github):
        """One poll of the owner's received-events timeline.

        Records every matching interaction (deduped on ``event_id``) and
        adds new actors to the pipeline with source ``repo_interaction``.
        """
        from core.github_client import (
            GitHubAuthError,
            GitHubNetworkError,
            GitHubRateLimitError,
        )
        from services.interactions import (
            event_matches,
            owner_repo_full_names,
        )

        owner = db.get_owner()
        if not owner:
            log.debug("No owner set — skipping attention poll.")
            return

        # The events feed covers every repo the owner touches, so the
        # filter needs the owner's own repos (from DB, live fetch as a
        # fallback) to pick out interactions WITH the owner.
        owner_repo_full = owner_repo_full_names(
            github, owner, db.owner_repository_names(),
        )

        touch_heartbeat(db, WORKER_KEY)
        try:
            events = github.user_events(owner)
        except GitHubAuthError as exc:
            log.error(
                "AUTH FAILURE (%s) on %s — token revoked/expired. Aborting.",
                exc.status_code, exc.url,
            )
            self._shutdown.set()
            return
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) fetching owner events — retrying next cycle",
                exc.status_code,
            )
            return
        except GitHubNetworkError as exc:
            log.warning(
                "Network error fetching owner events: %s — retrying next cycle",
                exc,
            )
            return

        recorded = 0
        new_actors = 0
        for event in events or []:
            if self._shutdown.is_set():
                return
            if not event_matches(event, owner, owner_repo_full):
                continue

            actor = (event.get("actor") or {}).get("login")
            if not actor or actor == owner:
                continue

            event_id = event.get("id")
            event_type = event.get("type")
            created_at = event.get("created_at")
            repo_full = (event.get("repo") or {}).get("name")
            if not event_id or not event_type or not created_at:
                continue

            # Deduped on event_id — only a genuinely new event is handled.
            added = db.record_interaction(
                actor, event_type, repo_full, event_id, created_at,
            )
            if not added:
                continue
            recorded += 1

            if db.get_user_cached_info(actor) is None:
                db.add_user(actor, "repo_interaction")
                new_actors += 1
                log.info(
                    "Attention: new interactor %s (%s on %s) added to pipeline",
                    actor, event_type, repo_full or "profile",
                )

        if recorded:
            log.info(
                "Attention poll: %d new interaction(s), %d new actor(s) "
                "added to the queue.",
                recorded, new_actors,
            )
        else:
            log.debug("Attention poll: no new interactions.")
