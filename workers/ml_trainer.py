"""
Background worker for ML model training.

Runs on a configurable interval (default: every 24 hours) and
retrains the FOLLOWBACK prediction model on accumulated data.
After every successful retrain it also recomputes
``users.ml_follow_prediction`` for all users with real data — in a
separate background thread, so the trainer's next sleep cycle is never
blocked by the (fast, ~seconds) re-prediction pass.

Usage in main.py::

    from workers.ml_trainer import MLTrainerWorker
    worker = MLTrainerWorker(shutdown_event)
    worker.start()
"""

import threading

from core.config import (
    ML_ENABLED,
    ML_TRAIN_AFTER_START,
    ML_TRAIN_INTERVAL_HOURS,
)
from core.logger import get_logger
from workers.runtime import (
    is_enabled,
    mark_action,
    mark_started,
    mark_stopped,
    sleep_interruptible,
    wait_until_enabled,
)

log = get_logger(__name__)

# Key of this worker in the worker_status table (dashboard toggle/status).
WORKER_KEY = "ml_trainer"


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
        # Currently running recompute thread (or None).  Training is at most
        # every 24h and a recompute takes seconds, so overlap is normally
        # impossible — the guard + join exist for shutdown hygiene.
        self._recompute_thread = None

    def run(self):
        from core.database import Database

        db = Database()

        log.info(
            "ML trainer worker started (interval=%dh, enabled=%s)",
            ML_TRAIN_INTERVAL_HOURS, ML_ENABLED,
        )
        mark_started(db, WORKER_KEY)

        try:
            if not ML_ENABLED:
                log.info("ML is disabled — trainer worker idle.")
                # Still sleep interruptibly until shutdown
                while not self._shutdown.is_set():
                    if not sleep_interruptible(
                        self._shutdown, 5, db=db, key=WORKER_KEY,
                    ):
                        break
                return

            # ── Initial training (if configured) ──
            if self._train_after_start and is_enabled(db, WORKER_KEY):
                log.info("ML: running initial training cycle ...")
                try:
                    metadata = self._train(db)
                    if metadata is not None:
                        self._recompute_predictions_async(
                            metadata["version"], metadata.get("run_id"),
                        )
                except Exception:
                    log.exception("ML initial training failed")
                mark_action(db, WORKER_KEY)

            # ── Periodic re-training ──
            while not self._shutdown.is_set():
                if not is_enabled(db, WORKER_KEY):
                    log.info("ML trainer paused — waiting for enable")
                    if not wait_until_enabled(db, WORKER_KEY, self._shutdown):
                        break
                    continue

                if not sleep_interruptible(
                    self._shutdown, self._interval, db=db, key=WORKER_KEY,
                ):
                    break
                if self._shutdown.is_set():
                    break

                log.info("ML: starting scheduled training cycle ...")
                try:
                    metadata = self._train(db)
                    if metadata is not None:
                        self._recompute_predictions_async(
                            metadata["version"], metadata.get("run_id"),
                        )
                except Exception:
                    log.exception("ML training cycle failed")
                mark_action(db, WORKER_KEY)
        finally:
            mark_stopped(db, WORKER_KEY)
            db.conn.close()
            # Wait for an in-flight recompute to finish before the process exits
            # — daemon threads holding torch must not be killed mid-run.
            if self._recompute_thread is not None and self._recompute_thread.is_alive():
                log.info("ML: waiting for background recompute to finish ...")
                self._recompute_thread.join(timeout=120)
            log.info("ML trainer worker stopped.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _train(db):
        """Run training and log the result.

        Returns the metadata dict on success, None when training was
        skipped (not enough data).
        """
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
        return metadata

    def _recompute_predictions_async(self, version, run_id=None):
        """Recompute all predictions for the freshly trained model.

        Runs in a separate daemon thread with its *own* Database
        connection — SQLite connections are not thread-safe, so sharing
        the trainer's ``db`` here would corrupt state.  The recompute is
        purely local (no GitHub API) and takes a few seconds for the
        current dataset, so it never perturbs the training cadence.

        When *run_id* is given (the ``ml_training_runs`` row recorded by
        the trainer), the thread also writes the resulting population
        distribution back into that row so the dashboard's ML tab shows
        what each retrain produced.
        """
        # Never stack recomputes: training every 24h means the previous
        # one is long done, but guard anyway (also covers restarts).
        if (
            self._recompute_thread is not None
            and self._recompute_thread.is_alive()
        ):
            log.info("ML: previous recompute still running — skipping.")
            return

        log.info("ML: kicking off background prediction recompute (model v%03d) ...", version)

        def _run():
            from core.database import Database
            from ml_service.recompute_predictions import recompute_all_predictions

            rdb = Database()
            try:
                stats = recompute_all_predictions(rdb)
                if run_id is not None:
                    rdb.update_ml_training_run_recompute(run_id, stats)
                    log.info(
                        "ML: recompute result recorded for run_id=%s "
                        "(%d×1 / %d×0 over %d users).",
                        run_id, stats.get("pred_1"), stats.get("pred_0"),
                        stats.get("total"),
                    )
            except Exception:
                log.exception("ML prediction recompute failed")
            finally:
                rdb.conn.close()

        self._recompute_thread = threading.Thread(
            target=_run, daemon=True, name="MLRecomputeWorker",
        )
        self._recompute_thread.start()
