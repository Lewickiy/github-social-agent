"""
Recompute ``users.ml_follow_prediction`` for every user with real data
using the currently deployed model.

Normal scoring writes the ML prediction only for the users it happens to
score (``predict_single`` in scorer.py / silent.py), so after a retrain
(e.g. v035 → v036) the stored values still reflect the *old* model until
each user is re-scored.  ``recompute_all_predictions`` refreshes them all
at once — it is called automatically by ``MLTrainerWorker`` in a
background thread after every successful retrain, and can also be run
manually from the CLI.

Population: users with real feature data — ``repos_fetched_at IS NOT
NULL`` **and** ``github_profile_json IS NOT NULL``.  Users without a full
profile (the pre-2026-07-31 scoring pass) would otherwise feed a
zero-filled vector — the exact data artifact documented in
``new_ml_model_analyse.md`` — so they are intentionally skipped and their
prediction is left untouched.  Soft-deleted users (current status
DELETED) are excluded as well — they are gone from GitHub and must not
be re-predicted.

No GitHub API calls are made — feature vectors are built entirely from
the DB (profile JSON, repo aggregates, languages, topics).

Usage (inside the bot/dashboard container, which has torch):::

    docker compose exec github-dashboard python -m ml_service.recompute_predictions

    # sanity check on the first 20 users:
    docker compose exec github-dashboard python -m ml_service.recompute_predictions --limit 20
"""

import argparse
import sys
import time

import torch

from core.database import Database
from core.logger import get_logger
from ml_service.inference import load_model

DEFAULT_BATCH = 500  # users per transaction commit

log = get_logger(__name__)


def recompute_all_predictions(db, batch=DEFAULT_BATCH, limit=None, verbose=False):
    """Recompute ``ml_follow_prediction`` for every eligible user.

    Parameters
    ----------
    db : Database
        Open database connection (the caller owns it — the background
        worker passes a thread-local one, never shares across threads).
    batch : int
        Users per transaction commit.
    limit : int | None
        Only recompute the first N users (sanity check).
    verbose : bool
        When True, also print progress to stdout (CLI mode).  The
        background worker leaves it False and relies on the log file.

    Returns
    -------
    dict
        ``{"total", "pred_1", "pred_0", "failed"}`` — counts over the
        recomputed population, plus the number of per-user errors.
        ``total`` is 0 when no model is available.
    """
    model, metadata = load_model()
    if model is None or metadata is None:
        log.warning("ML recompute skipped — no model at models/current.pt.")
        return {"total": 0, "pred_1": 0, "pred_0": 0, "failed": 0}

    log.info(
        "ML recompute: model v%d (%d samples, %d features)",
        metadata["version"], metadata["num_samples"], metadata["input_dim"],
    )
    if verbose:
        print(f"Model v{metadata['version']}  trained {metadata['trained_at']}")
        print(f"  {metadata['num_samples']} samples "
              f"({metadata['num_positives']}+ / {metadata['num_negatives']}−), "
              f"{metadata['input_dim']} features\n")

    rows = db.conn.execute(
        """
        SELECT username FROM users
        WHERE owner = 0
          AND repos_fetched_at IS NOT NULL
          AND github_profile_json IS NOT NULL
          AND username NOT IN (
              SELECT username FROM user_current_status WHERE status = 'DELETED'
          )
        ORDER BY username
        """
    ).fetchall()

    usernames = [r[0] for r in rows]
    if limit is not None:
        usernames = usernames[: limit]

    total = len(usernames)
    log.info("ML recompute: %d users to re-predict (batch=%d)", total, batch)
    if not total:
        return {"total": 0, "pred_1": 0, "pred_0": 0, "failed": 0}
    if verbose:
        print(f"Recomputing predictions for {total} users (batch={batch}) ...\n")

    # Feature setup from the model metadata (same as predict_single).
    from ml_service.features import build_feature_vector
    top_langs = [(name, 0) for name in metadata["top_languages"]]
    top_topics = [(name, 0) for name in metadata["top_topics"]]
    feature_order = metadata["feature_order"]

    # The stored 0/1 must reflect the *current* ML follow threshold (the
    # Management-tab strictness dial), so the FollowWorker gate and the
    # dashboard "ML candidates" stat stay consistent with it.
    threshold = db.get_ml_follow_threshold()

    model.eval()
    pred_1 = 0
    pred_0 = 0
    failed = 0
    start = time.monotonic()

    updates = []
    for idx, username in enumerate(usernames, 1):
        try:
            profile_json = db.get_user_profile_json(username)
            repo_agg = db.get_user_repo_aggregates(username)
            user_langs = db.user_languages(username)
            user_topics = db.user_topics(username)

            vec = build_feature_vector(
                profile_json, repo_agg, user_langs, user_topics,
                top_langs, top_topics, feature_order,
                # Source one-hot (issue #25); interactions excluded at
                # inference for the same leak-avoidance reason as
                # predict_single (no "before the follow" window yet).
                discovered_from=db.get_discovered_from(username),
            )
            tensor = torch.tensor([vec], dtype=torch.float32)
            pred = int(model.predict(tensor, threshold=threshold).item())
            updates.append((pred, username))

            if pred == 1:
                pred_1 += 1
            else:
                pred_0 += 1
        except Exception as exc:
            failed += 1
            if failed <= 5:
                log.warning(
                    "ML recompute: inference failed for %s — %s: %s",
                    username, type(exc).__name__, exc,
                )

        if len(updates) >= batch:
            db.conn.executemany(
                "UPDATE users SET ml_follow_prediction = ? WHERE username = ?",
                updates,
            )
            db.conn.commit()
            updates = []

        if idx % 1000 == 0 or idx == total:
            log.info("ML recompute: [%d/%d] 1=%d 0=%d fail=%d",
                     idx, total, pred_1, pred_0, failed)
            if verbose:
                print(f"  [{idx}/{total}] 1={pred_1} 0={pred_0} "
                      f"fail={failed}")

    if updates:
        db.conn.executemany(
            "UPDATE users SET ml_follow_prediction = ? WHERE username = ?",
            updates,
        )
        db.conn.commit()

    elapsed = time.monotonic() - start
    log.info(
        "ML recompute: done in %.1fs — %d users, %d×1 / %d×0, %d failed.",
        elapsed, total, pred_1, pred_0, failed,
    )
    if verbose:
        print(f"\nDone in {elapsed:.1f}s — {total} users, "
              f"{pred_1}×1 / {pred_0}×0, {failed} failed.")
    return {"total": total, "pred_1": pred_1, "pred_0": pred_0, "failed": failed}


def main():
    parser = argparse.ArgumentParser(
        description="Recompute ml_follow_prediction for all users with the current model.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only recompute the first N users (sanity check).",
    )
    parser.add_argument(
        "--batch", type=int, default=DEFAULT_BATCH,
        help="Users per transaction commit (default: %(default)s).",
    )
    args = parser.parse_args()

    db = Database()
    try:
        stats = recompute_all_predictions(
            db, batch=args.batch, limit=args.limit, verbose=True,
        )

        if not stats["total"]:
            print("Nothing to recompute.")
            return

        # What the stored predictions now look like overall.
        row = db.conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN ml_follow_prediction = 1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN ml_follow_prediction = 0 THEN 1 ELSE 0 END) "
            "FROM users WHERE ml_follow_prediction IS NOT NULL"
        ).fetchone()
        print(
            f"DB totals: {row[0]} users with predictions "
            f"({row[1]}×1 / {row[2]}×0)."
        )
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
