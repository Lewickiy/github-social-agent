"""
Training logic for the FOLLOWBACK predictor.

Provides a pure function that reads training data from the DB,
builds feature vectors, trains the PyTorch model, and saves it
to disk with metadata.

Why this pipeline is built the way it is
----------------------------------------
The training set is tiny (a few hundred labeled users) and positive-
skewed (~60 %), so the classic failure mode — seen in the old
``POS_WEIGHT_MULTIPLIER=3`` version — is the model degenerating into
"predict 1 for everyone" (that *is* the loss-minimising constant when
positives are weighted ×3 on an already 60 %-positive set).  The fixes:

* **Class-balanced weights** — each sample weighted by ``1/class_count``
  so the effective positive/negative weight ratio is exactly 1:1.  The
  model can no longer win by predicting the majority class.
* **Stratified train/val split** — both classes always appear in both
  splits, so val metrics aren't noise from a split that happened to
  contain 1 negative.
* **Early stopping** on validation loss — prevents memorising the ~100
  training rows (the old fixed-50-epoch run could overfit by epoch 20).
* **Dropout** between hidden layers — regularises further.
* **Honest metrics** — the single 80/20 split leaves ~24 validation
  examples, and a different random split of the *same* data bounced
  ``val_acc`` between 0.54 and 0.77 (v019 vs v020).  So beyond the
  hold-out metrics we also report a **stratified 5-fold cross-validation**
  mean over the whole dataset — that is the number to trust when judging
  whether the model actually discriminates.  ``pred1_ratio`` (the "did we
  degenerate again" check) is reported for both.
* **Reproducibility** — every random step (model init, batch shuffling,
  dropout, train/val split) is seeded with ``ML_SEED`` (``torch.manual_seed``
  at the top of each fit + a seeded generator in the split), so the *same*
  dataset always produces the *same* model and the *same* metrics.  The
  seed is stored in each model's metadata for provenance.
* **Dataset gate** — training waits until both classes are decently
  represented (``ML_MIN_POSITIVE_SAMPLES`` / ``ML_MIN_NEGATIVE_SAMPLES`` /
  ``ML_MIN_TOTAL_SAMPLES`` in config).  On a couple of hundred samples the
  net has nothing to learn and CV AUC just sits in the 0.5–0.6 noise band,
  so the trainer skips those runs entirely.
"""

import json
import os
import time
from datetime import datetime, timezone

import torch
import torch.nn.functional as F
import torch.optim as optim

from core.config import (
    ML_DROPOUT,
    ML_EARLY_STOP_PATIENCE,
    ML_EPOCHS,
    ML_HIDDEN_DIM,
    ML_KEEP_MODELS,
    ML_MIN_NEGATIVE_SAMPLES,
    ML_MIN_POSITIVE_SAMPLES,
    ML_MIN_TOTAL_SAMPLES,
    ML_MODEL_DIR,
    ML_SEED,
    ML_TOP_LANGUAGES,
    ML_TOP_TOPICS,
)
from core.logger import get_logger

from .features import build_feature_vector_for_training
from .model import FollowbackPredictor

log = get_logger(__name__)

# ── Training hyper-parameters ─────────────────────────────────────────────
BATCH_SIZE = 64
LEARNING_RATE = 0.001
WEIGHT_DECAY = 1e-4
TRAIN_SPLIT = 0.8
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Minimum positive + negative examples required to train.  Sourced from
# config so the dataset-size gate is tunable without touching code.
MIN_POSITIVE_SAMPLES = ML_MIN_POSITIVE_SAMPLES
MIN_NEGATIVE_SAMPLES = ML_MIN_NEGATIVE_SAMPLES
MIN_TOTAL_SAMPLES = ML_MIN_TOTAL_SAMPLES

# Label scheme: 1 = user followed us back (any time), 0 = no followback
# within 7+ days of our follow.  Kept in model metadata so each saved
# model records which rule produced it.
LABEL_SCHEME = "followback_vs_no_followback_7d_plus"

