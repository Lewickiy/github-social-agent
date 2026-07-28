import os

# =====================================================
# GITHUB API
# =====================================================

TOKEN = os.getenv("GITHUB_TOKEN")

if TOKEN is None:
    raise RuntimeError(
        "Environment variable GITHUB_TOKEN is not set."
    )

MY_USERNAME = "Lewickiy"

API = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}

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
