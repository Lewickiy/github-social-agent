"""
Evaluate the current FOLLOWBACK model — a CLI diagnostic.

Answers two questions without touching the training pipeline:

1. *Can the model discriminate?*  It runs the current model over every
   labeled training user (positives = followed back, negatives = no
   followback in 7+ days) and reports accuracy / AUC / precision /
   recall plus the fraction of predicted-1s — the "did it degenerate
   to constant-1 again" check.

2. *What does it do on real (unlabeled) accounts?*  It samples N users
   from the live queue (NEW, scored, with repos — the same population
   silent-mode scores) and prints the probability distribution, so you
   can see whether predictions actually spread across 0 and 1 instead
   of clustering at ~0.95.

Usage (from the repo root, inside the bot container or venv with torch)::

    python -m ml_service.evaluate --new-sample 300

Exit code is 0; all output is human-readable.
"""

import argparse
import json
import random
import sys

import torch

from core.database import Database
from ml_service.inference import load_model
from ml_service.trainer import _classification_metrics


def _build_vectors(db, usernames, metadata, top_langs, top_topics):
    """Build feature vectors for *usernames* in feature_order order."""
    from ml_service.features import build_feature_vector

    vectors = []
    for uname in usernames:
        profile_json = db.get_user_profile_json(uname)
        repo_agg = db.get_user_repo_aggregates(uname)
        user_langs = db.user_languages(uname)
        user_topics = db.user_topics(uname)
        vec = build_feature_vector(
            profile_json, repo_agg, user_langs, user_topics,
            top_langs, top_topics, metadata["feature_order"],
            discovered_from=db.get_discovered_from(uname),
        )
        vectors.append(vec)
    return torch.tensor(vectors, dtype=torch.float32)


def _per_source_auc(db, metadata, training):
    """Per-source AUC breakdown over the labeled training set (issue #25).

    Groups labeled users by their normalised discovery source
    (stargazers / contributors / repo_interaction / owner_followers /
    self / graph) and reports each channel's size and AUC, so the
    evaluation shows whether the model discriminates within each
    discovery channel rather than just overall.

    *metadata* is the already-loaded model metadata (the caller has the
    model loaded — no second disk read here).
    """
    from ml_service.features import normalise_source
    from ml_service.trainer import _classification_metrics

    model, _meta = load_model()
    if model is None:
        return []
    # Use the caller's metadata (already loaded) for feature names —
    # never re-read the model directory mid-evaluation.
    meta = metadata
    top_langs = [(n, 0) for n in meta["top_languages"]]
    top_topics = [(n, 0) for n in meta["top_topics"]]

    by_source = {}
    for uname, lbl in training:
        src = normalise_source(db.get_discovered_from(uname))
        by_source.setdefault(src, []).append((uname, lbl))

    out = []
    for src in sorted(by_source):
        group = by_source[src]
        usernames = [u for u, _ in group]
        labels = torch.tensor([l for _, l in group], dtype=torch.float32)
        if len(usernames) < 2:
            out.append({"source": src, "users": len(usernames), "auc": None})
            continue
        try:
            X = _build_vectors(db, usernames, meta, top_langs, top_topics)
            with torch.no_grad():
                probs = model(X).squeeze(-1)
            m = _classification_metrics(labels, probs)
            out.append({
                "source": src,
                "users": len(usernames),
                "auc": round(m["auc"], 3),
                "pos": int(labels.sum()),
            })
        except Exception as exc:
            out.append({"source": src, "users": len(usernames),
                        "auc": None, "error": str(exc)})
    return out


