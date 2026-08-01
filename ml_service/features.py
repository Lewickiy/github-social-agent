"""
Feature extraction for the FOLLOWBACK ML model.

Transforms raw DB data (profile JSON, repo aggregates,
languages, topics) into a normalised numerical feature vector.

All features are computed purely from the database — no
GitHub API calls required.
"""

import json
import math
from datetime import datetime, timezone


# ── Global constants ──────────────────────────────────────────────────────
# Profile fields from the GitHub API that we check for presence/absence
_BOOL_FIELDS = [
    "blog", "location", "email", "hireable",
    "twitter_username", "name",
]

# Magic value for "no repo activity" (9999 days)
_NO_ACTIVITY_DAYS = 9999


# ── Public helpers ────────────────────────────────────────────────────────

def extract_profile_features(profile_json):
    """Extract numerical features from the GitHub user profile JSON.

    Returns a dict of feature_name → float.

    All features are normalised to roughly [0, 1] or small values
    to avoid saturating the first layer's gradients.
    """
    if not profile_json:
        profile_json = {}

    feats = {}

    # ── Numeric: min-max normalisation with log-scale clamping ──
    followers = profile_json.get("followers", 0) or 0
    feats["followers_log"] = _log1p_normalise(followers, cap=10_000)

    following = profile_json.get("following", 0) or 0
    feats["following_log"] = _log1p_normalise(following, cap=10_000)

    public_repos = profile_json.get("public_repos", 0) or 0
    feats["public_repos_log"] = _log1p_normalise(public_repos, cap=1_000)

    public_gists = profile_json.get("public_gists", 0) or 0
    feats["public_gists_log"] = _log1p_normalise(public_gists, cap=1_000)

    # ──Boolean fields → 0/1 ──
    feats["has_bio"] = 1.0 if profile_json.get("bio") else 0.0
    feats["bio_len"] = min(len(profile_json.get("bio") or ""), 200) / 200.0
    feats["has_company"] = 1.0 if profile_json.get("company") else 0.0
    for field in _BOOL_FIELDS:
        feats[f"has_{field}"] = 1.0 if profile_json.get(field) else 0.0

    # ── hireable: None means not set ──
    feats["has_hireable"] = 1.0 if profile_json.get("hireable") else 0.0

    # ── Account type ──
    feats["type_is_org"] = 1.0 if profile_json.get("type") == "Organization" else 0.0

    # ── Account age (days since registration) ──
    feats["account_age_days"] = _account_age_days(profile_json.get("created_at"))

    return feats


def extract_repo_features(repo_agg):
    """Extract normalised features from aggregated repo stats.

    *repo_agg* is the dict returned by ``Database.get_user_repo_aggregates()``.
    """
    rc = repo_agg.get("repo_count", 0) or 0

    feats = {}

    feats["repo_count_log"] = _log1p_normalise(rc, cap=500)
    feats["avg_stars_log"] = _log1p_normalise(repo_agg.get("avg_stars", 0), cap=1_000)
    feats["max_stars_log"] = _log1p_normalise(repo_agg.get("max_stars", 0), cap=10_000)
    feats["sum_stars_log"] = _log1p_normalise(repo_agg.get("sum_stars", 0), cap=50_000)
    feats["avg_forks_log"] = _log1p_normalise(repo_agg.get("avg_forks", 0), cap=500)
    feats["avg_watchers_log"] = _log1p_normalise(repo_agg.get("avg_watchers", 0), cap=200)
    feats["archived_ratio"] = (repo_agg.get("archived_count", 0) / max(rc, 1))
    feats["fork_ratio"] = (repo_agg.get("fork_count", 0) / max(rc, 1))
    feats["has_description_ratio"] = repo_agg.get("has_description_ratio", 0)

    # avg repo age: clamp to ~10 years (3650 days)
    feats["avg_repo_age"] = _linear_normalise(
        repo_agg.get("avg_repo_age_days", 0), cap=3650,
    )

    # days since last push: invert so higher = more recent
    dps = repo_agg.get("days_since_last_push", _NO_ACTIVITY_DAYS)
    if dps >= _NO_ACTIVITY_DAYS:
        feats["recency"] = 0.0
    else:
        feats["recency"] = 1.0 - _linear_normalise(dps, cap=730)  # 2 years

    return feats


