import requests

from core.config import API, HEADERS
from core.logger import get_logger

log = get_logger(__name__)


def record_api_request(endpoint, status_code=None):
    """Persist one GitHub API round-trip for the dashboard's per-hour meter.

    Called on every real HTTP call to api.github.com (including 304s), so
    the Overview card can show a live requests-per-hour figure.  Best
    effort only — metering must never break a collection run, so all
    failures are swallowed.

    A fresh short-lived connection is used per call: the bot already paces
    itself (>= 1 s between requests), so the overhead is negligible and it
    is naturally thread-safe across the worker threads.
    """
    try:
        from core.database import Database

        db = Database()
        try:
            db.conn.execute("PRAGMA busy_timeout=5000")
            db.record_api_request(endpoint, status_code)
        finally:
            db.conn.close()
    except Exception:
        # Metering is best-effort; never let it crash the bot.
        log.debug("Failed to record API request %s", endpoint, exc_info=True)

# Default timeout for all GitHub API requests (seconds).
_REQUEST_TIMEOUT = 30

# ── Tuning for Retry-After respect ───────────────────────────────────────
# GitHub sometimes tells us to wait a few seconds, sometimes hours.
# We honour the hint but clamp so a single delay never blows past 15 min
# (any longer should fall through to the cooldown cycle instead).
RETRY_AFTER_MIN = 5
RETRY_AFTER_MAX = 15 * 60

# If the server's Retry-After hint exceeds this we bypass the retry loop
# and go straight to cooldown — retries would just hammer a still-blocked
# endpoint and waste quota to no effect.
LONG_HINT_THRESHOLD = 30 * 60

# Legacy retry schedule (used as fallback when GitHub sends no Retry-After).
# The actual first delay is derived from headers via first_wait_from_headers.
FALLBACK_RETRY_DELAYS = (5 * 60, 10 * 60, 15 * 60)