def main():
    parser = argparse.ArgumentParser(description="Evaluate the current ML model.")
    parser.add_argument(
        "--new-sample", type=int, default=300,
        help="Number of live NEW users to sample for the distribution check.",
    )
    args = parser.parse_args()

    model, metadata = load_model()
    if model is None:
        print("No model found (models/current.pt missing) — nothing to evaluate.")
        sys.exit(1)

    print(f"Model v{metadata['version']}  trained {metadata['trained_at']}")
    print(f"  {metadata['num_samples']} samples "
          f"({metadata['num_positives']}+ / {metadata['num_negatives']}−), "
          f"{metadata['input_dim']} features, hidden={metadata['hidden_dim']}, "
          f"dropout={metadata.get('dropout', 0.0)}")
    print(f"  reported val: acc={metadata.get('val_accuracy')}, "
          f"AUC={metadata.get('val_auc')}, pred1={metadata.get('pred1_ratio')}")
    if metadata.get("cv_auc") is not None:
        print(f"  {metadata.get('cv_folds')}-fold CV: "
              f"acc={metadata.get('cv_accuracy')}, "
              f"AUC={metadata.get('cv_auc')}, "
              f"precision={metadata.get('cv_precision')}, "
              f"recall={metadata.get('cv_recall')}, "
              f"pred1={metadata.get('cv_pred1_ratio')}")
    print()

    db = Database()
    top_langs = [(n, 0) for n in metadata["top_languages"]]
    top_topics = [(n, 0) for n in metadata["top_topics"]]

    # ── 1. Labeled training set ──
    training = db.get_training_users()
    usernames = [u for u, _ in training]
    labels = torch.tensor([lbl for _, lbl in training], dtype=torch.float32)
    if not usernames:
        print("No labeled training users found.")
    else:
        X = _build_vectors(db, usernames, metadata, top_langs, top_topics)
        with torch.no_grad():
            probs = model(X).squeeze(-1)

        m = _classification_metrics(labels, probs)
        probs_sorted = sorted(probs.tolist())
        n = len(probs)
        print(f"=== Labeled training set ({n} users) ===")
        print(f"  labels: {int(labels.sum())} pos / {int((labels == 0).sum())} neg")
        print(f"  pred=1: {int((probs >= 0.5).sum())} ({100 * m['pred1_ratio']:.0f}%), "
              f"pred=0: {int((probs < 0.5).sum())}")
        print(f"  accuracy={m['accuracy']:.3f}  AUC={m['auc']:.3f}  "
              f"precision={m['precision']:.3f}  recall={m['recall']:.3f}")
        print(f"  prob range: {probs_sorted[0]:.3f}..{probs_sorted[-1]:.3f}, "
              f"median={probs_sorted[n // 2]:.3f}")
        pos_p = [p for p, l in zip(probs.tolist(), labels.tolist()) if l == 1]
        neg_p = [p for p, l in zip(probs.tolist(), labels.tolist()) if l == 0]
        if pos_p:
            print(f"  pos mean prob={sum(pos_p) / len(pos_p):.3f}, "
                  f"neg mean prob={sum(neg_p) / len(neg_p):.3f}")

        # Per-source AUC breakdown (issue #25) — the model's discrimination
        # within each discovery channel.
        per_source = _per_source_auc(db, metadata, training)
        if per_source:
            print("  per-source AUC:")
            for row in per_source:
                auc = (
                    f"{row['auc']:.3f}" if row.get("auc") is not None
                    else "n/a (too few)"
                )
                pos = row.get("pos", "?")
                print(
                    f"    {row['source']:<16s} n={row['users']:>3d} "
                    f"pos={pos:>3d} AUC={auc}"
                )
        print()

    # ── 2. Live NEW users (the scoring population) ──
    rows = db.conn.execute(
        """
        SELECT u.username FROM users u
        LEFT JOIN user_current_status c ON c.username = u.username
        WHERE COALESCE(c.status, 'NEW') = 'NEW'
          AND u.score IS NOT NULL AND u.score > 0
          AND u.repos_fetched_at IS NOT NULL
          AND EXISTS (SELECT 1 FROM repositories WHERE user_id = u.username)
        """
    ).fetchall()
    all_new = [r[0] for r in rows]
    sample = random.sample(all_new, min(args.new_sample, len(all_new)))

    if not sample:
        db.conn.close()
        print("No live NEW users to sample.")
        return

    X_new = _build_vectors(db, sample, metadata, top_langs, top_topics)
    db.conn.close()
    with torch.no_grad():
        probs_new = model(X_new).squeeze(-1)
    p = sorted(probs_new.tolist())
    n = len(p)
    print(f"=== Live NEW users sample ({n}) ===")
    print(f"  pred=1: {int((probs_new >= 0.5).sum())} ({100 * int((probs_new >= 0.5).sum()) / n:.0f}%), "
          f"pred=0: {int((probs_new < 0.5).sum())}")
    print(f"  prob range: {p[0]:.3f}..{p[-1]:.3f}, median={p[n // 2]:.3f}")
    for lo, hi, label in ((0.0, 0.2, "<0.2"), (0.2, 0.4, "0.2-0.4"),
                          (0.4, 0.6, "0.4-0.6"), (0.6, 0.8, "0.6-0.8"),
                          (0.8, 1.01, "≥0.8")):
        cnt = sum(1 for v in p if lo <= v < hi)
        bar = "#" * int(cnt * 50 / n)
        print(f"  {label:8s} {cnt:4d}  {bar}")
    print("\nVerdict: " + (
        "model discriminates (probabilities spread across 0/1)"
        if int((probs_new >= 0.5).sum()) not in (0, n)
        else "model still degenerate — constant prediction, no discrimination"
    ))


if __name__ == "__main__":
    main()