def extract_language_features(user_languages, global_top_langs):
    """Extract language features: multi-hot for top-N + aggregated stats.

    *user_languages* is the list of (lang_name, pct) from DB.
    *global_top_langs* is a list of (lang_name, _freq) from DB.
    """
    user_lang_dict = dict(user_languages) if user_languages else {}
    top_names = [name for name, _ in global_top_langs]

    feats = {}

    # Language count & diversity
    lang_count = len(user_lang_dict)
    feats["language_count_log"] = _log1p_normalise(lang_count, cap=30)

    # Top language percentage
    if user_languages:
        feats["top_language_pct"] = user_languages[0][1] / 100.0
    else:
        feats["top_language_pct"] = 0.0

    # Multi-hot for top-N global languages
    for lang_name in top_names:
        key = f"lang_{_safe_key(lang_name)}"
        feats[key] = 1.0 if lang_name in user_lang_dict else 0.0

    return feats


def extract_topic_features(user_topics, global_top_topics):
    """Extract topic features: multi-hot for top-N + count.

    *user_topics* is a list of topic strings.
    *global_top_topics* is a list of (topic_name, _freq).
    """
    user_set = set(user_topics or [])
    top_names = [name for name, _ in global_top_topics]

    feats = {}

    # Topic count (log-scale)
    feats["topic_count_log"] = _log1p_normalise(len(user_set), cap=100)

    # Multi-hot for top-N global topics
    for topic_name in top_names:
        key = f"topic_{_safe_key(topic_name)}"
        feats[key] = 1.0 if topic_name in user_set else 0.0

    return feats


# ── Public API ────────────────────────────────────────────────────────────

def build_feature_vector(
    profile_json,
    repo_agg,
    user_languages,
    user_topics,
    global_top_langs,
    global_top_topics,
    feature_order,
):
    """Build an ordered feature vector as a list of floats.

    Parameters
    ----------
    All parameters correspond to data for a single user.
    *feature_order* is the list of feature names in the order
    expected by the model (saved during training).

    Returns
    -------
    list[float]
        Feature values in the same order as *feature_order*.
    """
    feats = {}
    feats.update(extract_profile_features(profile_json))
    feats.update(extract_repo_features(repo_agg))
    feats.update(extract_language_features(user_languages, global_top_langs))
    feats.update(extract_topic_features(user_topics, global_top_topics))

    return [feats.get(name, 0.0) for name in feature_order]


def build_feature_vector_for_training(
    profile_json, repo_agg, user_languages, user_topics,
    global_top_langs, global_top_topics,
):
    """Same as ``build_feature_vector`` but also returns feature names.

    Used during the initial training run to capture the feature
    order for subsequent inference.
    """
    feats = {}
    feats.update(extract_profile_features(profile_json))
    feats.update(extract_repo_features(repo_agg))
    feats.update(extract_language_features(user_languages, global_top_langs))
    feats.update(extract_topic_features(user_topics, global_top_topics))

    # Sort by name for deterministic ordering
    names = sorted(feats.keys())
    return [feats[n] for n in names], names


# ── Internal helpers ──────────────────────────────────────────────────────

def _log1p_normalise(value, cap):
    """log1p(value) / log1p(cap) — clamped to [0, 1]."""
    if value <= 0:
        return 0.0
    v = min(float(value), float(cap))
    return math.log1p(v) / math.log1p(cap)


def _linear_normalise(value, cap):
    """value / cap — clamped to [0, 1]."""
    return max(0.0, min(1.0, float(value) / float(cap)))


def _account_age_days(created_at):
    """Return account age in days, normalised to [0, 1] with a 10-year cap."""
    if not created_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - dt).days
        return _linear_normalise(days, 3650)  # 10 years
    except (ValueError, TypeError):
        return 0.0


def _safe_key(name):
    """Sanitise a language or topic name into a valid dict key."""
    return name.lower().replace(" ", "_").replace("-", "_").replace("#", "sharp").replace("+", "plus").replace(".", "_")
