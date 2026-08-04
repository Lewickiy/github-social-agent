"""Read-only dashboard queries over the bot's SQLite database.

All functions take a ``Database`` instance (from ``database.py``) and
return plain dicts/lists ready to be JSON-serialised by the API layer.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from core.config import CURRENT_SCORE_VERSION, GITHUB_API_RATE_LIMIT, ML_MODEL_DIR
from core.tz import get_timezone, local_midnight_utc, parse_utc

# The Followers growth chart always covers the same fixed window,
# regardless of the Overview interval toggle (which only scopes the
# activity feed / recent-actions block).
FOLLOWERS_HISTORY_DAYS = 30


def _window_since(db, days):
    """Local midnight *days* days ago — the shared window definition.

    ``days=1`` → today 00:00 (calendar "today"); ``days=30`` → 29 days
    ago 00:00 (30 calendar days including today).  Day boundaries use the
    user's timezone (``core.tz``), so "today" starts at the user's local
    midnight.  Every interval-scoped query uses this same boundary so all
    Overview cards (and the activity timeline) agree on one window.
    """
    return local_midnight_utc(db, days - 1).isoformat()


def github_api_usage(db, hours=1, days=None):
    """GitHub API request usage for the overview card.

    Counts every real round-trip the bot made to api.github.com (recorded
    by github_client.record_api_request).  Returns:

    * ``requests_last_hour`` — rolling ``hours``-hour window (the rate),
    * ``requests_in_window`` — requests within the last *days* days
      (drives the interval toggle), None when *days* is not given,
    * today's total, all-time total and the share of the primary hourly
      quota (GITHUB_API_RATE_LIMIT) the rolling window consumed.
    """
    conn = db.conn
    now = datetime.now(timezone.utc)

    def _count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    since = (now - timedelta(hours=hours)).isoformat()
    rolling = _count(
        "SELECT COUNT(*) FROM github_api_requests WHERE created_at >= ?",
        (since,),
    )
    in_window = None
    if days is not None:
        in_window = _count(
            "SELECT COUNT(*) FROM github_api_requests WHERE created_at >= ?",
            (_window_since(db, days),),
        )
    today = _count(
        "SELECT COUNT(*) FROM github_api_requests "
        "WHERE created_at >= ?",
        (local_midnight_utc(db).isoformat(),),
    )
    total = _count("SELECT COUNT(*) FROM github_api_requests")

    percent = round((rolling / GITHUB_API_RATE_LIMIT) * 100, 1) if GITHUB_API_RATE_LIMIT else 0.0
    return {
        "requests_last_hour": rolling,
        "requests_in_window": in_window,
        "requests_today": today,
        "requests_total": total,
        "rate_limit": GITHUB_API_RATE_LIMIT,
        "percent_last_hour": percent,
    }


# ── Overview / stats ──────────────────────────────────────────────────────

def overview_stats(db, daily_limit, days=None):
    """KPI numbers for the overview screen.

    When *days* is given (the Overview interval toggle), every pipeline
    count is scoped to the last *days* days using the timestamps we have:

    * discovered  — ``users.created_at``,
    * scored      — ``users.scored_at`` (same eligibility as all-time),
    * followed / followbacks / unfollowed-after-mutual / deleted — users who
      *entered* that status within the window, via the
      ``user_status_history`` transition log (exact ``changed_at`` times
      recorded going forward; backfilled from ``followed_at``/``actions``
      for historical data),
    * ML positives — scored within the window with ``ml_follow_prediction=1``
      (predictions are (re)computed during scoring).

    ``today_follows`` and ``followers_count`` stay "now" metrics (the
    daily budget meter), independent of the window.
    """
    conn = db.conn

    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    if days is not None:
        since = _window_since(db, days)
        created_clause = (" AND u.created_at >= ?", (since,))
        scored_clause = (" AND u.scored_at >= ?", (since,))
    else:
        created_clause = ("", ())
        scored_clause = ("", ())

    # Current status comes from the materialised user_current_status
    # table (the latest user_status_history transition).  "Not deleted"
    # excludes users whose current status is DELETED — written as NOT IN
    # over the tiny deleted set so the wide users rows are never scanned.
    created_w, created_p = created_clause
    total = count(
        "SELECT COUNT(*) FROM users u "
        "WHERE u.owner = 0 "
        "AND u.username NOT IN ("
        "    SELECT username FROM user_current_status WHERE status = 'DELETED'"
        ")" + created_w,
        created_p,
    )
    # "Scored" = users who actually went through scoring with the current
    # algorithm version.  `score > 0` is NOT a good proxy: the scorer
    # legitimately assigns 0 to most low-quality accounts, so counting only
    # positive scores understated real scoring progress (2% of the pipeline
    # instead of the actual ~77%).
    scored_w, scored_p = scored_clause
    scored = count(
        "SELECT COUNT(*) FROM users u "
        "LEFT JOIN user_current_status c ON c.username = u.username "
        "WHERE u.owner = 0 AND COALESCE(c.status, 'NEW') != 'DELETED' "
        "AND u.repos_fetched_at IS NOT NULL "
        "AND u.scored_at IS NOT NULL AND u.score_version = ?" + scored_w,
        (CURRENT_SCORE_VERSION,) + scored_p,
    )
    # Users whose score is actually above zero (follow-worthy).
    scored_positive = count(
        "SELECT COUNT(*) FROM users u "
        "LEFT JOIN user_current_status c ON c.username = u.username "
        "WHERE u.owner = 0 AND COALESCE(c.status, 'NEW') != 'DELETED' "
        "AND u.repos_fetched_at IS NOT NULL AND u.score > 0" + scored_w,
        scored_p,
    )
    # Followed / followbacks / unfollowed-after-mutual / deleted — scoped
    # through the user_status_history transition log: each KPI counts users
    # who *entered* that status within the window at the exact transition
    # time.  This replaces the older ``followed_at``/``created_at``
    # approximations (which could not distinguish "followed long ago" from
    # "followed in the window").
    if days is not None:
        def entered_in_window(status):
            return count(
                "SELECT COUNT(DISTINCT h.username) "
                "FROM user_status_history h "
                "JOIN users u ON u.username = h.username "
                "WHERE u.owner = 0 AND h.status = ? AND h.changed_at >= ?",
                (status, since),
            )

        followed = entered_in_window("FOLLOWED")
        followbacks = entered_in_window("FOLLOWBACK")
        unfollowed = entered_in_window("UNFOLLOWED_AFTER_MUTUAL_FOLLOW")
        deleted = entered_in_window("DELETED")
    else:
        # All-time counts come straight from the narrow materialised
        # table (excluding the owner, a single row) — no wide users scan.
        def current_count(status):
            return count(
                "SELECT COUNT(*) FROM user_current_status "
                "WHERE status = ? "
                "AND username NOT IN (SELECT username FROM users WHERE owner = 1)",
                (status,),
            )

        followed = current_count("FOLLOWED")
        followbacks = current_count("FOLLOWBACK")
        unfollowed = current_count("UNFOLLOWED_AFTER_MUTUAL_FOLLOW")
        deleted = current_count("DELETED")
    # Real processing queue: users still awaiting repo collection / fresh
    # scoring (same eligibility the silent runner uses).  `status = 'NEW'`
    # is not used here — it also counts already-processed users.
    queue = db.count_silent_processing_queue()
    # ML predictions are (re)computed during scoring, so the scored window
    # is the closest timestamp for "predicted within the window".
    ml_positive = count(
        "SELECT COUNT(*) FROM users u "
        "WHERE u.ml_follow_prediction = 1" + scored_w,
        scored_p,
    )
    # "Today" follows the user's local calendar day (core.tz), so the
    # daily budget meter resets at local midnight.
    today_follows = count(
        "SELECT COUNT(*) FROM actions "
        "WHERE action = 'FOLLOW' AND created_at >= ?",
        (local_midnight_utc(db).isoformat(),),
    )
    owner = db.get_owner()

    return {
        "totals": {
            "total": total,
            "scored": scored,
            "scored_positive": scored_positive,
            "queue": queue,
            "followed": followed,
            "followbacks": followbacks,
            "unfollowed_after_mutual": unfollowed,
            "deleted": deleted,
            "ml_positive": ml_positive,
        },
        "today_follows": today_follows,
        "daily_limit": daily_limit,
        "owner": owner,
        "followers_count": (
            conn.execute(
                "SELECT followers_count FROM users WHERE username = ?",
                (owner,),
            ).fetchone()[0]
            if owner
            else None
        ),
    }


def activity_timeline(db, days=30):
    """Follows per day for the last *days* days (including today).

    ``days=1`` yields a single point for today, so the Overview interval
    toggle (today / 3 / 7 / 15 / 30 days) maps to exactly that many
    daily buckets.  Buckets are the **local** calendar days (user's
    timezone), so each point aggregates the follows that happened on that
    local day.
    """
    conn = db.conn
    tz = get_timezone(db)
    since = _window_since(db, days)
    rows = conn.execute(
        "SELECT created_at FROM actions "
        "WHERE action = 'FOLLOW' AND created_at >= ?",
        (since,),
    ).fetchall()

    # Bucket by local calendar day (robust across DST changes).
    by_day = {}
    for (created_at,) in rows:
        day = parse_utc(created_at).astimezone(tz).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    # Fill missing days with zeroes so the chart is continuous.
    out = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now(tz) - timedelta(days=i)).date().isoformat()
        out.append({"day": day, "count": by_day.get(day, 0)})
    return out


def followers_history(db, days=FOLLOWERS_HISTORY_DAYS):
    """Daily snapshot history for the owner (for the followers chart).

    Each point carries the day's followers / following / public repos
    counts from the ``user_snapshots`` table.  The daily snapshot records
    followers **and** following together (at the user's local midnight —
    ``SNAPSHOT_HOUR``), so both series always advance in lockstep and
    the point labels are the user's local dates.

    No "live" point is appended for today: the chart is intentionally a
    pure daily-at-midnight series, so the two lines never drift apart
    (one metric fresh, the other stale).  The live follower count is
    still available on the KPI card via ``overview_stats.followers_count``.
    """
    owner = db.get_owner()
    if not owner:
        return []

    since = (datetime.now(get_timezone(db)) - timedelta(days=days)).date().isoformat()
    return [
        {
            "date": s["date"],
            "followers": s["followers_count"] or 0,
            "following": s["following_count"] or 0,
            "public_repos": s["public_repos_count"] or 0,
        }
        for s in db.get_user_snapshots(owner, since_date=since)
    ]


def status_distribution(db, days=None):
    """Count of users per lifecycle status for the selected window.

    With *days* given, each column counts distinct users who *entered*
    that status within the window, using the ``user_status_history``
    transition log (exact ``changed_at`` timestamps recorded going
    forward; backfilled for historical data).  This is the same per-stage
    timestamp semantics as the KPI cards and replaces the older
    ``followed_at``-based approximations.

    With *days* None returns the current lifecycle state of all users.
    """
    if days is not None:
        since = _window_since(db, days)
        rows = db.conn.execute(
            """
            SELECT h.status, COUNT(DISTINCT h.username)
            FROM user_status_history h
            JOIN users u ON u.username = h.username
            WHERE u.owner = 0
              AND h.status IN ('FOLLOWED', 'FOLLOWBACK',
                               'UNFOLLOWED_AFTER_MUTUAL_FOLLOW', 'DELETED')
              AND h.changed_at >= ?
            GROUP BY h.status
            """,
            (since,),
        ).fetchall()
        d = {r[0]: r[1] for r in rows}
        return [
            {"status": "FOLLOWED", "count": d.get("FOLLOWED", 0)},
            {"status": "FOLLOWBACK", "count": d.get("FOLLOWBACK", 0)},
            {
                "status": "UNFOLLOWED_AFTER_MUTUAL_FOLLOW",
                "count": d.get("UNFOLLOWED_AFTER_MUTUAL_FOLLOW", 0),
            },
            {"status": "DELETED", "count": d.get("DELETED", 0)},
        ]

    # Current lifecycle state: group the narrow materialised table and
    # exclude the owner (a single row) — avoids scanning the wide users
    # table just to carry the owner flag.
    rows = db.conn.execute(
        """SELECT COALESCE(c.status, 'NEW'), COUNT(*)
           FROM user_current_status c
           WHERE c.username NOT IN (SELECT username FROM users WHERE owner = 1)
           GROUP BY 1"""
    ).fetchall()
    return [{"status": r[0] or "UNKNOWN", "count": r[1]} for r in rows]


def score_buckets(db, days=None):
    """Count of scored users per 20-point bucket.

    When *days* is given, only users scored within the last *days* days
    are counted.
    """
    sql = """
        SELECT CASE
            WHEN score >= 80 THEN '80-100'
            WHEN score >= 60 THEN '60-79'
            WHEN score >= 40 THEN '40-59'
            WHEN score >= 20 THEN '20-39'
            WHEN score > 0 THEN '1-19'
            ELSE 'unscored'
        END AS bucket, COUNT(*)
        FROM users WHERE owner = 0
    """
    params = ()
    if days is not None:
        sql += " AND scored_at >= ?"
        params = (_window_since(db, days),)
    sql += " GROUP BY bucket"
    rows = db.conn.execute(sql, params).fetchall()
    order = ["1-19", "20-39", "40-59", "60-79", "80-100"]
    d = {r[0]: r[1] for r in rows}
    return [{"bucket": b, "count": d.get(b, 0)} for b in order]


def total_actions(db, days=None):
    """Count of all lifecycle events (FOLLOW/FOLLOWBACK/UNFOLLOWED/DELETED)
    within the last *days* days.  None = all-time."""
    sql = "SELECT COUNT(*) FROM actions"
    params = ()
    if days is not None:
        sql += " WHERE created_at >= ?"
        params = (_window_since(db, days),)
    return db.conn.execute(sql, params).fetchone()[0]


def recent_actions(db, limit=12, days=None):
    """Most recent actions for the activity feed.

    Covers the full event stream (FOLLOW / FOLLOWBACK / UNFOLLOWED /
    DELETED).  When *days* is given, only events from the last *days*
    days are returned — this is what the Overview interval toggle uses.
    """
    sql = "SELECT username, action, created_at FROM actions"
    params = []
    if days is not None:
        sql += " WHERE created_at >= ?"
        params.append(_window_since(db, days))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = db.conn.execute(sql, params).fetchall()
    return [
        {"username": r[0], "action": r[1], "created_at": r[2]}
        for r in rows
    ]


# ── Users list ────────────────────────────────────────────────────────────

def list_users(db, q=None, status=None, language=None, ml=None,
               min_score=None, sort="score", order="desc",
               page=1, per_page=25):
    """Paginated user list with filters.

    ``ml`` may be ``None`` (no filter), 0, 1, or "none" (users with a
    NULL prediction).  Returns ``(items, total)``.  Each item carries
    the user's top-3 languages for the language dots in the table.
    """
    conn = db.conn
    # Current status comes from the materialised user_current_status table;
    # followed_at is its maintained FOLLOWED-transition time.  Filters are
    # written against the narrow current_status table (NOT IN / IN subqueries)
    # so the wide users rows are never scanned for the join.
    where = [
        "u.owner = 0",
        "u.username NOT IN ("
        "    SELECT username FROM user_current_status WHERE status = 'DELETED'"
        ")",
    ]
    params = []

    if q:
        where.append("(u.username LIKE ? OR u.bio LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if status:
        where.append(
            "u.username IN ("
            "    SELECT username FROM user_current_status WHERE status = ?"
            ")"
        )
        params.append(status)
    if min_score is not None:
        where.append("u.score >= ?")
        params.append(min_score)
    if ml is not None:
        if ml == "none":
            where.append("u.ml_follow_prediction IS NULL")
        else:
            where.append("u.ml_follow_prediction = ?")
            params.append(1 if ml else 0)
    if language:
        where.append(
            """
            EXISTS (
                SELECT 1
                FROM repository_languages rl
                JOIN repositories r  ON r.id = rl.repository_id
                JOIN languages     l  ON l.id = rl.language_id
                WHERE r.user_id = u.username AND l.name = ?
            )
            """
        )
        params.append(language)

    where_sql = " AND ".join(where)

    sort_cols = {
        "score": "u.score",
        "followers": "u.followers",
        "repos": "u.public_repos",
        "name": "u.username",
        "created_at": "u.created_at",
    }
    # followed_at lives in the materialised user_current_status table.
    # Driving the query from that narrow table lets the planner use its
    # index instead of sorting the joined wide users rows.  INDEXED BY
    # forces that plan on newer SQLite versions too, where the planner
    # otherwise prefers scanning users by owner and sorting 90k rows in a
    # temp b-tree (~450ms vs <1ms).  (Every user has a current_status row
    # — backfilled by migration 023 and maintained on every transition —
    # so the inner join is complete.)
    if sort == "followed_at":
        from_sql = (
            "FROM user_current_status c "
            "INDEXED BY idx_current_status_followed_at "
            "JOIN users u ON u.username = c.username"
        )
        sort_sql = "c.followed_at"
        direction = "DESC NULLS LAST" if order == "desc" else "ASC NULLS FIRST"
    else:
        from_sql = (
            "FROM users u "
            "LEFT JOIN user_current_status c ON c.username = u.username"
        )
        sort_sql = sort_cols.get(sort, "u.score")
        direction = "DESC" if order == "desc" else "ASC"

    total = conn.execute(
        f"""SELECT COUNT(*) FROM users u
            WHERE {where_sql}""",
        params,
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"""
        SELECT u.username, u.score, u.followers, u.public_repos,
               COALESCE(c.status, 'NEW') AS status,
               u.bio, u.company, u.created_at,
               c.followed_at,
               u.scored_at, u.ml_follow_prediction, u.github_profile_json
        {from_sql}
        WHERE {where_sql}
        ORDER BY {sort_sql} {direction}
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    ).fetchall()

    items = [
        {
            "username": r[0],
            "score": r[1],
            "followers": r[2],
            "public_repos": r[3],
            "status": r[4],
            "bio": r[5],
            "company": r[6],
            "created_at": r[7],
            "followed_at": r[8],
            "scored_at": r[9],
            "ml_follow_prediction": r[10],
            "avatar_url": _avatar(r[11], r[0]),
        }
        for r in rows
    ]

    # Top languages for the visible page — one query for all users.
    usernames = [i["username"] for i in items]
    if usernames:
        placeholders = ",".join("?" * len(usernames))
        lang_rows = conn.execute(
            f"""
            SELECT r.user_id, l.name
            FROM repository_languages rl
            JOIN repositories r ON r.id = rl.repository_id
            JOIN languages     l ON l.id = rl.language_id
            WHERE r.user_id IN ({placeholders})
            GROUP BY r.user_id, l.id
            ORDER BY r.user_id, SUM(rl.weight) DESC
            """,
            usernames,
        ).fetchall()
        langs = {}
        for user_id, name in lang_rows:
            langs.setdefault(user_id, []).append(name)
        for item in items:
            item["top_languages"] = (langs.get(item["username"]) or [])[:3]
    else:
        for item in items:
            item["top_languages"] = []

    return items, total


