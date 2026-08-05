import os
from pathlib import Path

# Load .env file from project root (for PyCharm and terminal)
try:
    from dotenv import load_dotenv
    # Project root is two levels up from core/config.py
    _env_path = Path(__file__).resolve().parent.parent / ".env"
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

LOG_FILE = os.getenv("LOG_FILE", "logs/github_social.log")

# =====================================================
# SILENT MODE — human-like intervals (seconds)
# =====================================================

SILENT_DELAY_BETWEEN_USERS = 1  # pause between users
# ── Delays reduced 3/2 → 1/1 (documentation/API_using.md §7.1) ────────
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

# Number of parallel workers that process the silent-mode queue.
# 1 = original single-threaded behaviour; 2 roughly doubles the request
# rate (~1200 → ~2400 req/h) while staying far below GitHub's limits
# (primary 5000 req/h, secondary 900 pts/min/endpoint).
SILENT_WORKERS = 2

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
# 0 = midnight — both series (followers AND following) update together
# once a day so the Followers growth chart never shows one metric fresh
# and the other stale.
SNAPSHOT_HOUR = 0

# Take a snapshot immediately when the worker starts (in addition to the
# scheduled midnight one).  Ensures today has a data point even when the
# bot is started outside the midnight window (the per-day upsert keeps it
# idempotent).
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
# SILENT MODE — first-run gates & self-sustaining loop
# =====================================================

# A brand-new account can't do meaningful work: with zero followers the
# queue is empty (nothing to process, no graph seeds), and an owner with
# no collected repos makes the similarity half of the score dead (every
# score caps at 35/100).  When True, silent mode prints a clear message
# and WAITS — polling GitHub (owner followers / owner repos) every
# SILENT_GATE_CHECK_INTERVAL_SECONDS — until the account is ready,
# instead of silently finishing with "Processing 0 users".
SILENT_GATES_ENABLED = True

# How often (seconds) the gate-wait / idle-wait loops re-check GitHub
# (owner followers, owner repos) and the DB.
SILENT_GATE_CHECK_INTERVAL_SECONDS = 300

# Minimum number of owner repos that must be collected before scoring is
# allowed.  Below this, similarity scoring has nothing to compare against
# and the runner waits for owner data instead of scoring blind.
SILENT_MIN_OWNER_REPOS = 1

# When True, silent mode is the SINGLE working mode: it never exits.
# After draining its queue it waits for new work instead of stopping —
# owner-follower scans run inside silent, while follower-graph growth is
# handled by a separate calm background worker (see the DISCOVERY_* block
# below).  When False, silent mode processes one batch and exits (legacy
# one-shot behaviour).  Either way, --collect* / --score / --follow are
# only needed as one-off force/backfill modes.
SILENT_CONTINUOUS = True

# =====================================================
# GRAPH DISCOVERY WORKER — separate calm background thread
# =====================================================

# Grows the network by walking the follower graph at a conservative,
# constant rate instead of in bursts.  Started automatically by --silent
# (the only mode that needs growth); disable with DISCOVERY_WORKER_ENABLED.
#
# Hard request cap: each pass walks at most DISCOVERY_PASS_MAX_USERS users
# (~1 profile request each, plus a followers request when their count
# grew), paced to at most DISCOVERY_RATE_LIMIT_PER_HOUR requests/hour.  A
# pass is sized to one hour's budget and runs once per hour, so the worker
# stays calm and never blocks silent's processing.
DISCOVERY_WORKER_ENABLED = True
DISCOVERY_RATE_LIMIT_PER_HOUR = 500
DISCOVERY_PASS_MAX_USERS = 500

# =====================================================
# ML — нейронная сеть для прогнозирования FOLLOWBACK
# =====================================================

ML_ENABLED = True                  # enable ML inference
ML_TRAIN_AFTER_START = True        # train immediately after worker starts
ML_TRAIN_INTERVAL_HOURS = 24       # re-train every N hours
ML_MODEL_DIR = "models"            # directory for saved model files

# Feature dimensionality.  Kept deliberately small: with a tiny training
# set (a few hundred labeled users) every extra multi-hot dimension is
# almost always 0 and only adds noise.  10 languages + 15 topics yield
# ~54 features total (was 109 with 30/50) — a ratio the data can support.
ML_TOP_LANGUAGES = 10              # top-N languages for multi-hot encoding
ML_TOP_TOPICS = 15                 # top-N topics for multi-hot encoding

# Architecture / training (see ml_service/trainer.py).
ML_HIDDEN_DIM = 32                 # hidden layer width (was 64)
ML_DROPOUT = 0.2                   # dropout between hidden layers (0 = off)
ML_EPOCHS = 80                     # max epochs (early stopping usually cuts this short)
ML_EARLY_STOP_PATIENCE = 10        # stop after N epochs without val-loss improvement

# How many versioned models to keep on disk.  Each retrain overwrites
# current.pt; we additionally keep the previous version for a quick
# rollback.  Older versions (30+ at ~40 KB each) have no value — the
# newest model trained on the most data is always the best.
ML_KEEP_MODELS = 2

# Random seed for fully reproducible training runs.  Without it every
# retrain started from different random weights, a different train/val
# split and a different batch order — so identical data produced
# different models and val metrics bounced 0.54 → 0.77 between runs.
# With the seed set, the same dataset always yields bit-identical
# weights and metrics.  Recorded in each model's metadata.
ML_SEED = 42

# Minimum labeled dataset before training is allowed.  With a few
# hundred samples the net has nothing to learn (CV AUC lands in the
# 0.5–0.6 random-noise band) — so training stays skipped until both
# classes are decently represented.
ML_MIN_POSITIVE_SAMPLES = 100
ML_MIN_NEGATIVE_SAMPLES = 100
ML_MIN_TOTAL_SAMPLES = 250

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

# ── Skip /languages entirely for forked repos (documentation/API_using.md §7.2)
# A fork mirrors an upstream repo, so its language breakdown is nearly
# identical to the source — low signal for similarity scoring.  Forks are
# ~54 % of all collected repos, so skipping them cuts the dominant
# /languages API traffic almost in half.  Setting
# `READ_HEAVY_FORK_LANGUAGES` to True still overrides this and fetches
# languages for every repo (legacy behaviour).
SKIP_FORK_LANGUAGES = True
