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

# GitHub's primary rate limit for authenticated requests (requests/hour).
# The dashboard's "GitHub API / hour" card compares real traffic against
# this ceiling.
GITHUB_API_RATE_LIMIT = 5000


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

DAILY_FOLLOW_LIMIT = 50

FOLLOW_DELAY = 60

DATABASE = os.getenv("DATABASE", "data/github_social.db")

LOG_FILE = os.getenv("LOG_FILE", "github_social.log")

# =====================================================
# SILENT MODE — human-like intervals (seconds)
# =====================================================

SILENT_DELAY_BETWEEN_USERS = 1  # pause between users
# ── Delays reduced 3/2 → 1/1 (API_using.md §7.1) ───────────────────────
# The primary GitHub limit (5 000 req/h) is used at only ~13–16 %, so the
# old 5 s-per-repo stealth sleep was the real bottleneck (85–130 days for
# the ~33k-user queue).  1 s + the built-in ±30 % jitter keeps a
# human-ish rhythm while being ~3× faster.  The dominant /languages
# traffic is cut further by SKIP_FORK_LANGUAGES (§7.2) and ETag/304
# conditional requests (§7.3).
SILENT_DELAY_BETWEEN_REPOS = 1      # was 2 (throttled to avoid /languages abuse)
SILENT_DELAY_BETWEEN_REQUESTS = 1   # was 3 (throttled to avoid rate-limit)
SILENT_DELAY_BETWEEN_SCORES = 1  # pause between scoring users
SILENT_DELAY_BETWEEN_FOLLOWS = 2  # pause between follow actions in silent mode

# =====================================================
# FOLLOW SCORING
# =====================================================

SILENT_FOLLOW_SCORE_THRESHOLD = 35  # minimum score to auto-follow in silent mode

# =====================================================
# OWNER FOLLOWER SCAN — cyclic check in silent mode
# =====================================================

OWNER_FOLLOWER_SCAN_INTERVAL = 20  # check for new owner followers every N processed users

# =====================================================
# SCORING
# =====================================================

CURRENT_SCORE_VERSION = 4

# =====================================================
# OPTIMIZATION — TTL / FRESHNESS (days)
# =====================================================

REPO_FRESHNESS_DAYS = 7       # skip repo fetch if collected within this period (user-level)
OWNER_SYNC_DAYS = 14          # re-sync owner repos/langs at most this often
SCORE_FRESHNESS_DAYS = 20     # skip scoring if scored within this period
FOLLOWER_SCAN_DAYS = 5        # re-scan follower graph at most this often

# How often (hours) the background worker re-checks FOLLOWBACK users
# to detect those who unfollowed us after a mutual follow.
FOLLOWBACK_CHECK_INTERVAL_HOURS = 1

# =====================================================
# HISTORICAL SNAPSHOTS — daily mutable-profile snapshots
# =====================================================

# Hour of day (server local time) at which the snapshot worker records
# the daily snapshot of the owner's followers / following / public repos.
SNAPSHOT_HOUR = 12

# Take a snapshot immediately when the worker starts (in addition to the
# scheduled noon one).  Ensures today has a data point even when the bot
# is started outside the noon window.
SNAPSHOT_ON_START = True

# When True, process less-followed users first within each priority group
# (maximises follow-backs — smaller accounts are more likely to reciprocate).
# When False (default), popular users go first (richer data, better scoring).
SILENT_PRIORITIZE_SMALL = False

# Silent mode — per-repo TTL for the /languages API call.
# A repo whose `last_checked_at` is within this window is skipped
# (no /repos/{owner}/{repo}/languages request).
SILENT_REPO_CHECK_FRESHNESS_DAYS = 5

# =====================================================
# ML — нейронная сеть для прогнозирования FOLLOWBACK
# =====================================================

ML_ENABLED = True                  # enable ML inference
ML_TRAIN_AFTER_START = True        # train immediately after worker starts
ML_TRAIN_INTERVAL_HOURS = 24       # re-train every N hours
ML_MODEL_DIR = "models"            # directory for saved model files
ML_TOP_LANGUAGES = 30              # top-N languages for multi-hot encoding
ML_TOP_TOPICS = 50                 # top-N topics for multi-hot encoding

# ── Heavy / fork language-fetch gate ─────────────────────────────────────
# When `READ_HEAVY_FORK_LANGUAGES` is False (default) the bot does NOT
# call /repos/{owner}/{repo}/languages for:
#   • any repo larger than `SKIP_LANGS_MAX_SIZE_KB`, or
#   • any fork larger than `SKIP_LANGS_FORK_SIZE_KB`.
# This protects against GitHub's secondary rate limit (abuse detection)
# which is most easily tripped by /languages on heavy forks (DeepFaceLive,
# Ryujinx, etc.).  Flip the flag to True to revert to the old
# unconditional behaviour.
READ_HEAVY_FORK_LANGUAGES = False
SKIP_LANGS_MAX_SIZE_KB = 500_000   # 500 MB
SKIP_LANGS_FORK_SIZE_KB = 50_000    # 50 MB

# ── Skip /languages entirely for forked repos (API_using.md §7.2) ───────
# A fork mirrors an upstream repo, so its language breakdown is nearly
# identical to the source — low signal for similarity scoring.  Forks are
# ~54 % of all collected repos, so skipping them cuts the dominant
# /languages API traffic almost in half.  Setting
# `READ_HEAVY_FORK_LANGUAGES` to True still overrides this and fetches
# languages for every repo (legacy behaviour).
SKIP_FORK_LANGUAGES = True