def _avatar(profile_json, username):
    if profile_json:
        try:
            data = json.loads(profile_json)
            if data.get("avatar_url"):
                return data["avatar_url"]
        except (json.JSONDecodeError, TypeError):
            pass
    return f"https://github.com/{username}.png"


# ── User profile ──────────────────────────────────────────────────────────

def user_profile(db, username):
    """Full profile for one user (profile drawer)."""
    row = db.conn.execute(
        """
        SELECT u.username, u.score, u.public_repos, u.followers, u.bio, u.company,
               COALESCE(c.status, 'NEW') AS status,
               u.created_at, u.scored_at,
               c.followed_at,
               u.discovered_from, u.repos_fetched_at, u.followers_count,
               u.ml_follow_prediction, u.github_profile_json
        FROM users u
        LEFT JOIN user_current_status c ON c.username = u.username
        WHERE u.username = ?
        """,
        (username,),
    ).fetchone()
    if not row:
        return None

    profile = {
        "username": row[0],
        "score": row[1],
        "public_repos": row[2],
        "followers": row[3],
        "bio": row[4],
        "company": row[5],
        "status": row[6],
        "created_at": row[7],
        "scored_at": row[8],
        "followed_at": row[9],
        "discovered_from": row[10],
        "repos_fetched_at": row[11],
        "followers_count": row[12],
        "ml_follow_prediction": row[13],
        "avatar_url": _avatar(row[14], username),
    }

    # Extra fields from the cached GitHub profile JSON
    if row[14]:
        try:
            extra = json.loads(row[14])
            for key in (
                "name", "location", "blog", "twitter_username", "email",
                "hireable", "type", "following", "public_gists", "created_at",
            ):
                if key in extra:
                    profile[key] = extra[key]
        except (json.JSONDecodeError, TypeError):
            pass

    profile["languages"] = db.user_languages(username)
    profile["topics"] = db.user_topics(username)
    profile["companies"] = [
        {"login": c[0], "name": c[1], "type": c[2]}
        for c in db.get_user_companies(username)
    ]

    # Top repos by stars
    repos = db.conn.execute(
        """
        SELECT name, description, html_url, stars, forks, watchers,
               is_fork, is_archived, repository_updated_at
        FROM repositories
        WHERE user_id = ?
        ORDER BY stars DESC
        LIMIT 12
        """,
        (username,),
    ).fetchall()
    profile["repos"] = [
        {
            "name": r[0],
            "description": r[1],
            "html_url": r[2],
            "stars": r[3],
            "forks": r[4],
            "watchers": r[5],
            "is_fork": bool(r[6]),
            "is_archived": bool(r[7]),
            "updated_at": r[8],
        }
        for r in repos
    ]

    return profile


