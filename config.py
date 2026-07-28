import os
from pathlib import Path

# Load .env file from project root (for PyCharm and terminal)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv not installed — rely on OS environment only

# =====================================================
# GITHUB API
# =====================================================

TOKEN = os.getenv("GITHUB_TOKEN")

MY_USERNAME = "Lewickiy"

API = "https://api.github.com"


def get_headers():
    """Build request headers.  Raises if GITHUB_TOKEN is missing.

    Called lazily (not at import time) so that DB-only commands
    like --top, --profile, --migrate work without a token.
    """
    token = os.getenv("GITHUB_TOKEN")
    if token is None:
        raise RuntimeError(
            "Environment variable GITHUB_TOKEN is not set.\n"
            "Export it before running commands that need the GitHub API:\n"
            "  export GITHUB_TOKEN=your_token_here"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


# Keep HEADERS as a lazy property for backward compatibility
class _LazyHeaders:
    """Dict-like object that resolves headers on first access."""
    def __repr__(self):
        return repr(get_headers())
    def __getitem__(self, key):
        return get_headers()[key]
    def __iter__(self):
        return iter(get_headers())
    def __len__(self):
        return len(get_headers())
    def keys(self):
        return get_headers().keys()
    def values(self):
        return get_headers().values()
    def items(self):
        return get_headers().items()
    def get(self, key, default=None):
        return get_headers().get(key, default)


HEADERS = _LazyHeaders()

# =====================================================
# BOT SETTINGS
# =====================================================

DAILY_FOLLOW_LIMIT = 200

FOLLOW_DELAY = 60

DATABASE = "github_social.db"

LOG_FILE = "github_social.log"

# =====================================================
# SILENT MODE — human-like intervals (seconds)
# =====================================================

SILENT_DELAY_BETWEEN_USERS = 60  # pause between users (2 min)
SILENT_DELAY_BETWEEN_REPOS = 20  # pause between repos of one user
SILENT_DELAY_BETWEEN_REQUESTS = 5  # pause between API calls within a repo
SILENT_DELAY_BETWEEN_SCORES = 10  # pause between scoring users
SILENT_DELAY_BETWEEN_FOLLOWS = 30  # pause between follow actions in silent mode

# =====================================================
# FOLLOW SCORING
# =====================================================

SILENT_FOLLOW_SCORE_THRESHOLD = 35  # minimum score to auto-follow in silent mode

# =====================================================
# SCORING
# =====================================================

CURRENT_SCORE_VERSION = 4

# =====================================================
# OPTIMIZATION — TTL / FRESHNESS (days)
# =====================================================

REPO_FRESHNESS_DAYS = 7       # skip repo fetch if collected within this period
OWNER_SYNC_DAYS = 14          # re-sync owner repos/langs at most this often
SCORE_FRESHNESS_DAYS = 30     # skip scoring if scored within this period
FOLLOWER_SCAN_DAYS = 7        # re-scan follower graph at most this often