def _parse_int(value):
    """Safely parse an integer HTTP header (return None if missing/invalid)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_limit(exc):
    """Heuristic verdict: 'primary' (5000/hr) vs 'secondary' (abuse).

    Primary exhaustion: standard headers + Remaining=0 + no Retry-After.
    Secondary limit: usually carries Retry-After and lacks X-RateLimit-* .
    Resource denial: 403 with Remaining>0 and no Retry-After — not a rate
    limit at all, just GitHub refusing that specific endpoint/resource.
    """
    if getattr(exc, "retry_after", None) is not None:
        return "secondary (abuse)"
    if getattr(exc, "remaining", None) == 0 and getattr(exc, "reset_at", None):
        return "primary (5000/hr exhausted)"
    if getattr(exc, "remaining", None) is not None:
        return "primary partial"
    return "secondary (abuse, no headers)"


def first_wait_from_headers(exc, fallback_seconds=None):
    """Pick an initial retry delay (seconds) from server hints.

    Priority:
      1. ``Retry-After``  (seconds)
      2. ``X-RateLimit-Reset`` (unix ts → seconds-from-now)
      3. ``fallback_seconds``  (default *5 min*)
    Result is clamped to ``[RETRY_AFTER_MIN, RETRY_AFTER_MAX]``.
    """
    import time

    if fallback_seconds is None:
        fallback_seconds = 5 * 60

    raw = None
    if getattr(exc, "retry_after", None) is not None:
        raw = exc.retry_after
    elif getattr(exc, "reset_at", None) is not None:
        raw = exc.reset_at - int(time.time())

    if raw is None or raw <= 0:
        raw = fallback_seconds

    return max(RETRY_AFTER_MIN, min(int(raw), RETRY_AFTER_MAX))


def should_fetch_languages(repo,
                            fork_threshold_kb,
                            max_threshold_kb,
                            read_heavy_forks,
                            skip_forks=False):
    """Decide whether the /languages call is allowed for *repo*.

    Falls back to True when ``read_heavy_forks`` is True (legacy mode).
    Otherwise:
      - any fork when ``skip_forks`` is True               → False
      - any empty repo (size 0 KB)                         → False
      - any repo larger than ``max_threshold_kb``          → False
      - any fork larger than ``fork_threshold_kb``         → False
      - everything else                                     → True

    *repo* is the dict returned by ``GET /users/{login}/repos``.
    The ``size`` field from GitHub is in **KB**, and ``fork`` is a bool.
    Boundary equality (``size == threshold``) still permits the fetch
    (the comparison is strict ``>``).

    ``skip_forks`` (documentation/API_using.md §7.2): when True, /languages is never
    fetched for forked repos — a fork mirrors its upstream, so the
    language breakdown carries little signal for similarity scoring.
    """
    if read_heavy_forks:
        return True

    size_kb = repo.get("size", 0) or 0
    is_fork = bool(repo.get("fork", False))

    if skip_forks and is_fork:
        return False

    # Empty repos have no content — GitHub always returns 403 for /languages
    if size_kb == 0:
        return False

    if size_kb > max_threshold_kb:
        return False
    if is_fork and size_kb > fork_threshold_kb:
        return False
    return True


# ── Exceptions ────────────────────────────────────────────────────────────


class GitHubNetworkError(Exception):
    """Raised on transport-level errors (timeout, connection reset, DNS, etc.).

    These are transient — unlike auth errors, they should be retried
    with back-off rather than aborting the run.
    Carries the original exception for logging context.
    """

    def __init__(self, url, original_exception=None):
        self.url = url
        self.original = original_exception
        super().__init__(f"Network error for {url}: {original_exception}")


class GitHubNotModified(Exception):
    """Raised on HTTP 304 — resource unchanged since the last fetch.

    GitHub supports conditional requests: if the client sends an
    ``If-None-Match`` header with the previously received ``ETag`` and the
    resource has not changed, GitHub answers 304 **without counting the
    request against the rate limit**.  Callers should treat this as
    "use the cached copy" — it is a free request.
    """

    def __init__(self, url):
        self.url = url
        super().__init__(f"GitHub 304 (not modified) for {url}")


class GitHubAuthError(Exception):
    """Raised on HTTP 401 — token revoked/expired.  Retrying won't help.

    Distinct from :class:`GitHubRateLimitError` because auth failures
    demand an immediate abort + alert rather than a cooldown cycle.
    """

    def __init__(self, status_code, url, response=None):
        self.status_code = status_code
        self.url = url
        self.response = response
        super().__init__(f"GitHub {status_code} (auth failure) for {url}")


class GitHubRateLimitError(Exception):
    """Raised on HTTP 403 / 429 — primary or secondary rate limit hit.

    Carries the parsed ``Retry-After`` / ``X-RateLimit-Reset`` /
    ``X-RateLimit-Remaining`` headers so callers can drive their retry
    interval from the server's hint instead of a hard-coded schedule.
    """

    def __init__(self, status_code, url, response=None,
                 retry_after=None, reset_at=None, remaining=None):
        self.status_code = status_code
        self.url = url
        self.response = response
        self.retry_after = retry_after      # seconds (Retry-After) or None
        self.reset_at = reset_at            # unix ts (X-RateLimit-Reset) or None
        self.remaining = remaining          # int (X-RateLimit-Remaining) or None
        # Provisional verdict — derived in classify_limit() so callers can
        # log/branch on it without needing to know header semantics.
        self.verdict = classify_limit(self)
        retry_str = f"{retry_after}s" if retry_after is not None else "None"
        super().__init__(
            f"GitHub {status_code} for {url} ({self.verdict}; "
            f"Retry-After={retry_str} Reset={reset_at} Remaining={remaining})"
        )


class GithubClient:
    """Wrapper around the GitHub REST API."""

    def __init__(self):
        # ETag of the most recent response, captured so callers can persist
        # it and send it back as If-None-Match on the next round (304 → free).
        self.last_etag = None

    # --------------------------------------------------
    # Low-level request helper
    # --------------------------------------------------

    def request(self, method, url, etag=None, **kwargs):
        """Perform a request; optionally conditional via ``If-None-Match``.

        Parameters
        ----------
        etag : str, optional
            Previously received ETag.  When provided, sends
            ``If-None-Match: <etag>``.  If the resource is unchanged,
            GitHub answers 304 and :class:`GitHubNotModified` is raised
            (the request is NOT counted against the rate limit).

        On any successful (non-304) response, ``self.last_etag`` is set to
        the response's ``ETag`` header (may be None if GitHub omits it).
        """
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT)

        headers = dict(HEADERS)
        if etag:
            headers["If-None-Match"] = etag

        try:
            r = requests.request(
                method,
                API + url,
                headers=headers,
                **kwargs,
            )
        except requests.exceptions.RequestException as e:
            log.warning("Network error on %s %s: %s", method, url, e)
            raise GitHubNetworkError(url, original_exception=e) from e

        # Capture the ETag BEFORE any early returns, so callers can persist
        # it for the next conditional request.
        self.last_etag = r.headers.get("ETag")

        # Metering — every response (200/304/403/...) counts as an API call.
        record_api_request(url, r.status_code)

        # ── 304 → not modified (free request, use cached copy) ──
        if r.status_code == 304:
            log.debug("GitHub 304 (not modified) for %s — free request", url)
            raise GitHubNotModified(url)

        # ── 401 → fatal auth error (don't retry) ──
        if r.status_code == 401:
            log.error(
                "GitHub 401 (auth failure) for %s — token invalid/expired, aborting.",
                url,
            )
            raise GitHubAuthError(401, url, r)

        # ── 403 / 429 → rate limit; capture hint headers for retry logic ──
        if r.status_code in (403, 429):
            retry_after = _parse_int(r.headers.get("Retry-After"))
            reset_at = _parse_int(r.headers.get("X-RateLimit-Reset"))
            remaining = _parse_int(r.headers.get("X-RateLimit-Remaining"))

            # 403 with remaining > 0 and no Retry-After is NOT a rate limit —
            # GitHub is simply refusing this specific resource (e.g. /languages
            # on a repo too large to compute).  Retrying is pointless.
            if (
                r.status_code == 403
                and remaining is not None
                and remaining > 0
                and retry_after is None
            ):
                log.warning(
                    "GitHub 403 (resource denied) for %s — "
                    "Remaining=%s, not a rate limit; skipping.",
                    url, remaining,
                )
                return None

            exc = GitHubRateLimitError(
                r.status_code, url, r,
                retry_after=retry_after,
                reset_at=reset_at,
                remaining=remaining,
            )
            retry_str = f"{retry_after}s" if retry_after is not None else "None"
            log.warning(
                "%s — Retry-After=%s Reset=%s Remaining=%s",
                exc, retry_str, reset_at, remaining,
            )
            raise exc

        if r.status_code == 404:
            log.warning("GitHub 404 (not found) for %s", url)
            return None

        if r.status_code >= 400:
            log.error("GitHub HTTP %s for %s", r.status_code, url)
            return None

        if r.text:
            return r.json()

        return True

    # --------------------------------------------------
    # Public helpers
    # --------------------------------------------------

    def followers(self, username):
        """Return a list of all followers for *username* (paginated)."""
        result = []
        page = 1

        while True:
            data = self.request(
                "GET",
                f"/users/{username}/followers",
                params={"per_page": 100, "page": page},
            )

            if not data:
                break

            result.extend(data)
            page += 1

        return result

    def user(self, username):
        """Return the public profile for *username*."""
        return self.request("GET", f"/users/{username}")

    def follow(self, username):
        """Follow *username*. Returns True on success."""
        try:
            r = requests.put(
                f"{API}/user/following/{username}",
                headers=HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            record_api_request(f"/user/following/{username}", r.status_code)
            return r.status_code == 204
        except requests.exceptions.RequestException as e:
            log.warning("Network error following %s: %s", username, e)
            raise GitHubNetworkError(
                f"/user/following/{username}", original_exception=e
            ) from e

    def already_following(self, username):
        """Check whether we already follow *username*."""
        try:
            r = requests.get(
                f"{API}/user/following/{username}",
                headers=HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            record_api_request(f"/user/following/{username}", r.status_code)
            return r.status_code == 204
        except requests.exceptions.RequestException as e:
            log.warning("Network error checking follow for %s: %s", username, e)
            raise GitHubNetworkError(
                f"/user/following/{username}", original_exception=e
            ) from e

    def does_user_follow_us(self, username, my_username):
        """Check whether *username* follows *my_username*.

        ``GET /users/{username}/following/{my_username}``
        returns 204 (yes) or 404 (no).
        """
        try:
            r = requests.get(
                f"{API}/users/{username}/following/{my_username}",
                headers=HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            record_api_request(
                f"/users/{username}/following/{my_username}", r.status_code
            )
            return r.status_code == 204
        except requests.exceptions.RequestException as e:
            log.warning(
                "Network error checking if %s follows %s: %s",
                username, my_username, e,
            )
            raise GitHubNetworkError(
                f"/users/{username}/following/{my_username}",
                original_exception=e,
            ) from e

    # --------------------------------------------------
    # Repositories & languages
    # --------------------------------------------------

    def repos(self, username, etag=None):
        """Return all public repos for *username* (paginated).

        When *etag* is provided and the repo list is unchanged, raises
        :class:`GitHubNotModified` (free request — caller should use the
        cached copy).
        """
        result = []
        page = 1
        first_page_etag = None

        while True:
            data = self.request(
                "GET",
                f"/users/{username}/repos",
                etag=etag if page == 1 else None,
                params={
                    "per_page": 100,
                    "page": page,
                    "type": "public",
                    "sort": "updated",
                },
            )

            if page == 1:
                first_page_etag = self.last_etag

            if not data:
                break

            result.extend(data)
            if len(data) < 100:
                break
            page += 1

        # Restore the page-1 ETag (subsequent pages may have overwritten it)
        # so callers persist the ETag that represents the whole collection.
        self.last_etag = first_page_etag
        return result

    def repo_languages(self, owner, repo, etag=None):
        """Return language breakdown for a repository.

        Returns a dict like {"Java": 150000, "Python": 50000}.

        When *etag* is provided and the languages are unchanged, raises
        :class:`GitHubNotModified` (free request — caller should keep the
        cached language data).
        """
        return self.request("GET", f"/repos/{owner}/{repo}/languages", etag=etag) or {}
