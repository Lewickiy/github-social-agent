"""
Background worker for ML model training.

Runs on a configurable interval (default: every 24 hours) and
retrains the FOLLOWBACK prediction model on accumulated data.

Usage in main.py::

    from workers.ml_trainer import MLTrainerWorker
    worker = MLTrainerWorker(shutdown_event)
    worker.start()
"""

import threading
import time

from core.config import (
    ML_ENABLED,
    ML_TRAIN_AFTER_START,
    ML_TRAIN_INTERVAL_HOURS,
)
from core.logger import get_logger

log = get_logger(__name__)


class MLTrainerWorker(threading.Thread):
    """Daemon thread that periodically retrains the ML model.

    Creates its own Database instance to avoid SQLite
    thread-safety issues.  The GitHub API is not needed.

    Parameters
    ----------
    shutdown_event : threading.Event
        Shared with the main process — set on Ctrl+C / SIGTERM.
    train_interval_hours : int, optional
        Hours between training cycles.  Default from config
        ``ML_TRAIN_INTERVAL_HOURS`` (24 hours).
    train_after_start : bool, optional
        Run an initial training cycle immediately on startup.
        Default from config ``ML_TRAIN_AFTER_START`` (True).
    """

    def __init__(
        self,
        shutdown_event,
        train_interval_hours=ML_TRAIN_INTERVAL_HOURS,
        train_after_start=ML_TRAIN_AFTER_START,
    ):
        super().__init__(daemon=True, name="MLTrainerWorker")
        self._shutdown = shutdown_event
        self._interval = train_interval_hours * 3600
        self._train_after_start = train_after_start and ML_ENABLED

    def run(self):
        from core.database import Database

        db = Database()

        log.info(
            "ML trainer worker started (interval=%dh, enabled=%s)",
            ML_TRAIN_INTERVAL_HOURS, ML_ENABLED,
        )

        if not ML_ENABLED:
            log.info("ML is disabled — trainer worker idle.")
            # Still sleep interruptibly until shutdown
            while not self._shutdown.is_set():
                time.sleep(5)
            db.conn.close()
            return

        # ── Initial training (if configured) ──
        if self._train_after_start:
            log.info("ML: running initial training cycle ...")
            try:
                self._train(db)
            except Exception:
                log.exception("ML initial training failed")

        # ── Periodic re-training ──
        while not self._shutdown.is_set():
            deadline = time.monotonic() + self._interval
            while time.monotonic() < deadline and not self._shutdown.is_set():
                time.sleep(1)

            if self._shutdown.is_set():
                break

            log.info("ML: starting scheduled training cycle ...")
            try:
                self._train(db)
            except Exception:
                log.exception("ML training cycle failed")

        db.conn.close()
        log.info("ML trainer worker stopped.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _train(db):
        """Run training and log the result."""
        from ml_service.trainer import train_and_save

        metadata = train_and_save(db)
        if metadata is None:
            log.warning("ML training skipped — not enough data.")
        else:
            log.info(
                "ML model v%03d saved — %.1f%% accuracy on %d samples.",
                metadata["version"],
                metadata["val_accuracy"] * 100,
                metadata["num_samples"],
            )


