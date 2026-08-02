"""Read-only dashboard queries over the bot's SQLite database.

All functions take a ``Database`` instance (from ``database.py``) and
return plain dicts/lists ready to be JSON-serialised by the API layer.
"""

import json
from datetime import datetime, timedelta, timezone

from core.config import CURRENT_SCORE_VERSION, GITHUB_API_RATE_LIMIT


def github_api_usage(db, hours=1):
    """Rolling GitHub API request usage for the overview card.

    Counts every real round-trip the bot made to api.github.com (recorded
    by github_client.record_api_request), so the card shows the *actual*
    request rate over a rolling ``hours``-hour window — not a static
    counter.  Also returns today's total and the share of GitHub's primary
    hourly quota (GITHUB_API_RATE_LIMIT) that the window consumed.
    """
    conn = db.conn
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def _count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    rolling = _count(
        "SELECT COUNT(*) FROM github_api_requests WHERE created_at >= ?",
        (since,),
    )
    today = _count(
        "SELECT COUNT(*) FROM github_api_requests "
        "WHERE created_at >= ?",
        (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
    )
    total = _count("SELECT COUNT(*) FROM github_api_requests")

    percent = round((rolling / GITHUB_API_RATE_LIMIT) * 100, 1) if GITHUB_API_RATE_LIMIT else 0.0
    return {
        "requests_last_hour": rolling,
        "requests_today": today,
        "requests_total": total,
        "rate_limit": GITHUB_API_RATE_LIMIT,
        "percent_last_hour": percent,
    }


# ── Overview / stats ──────────────────────────────────────────────────────

def overview_stats(db, daily_limit):
    """KPI numbers for the overview screen."""
    conn = db.conn

    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    total = count(
        "SELECT COUNT(*) FROM users WHERE owner = 0 AND status != 'DELETED'"
    )
    # "Scored" = users who actually went through scoring with the current
    # algorithm version.  `score > 0` is NOT a good proxy: the scorer
    # legitimately assigns 0 to most low-quality accounts, so counting only
    # positive scores understated real scoring progress (2% of the pipeline
    # instead of the actual ~77%).
    scored = count(
        "SELECT COUNT(*) FROM users "
        "WHERE owner = 0 AND status != 'DELETED' "
        "AND repos_fetched_at IS NOT NULL "
        "AND scored_at IS NOT NULL AND score_version = ?",
        (CURRENT_SCORE_VERSION,),
    )
    # Users whose score is actually above zero (follow-worthy).
    scored_positive = count(
        "SELECT COUNT(*) FROM users "
        "WHERE owner = 0 AND status != 'DELETED' "
        "AND repos_fetched_at IS NOT NULL AND score > 0"
    )
    followed = count("SELECT COUNT(*) FROM users WHERE status = 'FOLLOWED'")
    followbacks = count("SELECT COUNT(*) FROM users WHERE status = 'FOLLOWBACK'")
    unfollowed = count(
        "SELECT COUNT(*) FROM users WHERE status = 'UNFOLLOWED_AFTER_MUTUAL_FOLLOW'"
    )
    # Real processing queue: users still awaiting repo collection / fresh
    # scoring (same eligibility the silent runner uses).  `status = 'NEW'`
    # is not used here — it also counts already-processed users.
    queue = db.count_silent_processing_queue()
    deleted = count("SELECT COUNT(*) FROM users WHERE status = 'DELETED'")
    ml_positive = count(
        "SELECT COUNT(*) FROM users WHERE ml_follow_prediction = 1"
    )
    today_follows = count(
        "SELECT COUNT(*) FROM actions "
        "WHERE action = 'FOLLOW' AND date(created_at) = date('now')"
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
    """Follows per day for the last N days (for the chart)."""
    conn = db.conn
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT date(created_at) AS day, COUNT(*) AS cnt
        FROM actions
        WHERE action = 'FOLLOW' AND created_at >= ?
        GROUP BY day
        ORDER BY day
        """,
        (since,),
    ).fetchall()

    # Fill missing days with zeroes so the chart is continuous.
    by_day = {r[0]: r[1] for r in rows}
    out = []
    for i in range(days, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
        out.append({"day": day, "count": by_day.get(day, 0)})
    return out


def followers_history(db, days=30):
    """Daily snapshot history for the owner (for the followers chart).

    Each point carries the day's followers / following / public repos
    counts from the ``user_snapshots`` table.  If today has no snapshot
    yet (the noon worker has not run), the latest stored follower count
    is appended as today's point so the chart always shows current data.
    """
    owner = db.get_owner()
    if not owner:
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    points = [
        {
            "date": s["date"],
            "followers": s["followers_count"] or 0,
            "following": s["following_count"] or 0,
            "public_repos": s["public_repos_count"] or 0,
        }
        for s in db.get_user_snapshots(owner, since_date=since)
    ]

    # Append the live follower count for today if no snapshot exists yet.
    today = datetime.now(timezone.utc).date().isoformat()
    if not points or points[-1]["date"] != today:
        live = db.conn.execute(
            "SELECT followers_count FROM users WHERE username = ?", (owner,)
        ).fetchone()
        if live and live[0] is not None:
            points.append(
                {"date": today, "followers": live[0], "following": None, "public_repos": None}
            )
    return points


def status_distribution(db):
    """Count of users per status."""
    rows = db.conn.execute(
        "SELECT status, COUNT(*) FROM users WHERE owner = 0 GROUP BY status"
    ).fetchall()
    return [{"status": r[0] or "UNKNOWN", "count": r[1]} for r in rows]


def score_buckets(db):
    """Count of scored users per 20-point bucket."""
    rows = db.conn.execute(
        """
        SELECT CASE
            WHEN score >= 80 THEN '80-100'
            WHEN score >= 60 THEN '60-79'
            WHEN score >= 40 THEN '40-59'
            WHEN score >= 20 THEN '20-39'
            WHEN score > 0 THEN '1-19'
            ELSE 'unscored'
        END AS bucket, COUNT(*)
        FROM users WHERE owner = 0
        GROUP BY bucket
        """
    ).fetchall()
    order = ["1-19", "20-39", "40-59", "60-79", "80-100"]
    d = {r[0]: r[1] for r in rows}
    return [{"bucket": b, "count": d.get(b, 0)} for b in order]


def recent_actions(db, limit=12):
    """Most recent actions (FOLLOW / etc.) for the activity feed."""
    rows = db.conn.execute(
        """
        SELECT username, action, created_at
        FROM actions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
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
    where = ["owner = 0", "status != 'DELETED'"]
    params = []

    if q:
        where.append("(username LIKE ? OR bio LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if status:
        where.append("status = ?")
        params.append(status)
    if min_score is not None:
        where.append("score >= ?")
        params.append(min_score)
    if ml is not None:
        if ml == "none":
            where.append("ml_follow_prediction IS NULL")
        else:
            where.append("ml_follow_prediction = ?")
            params.append(1 if ml else 0)
    if language:
        where.append(
            """
            EXISTS (
                SELECT 1
                FROM repository_languages rl
                JOIN repositories r  ON r.id = rl.repository_id
                JOIN languages     l  ON l.id = rl.language_id
                WHERE r.user_id = users.username AND l.name = ?
            )
            """
        )
        params.append(language)

    where_sql = " AND ".join(where)

    sort_cols = {
        "score": "score",
        "followers": "followers",
        "repos": "public_repos",
        "name": "username",
        "followed_at": "followed_at",
        "created_at": "created_at",
    }
    sort_sql = sort_cols.get(sort, "score")
    direction = "DESC" if order == "desc" else "ASC"
    # NULLS sort last for followed_at (many users are never followed)
    if sort == "followed_at":
        direction = "DESC NULLS LAST" if order == "desc" else "ASC NULLS FIRST"

    total = conn.execute(
        f"SELECT COUNT(*) FROM users WHERE {where_sql}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"""
        SELECT username, score, followers, public_repos, status,
               bio, company, created_at, followed_at, scored_at,
               ml_follow_prediction, github_profile_json
        FROM users
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
        SELECT username, score, public_repos, followers, bio, company,
               status, created_at, scored_at, followed_at, discovered_from,
               repos_fetched_at, followers_count, ml_follow_prediction,
               github_profile_json
        FROM users WHERE username = ?
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
    """Distinct languages with user counts (for the filter dropdown)."""
    rows = db.conn.execute(
        """
        SELECT l.name, COUNT(DISTINCT r.user_id)
        FROM repository_languages rl
        JOIN repositories r ON r.id = rl.repository_id
        JOIN languages     l ON l.id = rl.language_id
        GROUP BY l.name
        ORDER BY 2 DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [{"name": r[0], "users": r[1]} for r in rows]
