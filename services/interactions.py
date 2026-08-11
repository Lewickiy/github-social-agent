"""On-demand check: has a user interacted with the owner's profile/repos?

The unfollow worker asks this question before unfollowing a user who has
been followed for a long time without following back: it keeps the user
only when they demonstrably interacted with the owner — starred, forked,
opened/closed issues or PRs, commented, reviewed, pushed, released, or
followed the owner.

The answer is a plain ``bool`` and is deliberately **not persisted**: the
user asked for a True/False decision only, so nothing is written to the
database here.

Source: ``GET /users/{username}/events/public`` — the user's public
timeline, which GitHub caps at ~300 events over a 90-day window (3 pages
of 100).  For the "never interacted" case this is exact (no events → no
interaction).  A user whose only interaction is *older* than the window
may be misjudged as inactive — a safe direction of error for an unfollow
decision (we only ever keep someone we shouldn't, never the reverse).

Cost: one request per user per check (the first 100-event page); early
exit on the first matching event.
"""

from core.logger import get_logger

log = get_logger(__name__)

# Event types that count as "touched the owner's repo".  FollowEvent is
# handled separately (its payload carries the *followed* user, not a repo).
_INTERACTION_EVENT_TYPES = frozenset({
    "WatchEvent",                 # ⭐ starred
    "ForkEvent",                  # 🍴 forked
    "PullRequestEvent",           # opened / closed / merged a PR
    "PullRequestReviewEvent",     # reviewed a PR
    "PullRequestReviewCommentEvent",  # commented on a PR review
    "IssuesEvent",                # opened / closed an issue
    "IssueCommentEvent",          # commented on an issue / PR
    "CommitCommentEvent",         # commented on a commit
    "PushEvent",                  # pushed commits
    "CreateEvent",                # created a branch / tag / repo
    "DeleteEvent",                # deleted a branch / tag
    "ReleaseEvent",               # published a release
})

# GitHub returns at most 300 events (90 days); 3 pages of 100 each.
_MAX_EVENT_PAGES = 3


def user_has_interacted_with_owner(github, username, owner, owner_repo_names=None):
    """Return True when *username* interacted with *owner* or their repos.

    *github*         — a ``GithubClient`` (its ``request`` raises
                       ``GitHubNetworkError`` / ``GitHubRateLimitError`` /
                       ``GitHubAuthError`` on failure — these propagate to
                       the caller).
    *username*       — the user to inspect.
    *owner*          — the owner's login (their profile + repos).
    *owner_repo_names* — optional iterable of the owner's repo *short*
                       names (from the repositories table).  When None or
                       empty, they are loaded from the database, and if
                       that is empty too, fetched fresh from the API.
    """
    owner_repo_full = _owner_repo_full_names(github, owner, owner_repo_names)
    if not owner_repo_full:
        # Owner has no public repos — only a profile interaction
        # (followed the owner) can count.
        log.info(
            "Owner %s has no known repos — profile-only interaction check",
            owner,
        )

    page = 1
    while page <= _MAX_EVENT_PAGES:
        data = github.request(
            "GET",
            f"/users/{username}/events/public",
            params={"per_page": 100, "page": page},
        )
        if not data:
            return False

        for event in data:
            if _event_matches(event, owner, owner_repo_full):
                return True
        page += 1

    return False


def _owner_repo_full_names(github, owner, owner_repo_names):
    """Full ``owner/repo`` names for the owner's repositories.

    Prefers the provided short names (database); falls back to a fresh
    ``/users/{owner}/repos`` fetch when nothing is known — an empty repo
    list must never be mistaken for "no interaction possible" (that would
    unfollow everyone).
    """
    short = [n for n in (owner_repo_names or []) if n]
    if not short:
        try:
            from core.database import Database

            db = Database()
            try:
                rows = db.conn.execute(
                    "SELECT name FROM repositories WHERE user_id = ?",
                    (owner,),
                ).fetchall()
                short = [r[0] for r in rows]
            finally:
                db.conn.close()
        except Exception:
            log.warning("Failed to read owner repos from DB", exc_info=True)

    if short:
        return {f"{owner}/{name}" for name in short}

    # Nothing in the DB — fetch the live list (rare: owner not synced yet).
    try:
        repos = github.repos(owner) or []
        return {r.get("full_name") for r in repos if r.get("full_name")}
    except Exception:
        log.warning("Failed to fetch owner repos from API", exc_info=True)
        return set()


def _event_matches(event, owner, owner_repo_full):
    """True when *event* shows interaction with *owner* / their repos."""
    etype = event.get("type")

    # FollowEvent carries the followed account in payload.target, not a repo.
    if etype == "FollowEvent":
        target = (event.get("payload") or {}).get("target") or {}
        return target.get("login") == owner

    if etype not in _INTERACTION_EVENT_TYPES:
        return False

    repo = event.get("repo") or {}
    return repo.get("name") in owner_repo_full
