"""
Training logic for the FOLLOWBACK predictor.

Provides a pure function that reads training data from the DB,
builds feature vectors, trains the PyTorch model, and saves it
to disk with metadata.
"""

import json
import os
import time
from datetime import datetime, timezone

import torch
import torch.nn.functional as F
import torch.optim as optim

from config import ML_MODEL_DIR, ML_TOP_LANGUAGES, ML_TOP_TOPICS
from logger import get_logger

from .features import build_feature_vector_for_training
from .model import FollowbackPredictor

log = get_logger(__name__)

# ── Training hyper-parameters ─────────────────────────────────────────────
BATCH_SIZE = 64
EPOCHS = 50
LEARNING_RATE = 0.001
WEIGHT_DECAY = 1e-4
TRAIN_SPLIT = 0.8
POS_WEIGHT_MULTIPLIER = 3.0   # positives (FOLLOWBACK) are rarer → up-weight
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Minimum positive + negative examples required to train
MIN_POSITIVE_SAMPLES = 5
MIN_NEGATIVE_SAMPLES = 10
MIN_TOTAL_SAMPLES = 30


def train_and_save(db):
    """Run a full training cycle and persist the model.

    Parameters
    ----------
    db : Database
        Database connection (will be read-only, not modified).

    Returns
    -------
    dict | None
        Metadata dict on success, or None if not enough training data.
    """
    # ── 1. Load training users ──
    training_users = db.get_training_users()
    positives = [u for u, lbl in training_users if lbl == 1]
    negatives = [u for u, lbl in training_users if lbl == 0]

    log.info(
        "ML training: %d positive (FOLLOWBACK), %d negative (FOLLOWED>7d/UNFOLLOWED)",
        len(positives), len(negatives),
    )

    if len(positives) < MIN_POSITIVE_SAMPLES or len(negatives) < MIN_NEGATIVE_SAMPLES:
        log.warning(
            "ML training: insufficient samples (need ≥%d pos, ≥%d neg) — skipping.",
            MIN_POSITIVE_SAMPLES, MIN_NEGATIVE_SAMPLES,
        )
        return None

    if len(training_users) < MIN_TOTAL_SAMPLES:
        log.warning(
            "ML training: only %d total samples (need ≥%d) — skipping.",
            len(training_users), MIN_TOTAL_SAMPLES,
        )
        return None

    # ── 2. Global language & topic frequencies (for multi-hot encoding) ──
    top_langs = db.get_global_language_frequencies(ML_TOP_LANGUAGES)
    top_topics = db.get_global_topic_frequencies(ML_TOP_TOPICS)
    log.info(
        "ML training: using top %d languages, top %d topics",
        len(top_langs), len(top_topics),
    )

    # ── 3. Build feature vectors ──
    log.info("ML training: extracting features for %d users ...", len(training_users))
    features_list = []
    labels_list = []
    feature_order = None

    for username, label in training_users:
        profile_json = db.get_user_profile_json(username)
        repo_agg = db.get_user_repo_aggregates(username)
        user_langs = db.user_languages(username)
        user_topics = db.user_topics(username)

        vec, names = build_feature_vector_for_training(
            profile_json, repo_agg, user_langs, user_topics,
            top_langs, top_topics,
        )

        if feature_order is None:
            feature_order = names  # first user determines order
        features_list.append(vec)
        labels_list.append(label)

    input_dim = len(feature_order)
    log.info("ML training: %d features extracted (input_dim=%d)", input_dim, input_dim)

    # ── 4. Convert to tensors ──
    X = torch.tensor(features_list, dtype=torch.float32)
    y = torch.tensor(labels_list, dtype=torch.float32)

    # ── 5. Train/val split ──
    n_total = len(training_users)
    n_train = int(n_total * TRAIN_SPLIT)
    indices = torch.randperm(n_total)
    X_train, y_train = X[indices[:n_train]], y[indices[:n_train]]
    X_val, y_val = X[indices[n_train:]], y[indices[n_train:]]

    log.info("ML training: %d train / %d validation samples", n_train, n_total - n_train)

    # ── 6. Model & optimiser ──
    model = FollowbackPredictor(input_dim, hidden_dim=64).to(DEVICE)
    # Up-weight positives (FOLLOWBACK) to counter class imbalance.
    # NOTE: nn.BCELoss applies `weight` PER BATCH ELEMENT, and only accepts it
    # at construction time — a scalar / 1-element tensor would scale ALL
    # samples equally (a relative no-op).  We instead call the functional
    # F.binary_cross_entropy(... weight=vector) with a per-sample weight
    # vector: positives get ×POS_WEIGHT_MULTIPLIER, negatives ×1.0
    # (computed by _per_sample_weights()).
    optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)

    # ── 7. Training loop ──
    best_val_loss = float("inf")
    best_state = None
    start_time = time.monotonic()

    for epoch in range(EPOCHS):
        model.train()
        # Mini-batch training
        perm = torch.randperm(n_train)
        total_loss = 0.0
        batches = 0

        for i in range(0, n_train, BATCH_SIZE):
            idx = perm[i : i + BATCH_SIZE]
            xb = X_train[idx].to(DEVICE)
            yb = y_train[idx].to(DEVICE)

            optimizer.zero_grad()
            preds = model(xb).squeeze(-1)
            loss = F.binary_cross_entropy(
                preds, yb, weight=_per_sample_weights(yb)
            )
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            batches += 1

        avg_train_loss = total_loss / max(batches, 1)

        # Validation
        model.eval()
        with torch.no_grad():
            val_preds = model(X_val.to(DEVICE)).squeeze(-1)
            val_labels = y_val.to(DEVICE)
            # Same per-sample weighting so val_loss stays comparable to train_loss
            val_loss = F.binary_cross_entropy(
                val_preds, val_labels, weight=_per_sample_weights(val_labels)
            ).item()

            # Accuracy on validation set
            val_binary = (val_preds >= 0.5).float()
            val_acc = (val_binary == val_labels).float().mean().item()

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if epoch % 10 == 0 or epoch == EPOCHS - 1:
            log.info(
                "  epoch %3d/%d  train_loss=%.4f  val_loss=%.4f  val_acc=%.2f%%",
                epoch + 1, EPOCHS, avg_train_loss, val_loss, val_acc * 100,
            )

    elapsed = time.monotonic() - start_time
    log.info("ML training complete in %.1fs (best val_loss=%.4f)", elapsed, best_val_loss)

    # ── 8. Load best state & identify model version ──
    model.load_state_dict(best_state)
    version = _next_version()

    # ── 9. Save model & metadata ──
    os.makedirs(ML_MODEL_DIR, exist_ok=True)

    # Versioned model
    versioned_path = os.path.join(ML_MODEL_DIR, f"model_{version:03d}.pt")
    _save_model(model, versioned_path)

    # Symlink to current.pt (atomic via temp + rename)
    current_tmp = os.path.join(ML_MODEL_DIR, "current.pt.tmp")
    current_path = os.path.join(ML_MODEL_DIR, "current.pt")
    _save_model(model, current_tmp)
    os.replace(current_tmp, current_path)

    # Metadata
    meta_path = os.path.join(ML_MODEL_DIR, f"model_{version:03d}.json")
    metadata = {
        "version": version,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "num_samples": n_total,
        "num_positives": len(positives),
        "num_negatives": len(negatives),
        "input_dim": input_dim,
        "hidden_dim": 64,
        "feature_order": feature_order,
        "top_languages": [name for name, _ in top_langs],
        "top_topics": [name for name, _ in top_topics],
        "val_accuracy": round(val_acc, 4),
        "val_loss": round(best_val_loss, 4),
        "epochs": EPOCHS,
        "device": DEVICE,
        "training_time_seconds": round(elapsed, 1),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    # Also copy metadata as current.json for inference
    current_meta_path = os.path.join(ML_MODEL_DIR, "current.json")
    with open(current_meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    log.info(
        "ML model v%03d saved: %d features, %.1f%% val accuracy",
        version, input_dim, val_acc * 100,
    )
    return metadata


# ── Internal helpers ──────────────────────────────────────────────────────

def _per_sample_weights(labels):
    """Per-sample loss weights: positives ×POS_WEIGHT_MULTIPLIER, negatives ×1.0.

    Must be a vector of shape ``[batch]`` — ``nn.BCELoss`` applies ``weight``
    per batch element, so a scalar / 1-element tensor would scale all samples
    equally (a relative no-op) and do nothing for the class imbalance.
    """
    return torch.where(
        labels == 1, POS_WEIGHT_MULTIPLIER, 1.0
    )


def _next_version():
    """Determine the next model version number from existing files."""
    if not os.path.isdir(ML_MODEL_DIR):
        return 1
    existing = [
        int(f.replace("model_", "").replace(".pt", ""))
        for f in os.listdir(ML_MODEL_DIR)
        if f.startswith("model_") and f.endswith(".pt")
    ]
    return max(existing) + 1 if existing else 1


def _save_model(model, path):
    """Save model state dict + metadata to *path*."""
    torch.save(
        {
            "state_dict": model.state_dict(),
            "input_dim": model.input_dim,
            "hidden_dim": model.hidden_dim,
        },
        path,
    )
