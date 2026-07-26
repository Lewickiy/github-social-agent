import os

# =====================================================
# GITHUB API
# =====================================================

TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    "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",
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

DAILY_FOLLOW_LIMIT = 90

FOLLOW_DELAY = 60

DATABASE = "github_social.db"

LOG_FILE = "github_social.log"

# =====================================================
# SILENT MODE — human-like intervals (seconds)
# =====================================================

SILENT_DELAY_BETWEEN_USERS   = 120   # pause between users (2 min)
SILENT_DELAY_BETWEEN_REPOS   = 15    # pause between repos of one user
SILENT_DELAY_BETWEEN_REQUESTS = 5    # pause between API calls within a repo
SILENT_DELAY_BETWEEN_SCORES   = 30   # pause between scoring users