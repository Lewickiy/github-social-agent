"""Migration 019 — Clear scores stamped without collected repos.

Score-only passes (``python main.py --score``) stamped ``score`` /
``scored_at`` / ``score_version`` on users whose repositories were never
collected (``repos_fetched_at IS NULL``).  A score computed without repo
data (languages / topics / recency) is not trustworthy — it pollutes the
"scored" KPI and leaves such users looking processed.  This migration
resets those fields (and the equally untrustworthy ML prediction) to
NULL so the database honours the rule: **no score without collected
repos**.

Users with ``repos_fetched_at IS NOT NULL`` (even with 0 public repos)
are untouched — their scores are based on real collection.
"""


def up(conn):
    cur = conn.execute(
        """
        UPDATE users
        SET score = NULL,
            scored_at = NULL,
            score_version = NULL,
            ml_follow_prediction = NULL
        WHERE repos_fetched_at IS NULL
        """
    )
    conn.commit()
    print(f"  Cleared score fields for {cur.rowcount} users without collected repos.")


def down(conn):
    # Not reversible — the cleared scores were untrustworthy by definition.
    pass