# ── Cross-validation ──────────────────────────────────────────────────────
# A single 80/20 stratified split leaves only ~24 validation examples, and
# a different random split of identical data moved val_acc 0.54 → 0.77
# (v019 → v020).  The reported val_* metrics are therefore noise; the
# cv_* means below are the honest estimate of model quality.  Fold
# assignment is seeded with the same ML_SEED as everything else.
CV_FOLDS = 5


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
        "ML training: %d positive (followed back), %d negative (no followback in 7+ days)",
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

        # Interaction features (issue #25): ONLY events before our follow
        # — anything after would leak the label.  A user who was never
        # followed (no followed_at) gets no interaction features at all
        # (the conservative empty set).
        followed_at = db.get_followed_at(username)
        interactions = (
            db.user_interactions_before(username, followed_at)
            if followed_at else []
        )
        discovered_from = db.get_discovered_from(username)

        vec, names = build_feature_vector_for_training(
            profile_json, repo_agg, user_langs, user_topics,
            top_langs, top_topics,
            interactions=interactions, discovered_from=discovered_from,
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

    # ── 5. Stratified train/val split ──
    # Both classes must appear in both splits (a random split of 119 rows
    # can produce a val fold with 1 negative — val_acc then says nothing).
    train_idx, val_idx = _stratified_split(y, TRAIN_SPLIT)
    X_train, y_train = X[train_idx], y[train_idx]
    X_val, y_val = X[val_idx], y[val_idx]

    log.info(
        "ML training: %d train / %d validation samples "
        "(train %d pos/%d neg, val %d pos/%d neg)",
        len(train_idx), len(val_idx),
        int(y_train.sum()), int((y_train == 0).sum()),
        int(y_val.sum()), int((y_val == 0).sum()),
    )

    # ── 6-7. Fit (class-balanced loss, early stopping) ──
    model, best_val_loss, best_epoch, early_stopped, elapsed = _fit_model(
        X_train, y_train, X_val, y_val, verbose=True,
    )

    # ── 8. Evaluate the best model on the hold-out ──
    model.eval()
    with torch.no_grad():
        val_probs = model(X_val.to(DEVICE)).squeeze(-1)
        metrics = _classification_metrics(y_val.cpu(), val_probs.cpu())

    # ── 8b. Honest estimate: stratified k-fold CV over the whole set ──
    cv = _kfold_metrics(X, y)
    if cv is not None:
        log.info(
            "ML CV (%d-fold): acc=%.3f  AUC=%.3f  precision=%.3f  recall=%.3f  pred1=%.0f%%",
            CV_FOLDS, cv["accuracy"], cv["auc"], cv["precision"],
            cv["recall"], cv["pred1_ratio"] * 100,
        )
    else:
        log.warning("ML CV skipped — too few samples of a class per fold.")

    log.info(
        "ML val metrics: acc=%.3f  AUC=%.3f  precision=%.3f  recall=%.3f  pred1=%.0f%%",
        metrics["accuracy"], metrics["auc"], metrics["precision"],
        metrics["recall"], metrics["pred1_ratio"] * 100,
    )

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
        "seed": ML_SEED,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "label_scheme": LABEL_SCHEME,
        "num_samples": len(training_users),
        "num_positives": len(positives),
        "num_negatives": len(negatives),
        "input_dim": input_dim,
        "hidden_dim": ML_HIDDEN_DIM,
        "dropout": ML_DROPOUT,
        "feature_order": feature_order,
        "top_languages": [name for name, _ in top_langs],
        "top_topics": [name for name, _ in top_topics],
        "val_accuracy": round(metrics["accuracy"], 4),
        "val_auc": round(metrics["auc"], 4),
        "val_precision": round(metrics["precision"], 4),
        "val_recall": round(metrics["recall"], 4),
        "val_loss": round(best_val_loss, 4),
        "pred1_ratio": round(metrics["pred1_ratio"], 4),
        "cv_folds": CV_FOLDS if cv else 0,
        "cv_accuracy": round(cv["accuracy"], 4) if cv else None,
        "cv_auc": round(cv["auc"], 4) if cv else None,
        "cv_precision": round(cv["precision"], 4) if cv else None,
        "cv_recall": round(cv["recall"], 4) if cv else None,
        "cv_pred1_ratio": round(cv["pred1_ratio"], 4) if cv else None,
        "epochs": best_epoch,
        "early_stopped": early_stopped,
        "device": DEVICE,
        "training_time_seconds": round(elapsed, 1),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    # ── 9b. Persist a history row for the dashboard's ML tab ──
    # ml_training_runs accumulates one row per retrain (metrics + the
    # recompute outcome later filled in by the background worker), so the
    # UI can show how dataset, features and CV quality evolved over time.
    # A missing table (migrations not applied) must never break training.
    try:
        metadata["run_id"] = db.record_ml_training_run(metadata)
    except Exception:
        log.exception("ML: failed to record training run history")
        metadata["run_id"] = None

    # Also copy metadata as current.json for inference
    current_meta_path = os.path.join(ML_MODEL_DIR, "current.json")
    with open(current_meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    # ── 10. Prune old models (keep only the most recent ML_KEEP_MODELS) ──
    pruned = _prune_old_models(version, keep=ML_KEEP_MODELS)
    if pruned:
        log.info("ML: pruned old models: %s", ", ".join(pruned))

    log.info(
        "ML model v%03d saved: %d features, val acc=%.1f%%, CV AUC=%.3f",
        version, input_dim, metrics["accuracy"] * 100,
        cv["auc"] if cv else 0.0,
    )
    log.info(
        "ML: recorded training run v%03d (run_id=%s)",
        version, metadata.get("run_id"),
    )
    return metadata


# ── Core fitting loop (shared by single-split and CV folds) ───────────────

def _fit_model(X_train, y_train, X_val, y_val, verbose=False):
    """Train FollowbackPredictor with class-balanced BCE + early stopping.

    Returns ``(model, best_val_loss, best_epoch, early_stopped, elapsed_s)``
    with the model already restored to its best validation state.
    *verbose* enables per-epoch progress logging (used for the main fit
    only — CV folds would spam the log).

    Fully deterministic: the seed is reset at the top of every call, so
    model init, batch shuffling and dropout all reproduce exactly — both
    for the main fit and for each CV fold.
    """
    torch.manual_seed(ML_SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(ML_SEED)

    model = FollowbackPredictor(
        X_train.shape[1], hidden_dim=ML_HIDDEN_DIM, dropout=ML_DROPOUT,
    ).to(DEVICE)
    optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)

    # Per-sample loss weights: class-balanced (1/count per class, then
    # normalised so the weights sum to n).  This replaces the old fixed
    # POS_WEIGHT_MULTIPLIER which made "predict all-1s" optimal.
    train_weights = _balanced_weights(y_train).to(DEVICE)
    val_weights = _balanced_weights(y_val).to(DEVICE)

    best_val_loss = float("inf")
    best_state = None
    best_epoch = 0
    epochs_no_improve = 0
    start_time = time.monotonic()

    for epoch in range(ML_EPOCHS):
        model.train()
        perm = torch.randperm(len(X_train))
        total_loss = 0.0
        batches = 0

        for i in range(0, len(X_train), BATCH_SIZE):
            idx = perm[i : i + BATCH_SIZE]
            xb = X_train[idx].to(DEVICE)
            yb = y_train[idx].to(DEVICE)

            optimizer.zero_grad()
            preds = model(xb).squeeze(-1)
            loss = F.binary_cross_entropy(
                preds, yb, weight=train_weights[idx],
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
            val_loss = F.binary_cross_entropy(
                val_preds, val_labels, weight=val_weights,
            ).item()

        if val_loss < best_val_loss - 1e-4:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            best_epoch = epoch + 1
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1

        if verbose and (epoch % 10 == 0 or epoch == ML_EPOCHS - 1):
            log.info(
                "  epoch %3d/%d  train_loss=%.4f  val_loss=%.4f",
                epoch + 1, ML_EPOCHS, avg_train_loss, val_loss,
            )

        if epochs_no_improve >= ML_EARLY_STOP_PATIENCE:
            if verbose:
                log.info(
                    "  early stopping at epoch %d (no val improvement for %d epochs)",
                    epoch + 1, ML_EARLY_STOP_PATIENCE,
                )
            break

    model.load_state_dict(best_state)
    elapsed = time.monotonic() - start_time
    return model, best_val_loss, best_epoch, best_epoch < ML_EPOCHS, elapsed


def _kfold_metrics(X, y):
    """Stratified *CV_FOLDS*-fold CV over the whole dataset.

    Each fold holds out 1/*CV_FOLDS* of each class, trains a fresh model
    on the rest (same architecture / loss / early stopping) and evaluates
    the held-out fold with plain metrics.  Returns the mean metrics dict,
    or None when any fold would lack a class.

    Fold assignment is seeded with ``ML_SEED`` (same as the rest of
    training), so CV numbers are directly comparable between retrains —
    unlike the old random hold-out split.
    """
    n = len(y)
    pos_idx = (y == 1).nonzero(as_tuple=True)[0]
    neg_idx = (y == 0).nonzero(as_tuple=True)[0]
    if len(pos_idx) < CV_FOLDS or len(neg_idx) < CV_FOLDS:
        return None

    g = torch.Generator().manual_seed(ML_SEED)
    pos_idx = pos_idx[torch.randperm(len(pos_idx), generator=g)]
    neg_idx = neg_idx[torch.randperm(len(neg_idx), generator=g)]

    fold_metrics = []
    for fold in range(CV_FOLDS):
        val_idx = torch.cat([pos_idx[fold::CV_FOLDS], neg_idx[fold::CV_FOLDS]])
        val_mask = torch.zeros(n, dtype=torch.bool)
        val_mask[val_idx] = True
        train_idx = (~val_mask).nonzero(as_tuple=True)[0]

        model, _, _, _, _ = _fit_model(
            X[train_idx], y[train_idx], X[val_idx], y[val_idx],
        )
        model.eval()
        with torch.no_grad():
            probs = model(X[val_idx].to(DEVICE)).squeeze(-1)
        fold_metrics.append(_classification_metrics(y[val_idx].cpu(), probs.cpu()))

    keys = fold_metrics[0].keys()
    return {k: sum(m[k] for m in fold_metrics) / len(fold_metrics) for k in keys}


# ── Internal helpers ──────────────────────────────────────────────────────

def _balanced_weights(labels):
    """Per-sample loss weights so the effective class weights are 1:1.

    Each sample in class *c* gets ``n / (2 * count_c)`` — the weight sums
    to n across both classes, with the rarer class up-weighted and the
    common one down-weighted.  Returns a 1-D tensor of shape [n].
    """
    n = labels.numel()
    n_pos = labels.sum().item()
    n_neg = n - n_pos
    pos_w = n / max(2 * n_pos, 1)
    neg_w = n / max(2 * n_neg, 1)
    return torch.where(labels == 1, pos_w, neg_w)


def _stratified_split(labels, train_frac):
    """Return (train_idx, val_idx) with both classes in both splits.

    Seeded with ``ML_SEED`` so the same dataset always yields the same
    split — otherwise val metrics are pure split-lottery (val_acc bounced
    0.54 → 0.77 on identical data before the seed).
    """
    g = torch.Generator().manual_seed(ML_SEED)
    pos_idx = (labels == 1).nonzero(as_tuple=True)[0]
    neg_idx = (labels == 0).nonzero(as_tuple=True)[0]
    pos_idx = pos_idx[torch.randperm(len(pos_idx), generator=g)]
    neg_idx = neg_idx[torch.randperm(len(neg_idx), generator=g)]
    n_pos_tr = max(int(len(pos_idx) * train_frac), 1)
    n_neg_tr = max(int(len(neg_idx) * train_frac), 1)
    train_idx = torch.cat([pos_idx[:n_pos_tr], neg_idx[:n_neg_tr]])
    val_idx = torch.cat([pos_idx[n_pos_tr:], neg_idx[n_neg_tr:]])
    return train_idx, val_idx


def _classification_metrics(labels, probs):
    """Accuracy / AUC / precision / recall / pred1-ratio for a prediction set.

    AUC is computed with the Mann-Whitney-U formula (handles ties by
    averaging ranks) — no sklearn dependency.
    """
    n = len(labels)
    n_pos = int(labels.sum())
    n_neg = n - n_pos

    preds = (probs >= 0.5).float()
    tp = int(((preds == 1) & (labels == 1)).sum())
    fp = int(((preds == 1) & (labels == 0)).sum())
    fn = int(((preds == 0) & (labels == 1)).sum())
    tn = int(((preds == 0) & (labels == 0)).sum())

    accuracy = (tp + tn) / max(n, 1)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    pred1_ratio = int((preds == 1).sum()) / max(n, 1)
    auc = _auc(labels, probs)

    return {
        "accuracy": accuracy,
        "auc": auc,
        "precision": precision,
        "recall": recall,
        "pred1_ratio": pred1_ratio,
        "n_pos": n_pos,
        "n_neg": n_neg,
    }


def _auc(labels, probs):
    """Area under the ROC curve for binary labels vs probability scores.

    Pure-torch implementation of the rank-based (Mann-Whitney U) formula;
    tied scores get averaged ranks.  Returns 0.5 when either class is
    missing (no discriminative info).
    """
    n = len(labels)
    n_pos = int(labels.sum())
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5

    order = torch.argsort(probs)  # ascending — rank 1 = lowest prob
    probs_sorted = probs[order]
    labels_sorted = labels[order]

    # Average ranks within tied groups (1-based).
    ranks = torch.arange(1, n + 1, dtype=torch.float)
    group = 0
    while group < n:
        end = group + 1
        while end < n and probs_sorted[end] == probs_sorted[group]:
            end += 1
        if end - group > 1:
            ranks[group:end] = float(group + 1 + end) / 2.0
        group = end

    sum_ranks_pos = ranks[labels_sorted == 1].sum().item()
    return (sum_ranks_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


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


def _prune_old_models(version, keep=ML_KEEP_MODELS):
    """Delete versioned models older than the *keep* most recent.

    Every retrain overwrites ``current.pt``; keeping the last *keep*
    versioned files gives a quick rollback path.  The whole directory
    was accumulating 30+ versions (~40 KB each) for no benefit — the
    newest model (trained on the most data) is always the best.
    Returns the list of removed files.
    """
    if not os.path.isdir(ML_MODEL_DIR):
        return []
    removed = []
    cutoff = version - keep
    for fname in os.listdir(ML_MODEL_DIR):
        for ext in (".pt", ".json"):
            if fname.startswith("model_") and fname.endswith(ext):
                try:
                    v = int(fname.replace("model_", "").replace(ext, ""))
                except ValueError:
                    continue
                if v <= cutoff:
                    path = os.path.join(ML_MODEL_DIR, fname)
                    try:
                        os.remove(path)
                        removed.append(fname)
                    except OSError:
                        pass
    return removed


def _save_model(model, path):
    """Save model state dict + metadata to *path*."""
    torch.save(
        {
            "state_dict": model.state_dict(),
            "input_dim": model.input_dim,
            "hidden_dim": model.hidden_dim,
            "dropout": model.dropout,
        },
        path,
    )