def language_options(db, limit=50):
    """Distinct languages with user counts (for the filter dropdown).

    The inner DISTINCT over (user, language) pairs makes the per-language
    count a plain COUNT(*) — the planner no longer needs a COUNT(DISTINCT)
    temp b-tree over the full 300k+ join (~2.5x faster on the live data).
    """
    rows = db.conn.execute(
        """
        SELECT l.name, COUNT(*) AS users
        FROM (
            SELECT DISTINCT r.user_id, rl.language_id
            FROM repository_languages rl
            JOIN repositories r ON r.id = rl.repository_id
        ) x
        JOIN languages l ON l.id = x.language_id
        GROUP BY l.name
        ORDER BY users DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [{"name": r[0], "users": r[1]} for r in rows]


# ── ML dashboard tab ──────────────────────────────────────────────────────

def _current_model_metadata():
    """Read the deployed model's metadata JSON (no torch involved).

    ``current.json`` is the live pointer; fall back to the newest
    versioned ``model_NNN.json`` when it is missing or unreadable.
    Returns None when no model has been trained yet.
    """
    model_dir = os.path.abspath(ML_MODEL_DIR)
    path = os.path.join(model_dir, "current.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    if os.path.isdir(model_dir):
        try:
            files = sorted(
                f for f in os.listdir(model_dir)
                if f.startswith("model_") and f.endswith(".json")
            )
        except OSError:
            files = []
        if files:
            try:
                with open(os.path.join(model_dir, files[-1]), "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
    return None


def ml_state(db):
    """Everything the ML tab needs: model, dataset, training-run history.

    * ``current_model`` — metadata of the deployed model (read from
      ``current.json`` — no torch involved), None when nothing has been
      trained yet.
    * ``dataset`` — live counts: how many users carry a prediction and how
      it is distributed, plus the size of the training sample pool (the
      same users ``get_training_users`` would label).
    * ``history`` — one row per retrain from ``ml_training_runs`` (recorded
      going forward since migration 024, backfilled for older models),
      including each model's metrics and the population distribution its
      follow-up recompute produced.
    """
    conn = db.conn

    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    total_users = count("SELECT COUNT(*) FROM users WHERE owner = 0")
    with_pred = count(
        "SELECT COUNT(*) FROM users WHERE ml_follow_prediction IS NOT NULL"
    )
    pred_1 = count("SELECT COUNT(*) FROM users WHERE ml_follow_prediction = 1")
    pred_0 = count("SELECT COUNT(*) FROM users WHERE ml_follow_prediction = 0")

    training = db.get_training_users()
    n_pos = sum(1 for _, lbl in training if lbl == 1)
    n_neg = len(training) - n_pos

    return {
        "current_model": _current_model_metadata(),
        "dataset": {
            "total_users": total_users,
            "with_prediction": with_pred,
            "pred_1": pred_1,
            "pred_0": pred_0,
            "positive_share_pct": (
                round(pred_1 / with_pred * 100, 1) if with_pred else 0.0
            ),
            "training_samples": len(training),
            "training_positives": n_pos,
            "training_negatives": n_neg,
            "training_positive_share_pct": round(
                n_pos / max(len(training), 1) * 100, 1
            ),
        },
        "history": db.ml_training_runs(limit=100),
    }
