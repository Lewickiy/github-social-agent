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

# The daily social-action budget — a COMBINED cap shared by follows and
# unfollows: FollowWorker + UnfollowWorker together may perform at most
# 50 actions per local day (e.g. 40 follows + 10 unfollows).  Both workers
# count against this single pool (see Database.today_social_actions).
DAILY_FOLLOW_LIMIT = 50

# Random pause between two follow actions (seconds).  The FollowWorker is
# the only follower in the system: it spaces subscriptions 20–30 minutes
# apart, so the daily budget (≈50 follows, one every ~25 min) is spread
# across the whole day instead of being spent in a burst.  The
# UnfollowWorker uses the same random 20–30 min pacing.
FOLLOW_INTERVAL_MIN_SECONDS = 20 * 60
FOLLOW_INTERVAL_MAX_SECONDS = 30 * 60

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
# (Follow pacing is handled by the dedicated FollowWorker — a random
# 20–30 min interval, see FOLLOW_INTERVAL_MIN/MAX_SECONDS below.)

# Number of parallel workers that process the silent-mode queue.
# 1 = original single-threaded behaviour; 2 roughly doubles the request
# rate (~1200 → ~2400 req/h) while staying far below GitHub's limits
# (primary 5000 req/h, secondary 900 pts/min/endpoint).
SILENT_WORKERS = 2

# =====================================================
# FOLLOW SCORING
# =====================================================

SILENT_FOLLOW_SCORE_THRESHOLD = 35  # minimum score to auto-follow (silent & FollowWorker)

# =====================================================
# FOLLOW WORKER — dedicated follow executor thread
# =====================================================

# Follows the queued candidates (NEW users with score >= threshold, best
# score first) in a separate daemon thread (workers/follow_worker.py),
# independently of the analysis pipeline: silent mode only collects +
# scores, while the worker drains the follow queue at a random 20–30 min
# interval between subscriptions and within DAILY_FOLLOW_LIMIT.  When no
# qualifying users exist it simply waits — polling every
# FOLLOW_WORKER_POLL_INTERVAL_SECONDS.
#
# This flag only seeds the *fresh-install* default of the Management-tab
# toggle (migration 027); at runtime the worker's enabled/disabled state
# lives in the worker_status table and is controlled from the dashboard.
FOLLOW_WORKER_ENABLED = True
FOLLOW_WORKER_POLL_INTERVAL_SECONDS = 60

# =====================================================
# UNFOLLOW WORKER — inactive-follow cleanup thread
# =====================================================

# A dedicated daemon thread (workers/unfollow_worker.py) unfollows users
# who were followed UNFOLLOW_AFTER_DAYS or more ago, never followed us
# back, and never interacted with the owner's profile or repositories
# (star / fork / issue / PR / comment / review / push / … — checked live
# via the public events timeline, NOT persisted).  It paces unfollows at
# the same random 20–30 min interval as the FollowWorker and draws from
# the SAME daily budget (DAILY_FOLLOW_LIMIT, follows + unfollows combined).
#
# UNFOLLOW_WORKER_ENABLED only seeds the *fresh-install* default of the
# Management-tab toggle (migration 028); at runtime the worker's
# enabled/disabled state lives in the worker_status table and is
# controlled from the dashboard like every other worker.
UNFOLLOW_WORKER_ENABLED = True

# How long (days) we keep a non-responding follow before it becomes
# eligible for the unfollow worker.  Matches the ML negative-class
# observation window (ML_MIN_* / get_training_users 7-day rule).
UNFOLLOW_AFTER_DAYS = 7

# Poll interval when no eligible users exist (the worker waits and
# re-scans instead of burning API requests).
UNFOLLOW_WORKER_POLL_INTERVAL_SECONDS = 60

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
# one-shot behaviour).  Either way, --collect* / --score are only needed
# as one-off force/backfill modes.
SILENT_CONTINUOUS = True

# =====================================================
# GRAPH DISCOVERY WORKER — separate calm background thread
# =====================================================

# Grows the network by walking the follower graph at a conservative,
# constant rate instead of in bursts.  Started automatically by --silent
# (the only mode that needs growth).  DISCOVERY_WORKER_ENABLED only seeds
# the fresh-install default of the Management-tab toggle (migration 027);
# at runtime the worker's state lives in the worker_status table.
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

# ── ML follow gate (the "strictness" dial) ──────────────────────────────
# The FollowWorker normally follows every NEW candidate whose heuristic
# score passes SILENT_FOLLOW_SCORE_THRESHOLD.  The ML follow gate makes
# the model a second opinion: when enabled, a candidate whose stored
# followback prediction is 0 (model confidence < threshold) is skipped,
# so the daily budget goes to users the model believes will reciprocate.
#
#   * ``ML_FOLLOW_GATE_ENABLED`` — master switch for the gate.  These
#     constants only seed the *fresh-install* defaults; at runtime both
#     values live in the settings table (keys ``ml_follow_gate_enabled`` /
#     ``ml_follow_threshold``) and are editable from the Management tab
#     without a restart.
#   * ``ML_FOLLOW_THRESHOLD`` — the model's followback probability a
#     candidate must reach to be followed (0.5 = current decision rule;
#     higher = fewer, more selective follows).  Predictions are stored
#     with the threshold applied (ml_follow_prediction = 0/1), and a
#     background recompute refreshes them whenever the threshold changes.
#   * Candidates with no prediction yet (NULL — scored before ML existed
#     or inference failed) are NOT vetoed: the gate only blocks a
#     definitive 0, so the queue keeps flowing.
#
# The gate is OFF by default: the bot follows by heuristic score exactly
# as before, while the model still trains and refreshes predictions in
# the background (shadow mode) — flip the Management-tab toggle to make
# the gate a hard second opinion again.
ML_FOLLOW_GATE_ENABLED = False
ML_FOLLOW_THRESHOLD = 0.5

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
