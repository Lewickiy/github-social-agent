"""Shared runtime for background workers — status reporting + pause support.

Every daemon worker reports its lifecycle (started / stopped / last
action / last error / heartbeat) into the ``worker_status`` table so the
Management tab can show live per-worker state, and honours an ``enabled``
toggle persisted in that same table: while disabled, the worker skips its
work cycle and simply waits, so workers can be paused/resumed from the UI
without restarting the bot.

All functions degrade gracefully when the ``worker_status`` table does
not exist yet (migrations not applied): status writes become no-ops and
``is_enabled`` defaults to True, so the bot never breaks on an
un-migrated database.
"""

import threading
import time

from core.logger import get_logger

log = get_logger(__name__)

# Heartbeat cadence inside the shared interruptible sleep — keeps the
# row's heartbeat_at fresh even during long (hour/day) sleeps.
HEARTBEAT_EVERY_SECONDS = 60

# How long after the last heartbeat a worker is treated as unreachable
# (bot process down/crashed) by the dashboard.
ALIVE_WINDOW_SECONDS = 5 * 60

# Re-check cadence while paused (a toggle change applies within this window).
PAUSE_POLL_SECONDS = 5

# Throttle heartbeat writes per worker: the 1 s interruptible sleep would
# otherwise write once per second per worker.  Per-process state — each
# worker only lives in the bot process, so a single shared dict is fine.
_last_heartbeat: dict[str, float] = {}
_heartbeat_lock = threading.Lock()


def _heartbeat_due(key):
    with _heartbeat_lock:
        last = _last_heartbeat.get(key, 0.0)
        now = time.monotonic()
        if now - last < HEARTBEAT_EVERY_SECONDS:
            return False
        _last_heartbeat[key] = now
        return True


def _safe(db, key, what, fn):
    """Run a worker-status write, never letting it break the worker.

    Status bookkeeping is best-effort telemetry: a transient DB error
    (e.g. a lock) must not kill a worker thread or mask a real error in
    ``mark_error``.  The DB methods are already no-ops pre-migration, so
    this guard only fires on genuinely broken connections.
    """
    try:
        fn()
    except Exception:
        log.warning("Worker status %s failed for %s", what, key)


def touch_heartbeat(db, key):
    """Record a liveness tick for worker *key* (throttled to ~1/min)."""
    if db is not None and key is not None and _heartbeat_due(key):
        _safe(db, key, "heartbeat", lambda: db.touch_worker_heartbeat(key))


def mark_started(db, key):
    _safe(db, key, "started", lambda: db.mark_worker_started(key))


def mark_stopped(db, key):
    _safe(db, key, "stopped", lambda: db.mark_worker_stopped(key))


def mark_action(db, key):
    _safe(db, key, "action", lambda: db.mark_worker_action(key))


def mark_error(db, key, message):
    _safe(db, key, "error", lambda: db.mark_worker_error(key, message))


def is_enabled(db, key, default=True):
    """True when worker *key* is toggled on (default True pre-migration)."""
    try:
        return db.worker_enabled(key, default=default)
    except Exception:
        # A failed toggle read must not stop the worker — keep it running.
        log.warning("Worker status read failed for %s — assuming enabled", key)
        return True


def sleep_interruptible(shutdown_event, seconds, db=None, key=None):
    """Sleep *seconds*, waking early on shutdown, keeping the heartbeat fresh.

    Returns True when the full sleep completed, False on shutdown.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if shutdown_event.is_set():
            return False
        touch_heartbeat(db, key)
        remaining = deadline - time.monotonic()
        time.sleep(max(0.0, min(1.0, remaining)))
    return not shutdown_event.is_set()


def wait_until_enabled(db, key, shutdown_event, poll=PAUSE_POLL_SECONDS):
    """Sleep until worker *key* is toggled on again or shutdown is requested.

    Called when a worker finds itself paused, so re-enabling from the
    dashboard takes effect within ~*poll* seconds.  Returns False on
    shutdown (caller should exit).
    """
    while not shutdown_event.is_set():
        if is_enabled(db, key):
            return True
        if not sleep_interruptible(shutdown_event, poll, db=db, key=key):
            return False
    return False
