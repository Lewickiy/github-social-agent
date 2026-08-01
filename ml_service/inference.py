"""
ML inference helpers — model loading and single-user prediction.

Used by ``scorer.py`` and ``silent.py`` during scoring.
Kept separate from ``workers/ml_trainer.py`` because these are
inference utilities, not background worker logic.
"""

import json
import os

import torch

from config import ML_MODEL_DIR
from logger import get_logger

log = get_logger(__name__)


def load_model():
    """Load the latest trained model + metadata from disk.

    Returns
    -------
    tuple (FollowbackPredictor | None, dict | None)
        (model, metadata) — both None if no model exists yet.
    """
    current_path = os.path.join(ML_MODEL_DIR, "current.pt")
    if not os.path.isfile(current_path):
        log.debug("ML model not found at %s — inference disabled.", current_path)
        return None, None

    try:
        checkpoint = torch.load(current_path, map_location="cpu", weights_only=True)
        from ml_service.model import FollowbackPredictor
        model = FollowbackPredictor(
            input_dim=checkpoint["input_dim"],
            hidden_dim=checkpoint["hidden_dim"],
        )
        model.load_state_dict(checkpoint["state_dict"])
        model.eval()
    except Exception:
        log.exception("Failed to load ML model from %s", current_path)
        return None, None

    # Load metadata
    metadata = _load_metadata()

    return model, metadata


def predict_single(model, metadata, db, username):
    """Run inference for a single user and store the prediction.

    Parameters
    ----------
    model : FollowbackPredictor
    metadata : dict
        Must contain ``feature_order``, ``top_languages``, ``top_topics``.
    db : Database
    username : str

    Returns
    -------
    int or None
        0 or 1 if prediction succeeded, None on error.
    """
    from ml_service.features import build_feature_vector

    if model is None or metadata is None:
        return None

    try:
        profile_json = db.get_user_profile_json(username)
        repo_agg = db.get_user_repo_aggregates(username)
        user_langs = db.user_languages(username)
        user_topics = db.user_topics(username)

        top_langs = [(name, 0) for name in metadata["top_languages"]]
        top_topics = [(name, 0) for name in metadata["top_topics"]]

        vec = build_feature_vector(
            profile_json, repo_agg, user_langs, user_topics,
            top_langs, top_topics, metadata["feature_order"],
        )

        tensor = torch.tensor([vec], dtype=torch.float32)
        pred = int(model.predict(tensor).item())
        db.save_ml_prediction(username, pred)
        return pred
    except Exception:
        log.exception("ML inference failed for %s", username)
        return None


# ── Internal ──────────────────────────────────────────────────────────────

def _load_metadata():
    """Load metadata JSON for the current model."""
    meta_path = os.path.join(ML_MODEL_DIR, "current.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Fall back to the latest versioned metadata
    try:
        versioned = sorted(
            [
                os.path.join(ML_MODEL_DIR, f)
                for f in os.listdir(ML_MODEL_DIR)
                if f.startswith("model_") and f.endswith(".json")
            ],
            reverse=True,
        )
        if versioned:
            with open(versioned[0], "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass

    return None
