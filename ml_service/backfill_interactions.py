"""One-shot backfill: recover interaction history for the ML population.

The ``interactions`` table starts empty, but already-scored candidates and
already-labeled training users have interaction history we can recover.
This script runs the live check (``services.interactions`` —
``/users/{username}/events/public``, ~90-day window) for:

* every user in ``get_training_users()`` — they need the feature for
  training, and
* every scored, non-deleted user (in-queue candidates) — they need it at
  inference,

and persists every matching event with its REAL timestamp (deduped on
``event_id``).  ~1 API request per user (3 for interactors with older
events); a few hundred users fit one discovery-budget window.

Known limitation (accepted, from the issue): interactions older than the
~90-day events window are not recoverable and are recorded as \"none\" — a
conservative direction of error.

Resumable + ETA like ``ml_service/recompute_predictions.py``: users are
processed in a stable order (training users first, then scored candidates),
each is committed immediately, and a missing/crashed run simply continues
from where it stopped (already-recorded events are deduped).

Usage (inside the bot container or venv)::

    python -m ml_service.backfill_interactions            # full run
    python -m ml_service.backfill_interactions --limit 20 # sanity check
    python -m ml_service.backfill_interactions --source training  # training users only
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from core.config import MY_USERNAME
from core.database import Database
from core.github_client import (
    GitHubAuthError,
    GitHubNetworkError,
    GitHubRateLimitError,
)
from core.logger import get_logger

log = get_logger(__name__)

DEFAULT_BATCH = 50  # users between progress prints / commits


def _target_users(db, source):
    """Usernames to backfill, in a stable order (resume-friendly).

    *source* ``'training'`` — labeled training users only; ``'scored'`` —
    every scored non-deleted user; ``None`` — both (training first).
    """
    users = []
    seen = set()

    if source in (None, "training"):
        for username, _lbl in db.get_training_users():
            if username not in seen:
                seen.add(username)
                users.append(username)

    if source in (None, "scored"):
        rows = db.conn.execute(
            """
            SELECT u.username FROM users u
            LEFT JOIN user_current_status c ON c.username = u.username
            WHERE u.owner = 0
              AND COALESCE(c.status, 'NEW') != 'DELETED'
              AND u.score IS NOT NULL
            ORDER BY u.username
            """
        ).fetchall()
        for (username,) in rows:
            if username not in seen:
                seen.add(username)
                users.append(username)

    return users


def _owner_repos(db, github):
    """Full ``owner/repo`` names for the owner (DB first, live fallback)."""
    from services.interactions import owner_repo_full_names

    return owner_repo_full_names(github, MY_USERNAME, db.owner_repository_names())


def backfill_interactions(db, github, limit=None, batch=DEFAULT_BATCH,
                          source=None, verbose=False):
    """Run the interaction backfill over the target population.

    Returns a stats dict: ``{"total", "users_with", "events_recorded",
    "failed"}``.  ``total`` is 0 when the owner has no repos (nothing to
    match against).
    """
    from services.interactions import find_interactions_with_owner

    owner = db.get_owner()
    if not owner:
        log.warning("Backfill skipped — no owner set.")
        return {"total": 0, "users_with": 0, "events_recorded": 0, "failed": 0}

    users = _target_users(db, source)
    if limit is not None:
        users = users[:limit]

    total = len(users)
    log.info("Backfill: %d user(s) to check (%s)", total,
             source or "training + scored")
    if not total:
        return {"total": 0, "users_with": 0, "events_recorded": 0, "failed": 0}
    if verbose:
        print(f"Backfilling interactions for {total} users "
              f"(~1 API request each) ...\n")

    owner_repo_full = _owner_repos(db, github)
    if not owner_repo_full:
        log.warning(
            "Backfill skipped — owner %s has no known repositories.",
            owner,
        )
        return {"total": 0, "users_with": 0, "events_recorded": 0, "failed": 0}

    users_with = 0
    events_recorded = 0
    failed = 0
    start = time.monotonic()

    for idx, username in enumerate(users, 1):
        try:
            matches = find_interactions_with_owner(
                github, username, owner, owner_repo_full,
            )
        except GitHubAuthError as exc:
            log.error(
                "AUTH FAILURE (%s) on %s — token revoked/expired. Aborting.",
                exc.status_code, exc.url,
            )
            break
        except GitHubRateLimitError as exc:
            log.warning(
                "Rate limit (%s) checking %s — pausing 60s then continuing",
                exc.status_code, username,
            )
            time.sleep(60)
            failed += 1
            continue
        except GitHubNetworkError as exc:
            log.warning(
                "Network error checking %s: %s — skipping", username, exc,
            )
            failed += 1
            continue
        except Exception as exc:
            failed += 1
            if failed <= 5:
                log.warning(
                    "Backfill: unexpected error for %s — %s: %s",
                    username, type(exc).__name__, exc,
                )
            continue

        recorded = 0
        for event in matches:
            event_id = event.get("id")
            event_type = event.get("type")
            created_at = event.get("created_at")
            repo_full = (event.get("repo") or {}).get("name")
            if not event_id or not event_type or not created_at:
                continue
            if db.record_interaction(
                username, event_type, repo_full, event_id, created_at,
            ):
                recorded += 1
        if recorded:
            users_with += 1
            events_recorded += recorded

        # Commit + progress with ETA — immediate persistence makes the run
        # resumable (re-runs dedup on event_id).
        if idx % batch == 0 or idx == total:
            db.conn.commit()
            elapsed = time.monotonic() - start
            rate = idx / max(elapsed, 0.001)
            eta = (total - idx) / rate if rate else 0
            log.info(
                "Backfill: [%d/%d] users_with=%d events=%d failed=%d "
                "ETA %.0fs",
                idx, total, users_with, events_recorded, failed, eta,
            )
            if verbose:
                print(
                    f"  [{idx}/{total}] with-interaction={users_with} "
                    f"events={events_recorded} fail={failed} ETA "
                    f"{int(eta)}s"
                )

    db.conn.commit()
    elapsed = time.monotonic() - start
    log.info(
        "Backfill: done in %.1fs — %d users, %d with interaction(s), "
        "%d event(s) recorded, %d failed.",
        elapsed, total, users_with, events_recorded, failed,
    )
    if verbose:
        print(f"\nDone in {elapsed:.1f}s — {total} users, "
              f"{users_with} with interaction(s), {events_recorded} event(s), "
              f"{failed} failed.")
    return {
        "total": total,
        "users_with": users_with,
        "events_recorded": events_recorded,
        "failed": failed,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Backfill interaction history for the ML population.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N users (sanity check).",
    )
    parser.add_argument(
        "--batch", type=int, default=DEFAULT_BATCH,
        help="Progress print / commit every N users (default: %(default)s).",
    )
    parser.add_argument(
        "--source", choices=("training", "scored"), default=None,
        help="Backfill only training users or only scored candidates "
             "(default: both, training first).",
    )
    args = parser.parse_args()

    db = Database()
    github = None
    try:
        from core.github_client import GithubClient

        github = GithubClient()
        stats = backfill_interactions(
            db, github, limit=args.limit, batch=args.batch,
            source=args.source, verbose=True,
        )
        if not stats["total"]:
            print("Nothing to backfill.")
            return

        row = db.conn.execute(
            "SELECT COUNT(DISTINCT username), COUNT(*) FROM interactions"
        ).fetchone()
        print(
            f"DB totals: {row[0]} users with {row[1]} interaction(s) recorded."
        )
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
