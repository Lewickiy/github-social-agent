"""Migration 024 — ``ml_training_runs``: per-retrain history for the ML tab.

The dashboard's new ML page shows the state of the followback predictor
and how it evolves across retrains: dataset size, feature count, hold-out
metrics, the honest k-fold CV numbers, and — after the background
recompute that follows every retrain — the resulting prediction
distribution over the whole population.

Before this table the only history was the *log file* (ephemeral) and the
pruned ``models/model_NNN.json`` files (only ``ML_KEEP_MODELS`` survive).
Each successful ``train_and_save`` now inserts a row (see
``Database.record_ml_training_run``) and the recompute worker fills the
``recompute_*`` columns (``Database.update_ml_training_run_recompute``).

The backfill below reconstructs rows for models that were trained before
this migration existed, reading their metadata JSON files from
``ML_MODEL_DIR`` (the models volume, mounted in both containers).
"""

import json
import os

from core.config import ML_MODEL_DIR

# Columns stored directly from the trainer's metadata dict (JSON metadata
# keys → SQLite columns; list columns are serialised as JSON text).
_COLUMNS = [
    "version", "trained_at", "label_scheme", "num_samples",
    "num_positives", "num_negatives", "input_dim", "hidden_dim",
    "dropout", "top_languages", "top_topics", "val_accuracy",
    "val_auc", "val_precision", "val_recall", "val_loss",
    "pred1_ratio", "cv_folds", "cv_accuracy", "cv_auc",
    "cv_precision", "cv_recall", "cv_pred1_ratio", "epochs",
    "early_stopped", "device", "training_time_seconds",
]

_JSON_COLUMNS = {"top_languages", "top_topics"}


def up(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_training_runs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            version               INTEGER NOT NULL,
            trained_at            TEXT,
            label_scheme          TEXT,
            num_samples           INTEGER,
            num_positives         INTEGER,
            num_negatives         INTEGER,
            input_dim             INTEGER,
            hidden_dim            INTEGER,
            dropout               REAL,
            top_languages         TEXT,
            top_topics            TEXT,
            val_accuracy          REAL,
            val_auc               REAL,
            val_precision         REAL,
            val_recall            REAL,
            val_loss              REAL,
            pred1_ratio           REAL,
            cv_folds              INTEGER,
            cv_accuracy           REAL,
            cv_auc                REAL,
            cv_precision          REAL,
            cv_recall             REAL,
            cv_pred1_ratio        REAL,
            epochs                INTEGER,
            early_stopped         INTEGER,
            device                TEXT,
            training_time_seconds REAL,
            -- Population recompute that follows every retrain (filled by
            -- the background MLRecomputeWorker thread).
            recompute_total       INTEGER,
            recompute_pred_1      INTEGER,
            recompute_pred_0      INTEGER,
            recompute_failed      INTEGER,
            recompute_done_at     TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ml_runs_version   ON ml_training_runs(version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ml_runs_trained_at ON ml_training_runs(trained_at)")
    conn.commit()

    # ── Backfill: historical model metadata (pre-migration retrains) ──
    existing = {
        r[0]
        for r in conn.execute("SELECT version FROM ml_training_runs").fetchall()
    }
    model_dir = os.path.abspath(ML_MODEL_DIR)
    inserted = 0
    if os.path.isdir(model_dir):
        for fname in sorted(os.listdir(model_dir)):
            if not (fname.startswith("model_") and fname.endswith(".json")):
                continue
            path = os.path.join(model_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            version = meta.get("version")
            if not version or version in existing:
                continue
            values = []
            for col in _COLUMNS:
                v = meta.get(col)
                if col in _JSON_COLUMNS and v is not None:
                    v = json.dumps(v)
                values.append(v)
            conn.execute(
                f"INSERT INTO ml_training_runs ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                values,
            )
            existing.add(version)
            inserted += 1

    conn.commit()
    print(f"  Backfilled {inserted} historical training run(s) into ml_training_runs.")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS ml_training_runs")
    conn.commit()
