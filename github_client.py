import requests

from config import API, HEADERS
from logger import get_logger

log = get_logger(__name__)

# Default timeout for all GitHub API requests (seconds).
_REQUEST_TIMEOUT = 30


class GitHubRateLimitError(Exception):
    """Raised when GitHub returns 401 or 403 (rate-limit / auth issue)."""

    def __init__(self, status_code, url, response=None):
        self.status_code = status_code
        self.url = url
        self.response = response
        super().__init__(f"GitHub {status_code} for {url}")


class GithubClient:
    """Wrapper around the GitHub REST API."""

    # --------------------------------------------------
    # Low-level request helper
    # --------------------------------------------------

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT)

        r = requests.request(
            method,
            API + url,
            headers=HEADERS,
            **kwargs,
        )

        if r.status_code in (401, 403):
            raise GitHubRateLimitError(r.status_code, url, r)

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
        r = requests.put(
            f"{API}/user/following/{username}",
            headers=HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        return r.status_code == 204

    def already_following(self, username):
        """Check whether we already follow *username*."""
        r = requests.get(
            f"{API}/user/following/{username}",
            headers=HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        return r.status_code == 204

    # --------------------------------------------------
    # Repositories & languages
    # --------------------------------------------------

    def repos(self, username):
        """Return all public repos for *username* (paginated)."""
        result = []
        page = 1

        while True:
            data = self.request(
                "GET",
                f"/users/{username}/repos",
                params={
                    "per_page": 100,
                    "page": page,
                    "type": "public",
                    "sort": "updated",
                },
            )

            if not data:
                break

            result.extend(data)
            if len(data) < 100:
                break
            page += 1

        return result

    def repo_languages(self, owner, repo):
        """Return language breakdown for a repository.

        Returns a dict like {"Java": 150000, "Python": 50000}.
        """
        return self.request("GET", f"/repos/{owner}/{repo}/languages") or {}
