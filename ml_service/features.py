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


def extract_interaction_features(interactions):
    """Extract interaction features (issue #25) — attention we received.

    *interactions* is the list of dicts from ``Database.user_interactions_before``
    (already filtered to events BEFORE the follow timestamp — temporal
    hygiene is enforced by the caller, never here).

    Features:
      * ``interacted_with_us`` — binary: any persisted interaction before
        the follow,
      * per-event-type counts (stars / forks / issues / PRs / comments),
        capped and log-normalised like the repo features.

    Returns a dict of feature_name → float.
    """
    feats = {}
    interactions = interactions or []

    feats["interacted_with_us"] = 1.0 if interactions else 0.0

    # GitHub event types → semantic bucket (issue #25 spec).
    buckets = {
        "stars": {"WatchEvent"},
        "forks": {"ForkEvent"},
        "issues": {"IssuesEvent"},
        "prs": {"PullRequestEvent", "PullRequestReviewEvent"},
        "comments": {"IssueCommentEvent", "CommitCommentEvent", "PullRequestReviewCommentEvent"},
    }
    counts = {name: 0 for name in buckets}
    for it in interactions:
        et = it.get("event_type") if isinstance(it, dict) else None
        for name, types in buckets.items():
            if et in types:
                counts[name] += 1
                break

    for name, cnt in counts.items():
        # Capped + log-normalised, mirroring the repo count features.
        feats[f"interaction_{name}_log"] = _log1p_normalise(cnt, cap=10)

    return feats


# Fixed, ordered set of discovery-source categories (issue #25).  The raw
# ``users.discovered_from`` column is NOT a clean category today: the graph
# walk writes the scanned username as the value, and repo-content sources
# use prefixed values.  ``normalise_source`` maps any raw value onto this
# fixed set so the one-hot is stable across retrains.
SOURCE_CATEGORIES = [
    "stargazers",      # stargazers:owner/repo (issue #22)
    "contributors",    # contributors:owner/repo (issue #22)
    "repo_interaction",  # attention worker (issue #21)
    "owner_followers", # follower scan
    "self",            # the owner
    "graph",           # anything else (follower-graph walk, unknown)
]


def normalise_source(discovered_from):
    """Map a raw ``discovered_from`` value onto a fixed source category.

    * ``stargazers:*`` → ``stargazers``
    * ``contributors:*`` → ``contributors``
    * ``repo_interaction`` / ``owner_followers`` / ``self`` as-is
    * everything else (scanned usernames from the graph walk, None) →
      ``graph``
    """
    if not discovered_from:
        return "graph"
    if discovered_from in ("stargazers", "contributors", "repo_interaction",
                           "owner_followers", "self"):
        return discovered_from
    if discovered_from.startswith("stargazers:"):
        return "stargazers"
    if discovered_from.startswith("contributors:"):
        return "contributors"
    return "graph"


def extract_source_features(discovered_from):
    """One-hot discovery-source features (issue #25).

    *discovered_from* is the raw ``users.discovered_from`` value (or None).
    Normalised onto the fixed :data:`SOURCE_CATEGORIES` set so the model
    can calibrate for the population shift between discovery channels and
    evaluation can report per-channel AUC.
    """
    cat = normalise_source(discovered_from)
    return {f"source_{c}": (1.0 if c == cat else 0.0) for c in SOURCE_CATEGORIES}


# ── Public API ────────────────────────────────────────────────────────────

def build_feature_vector(
    profile_json,
    repo_agg,
    user_languages,
    user_topics,
    global_top_langs,
    global_top_topics,
    feature_order,
    interactions=None,
    discovered_from=None,
):
    """Build an ordered feature vector as a list of floats.

    Parameters
    ----------
    All parameters correspond to data for a single user.
    *feature_order* is the list of feature names in the order
    expected by the model (saved during training).
    *interactions* is the list of dicts from
    ``Database.user_interactions_before`` (events BEFORE the follow —
    temporal hygiene enforced by the caller).  *discovered_from* is the
    raw ``users.discovered_from`` value for the one-hot source feature.
    Both default to None (all-zero feature groups) for callers that
    predate issue #25 or have no data.

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
    feats.update(extract_interaction_features(interactions))
    feats.update(extract_source_features(discovered_from))

    return [feats.get(name, 0.0) for name in feature_order]


def build_feature_vector_for_training(
    profile_json, repo_agg, user_languages, user_topics,
    global_top_langs, global_top_topics,
    interactions=None, discovered_from=None,
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
    feats.update(extract_interaction_features(interactions))
    feats.update(extract_source_features(discovered_from))

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
