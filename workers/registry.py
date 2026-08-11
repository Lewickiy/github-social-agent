"""Registry of background workers shown in the dashboard Management tab.

Single source of truth for the worker list.  Keys must match the
``WORKER_KEY`` constants in each ``workers/*.py`` module and the rows of
the ``worker_status`` table (migration 027).  Label/description are
display metadata for the UI; the API merges them with live runtime
status (started/stopped/last action/last error/heartbeat + toggle).
"""

WORKERS = (
    {
        "key": "follow",
        "label": "Follow",
        "description": (
            "Follows the highest-scored candidates (≥ threshold) at a "
            "random 20–30 min interval, within the daily limit."
        ),
    },
    {
        "key": "unfollow",
        "label": "Unfollow",
        "description": (
            "Unfollows users followed 7+ days who never followed back and "
            "never interacted with your profile or repos — same interval "
            "and daily budget as follows."
        ),
    },
    {
        "key": "graph_discovery",
        "label": "Graph discovery",
        "description": (
            "Walks the follower graph at a calm, constant rate (≤500 req/h) "
            "— one bounded pass at a time to grow the network."
        ),
    },
    {
        "key": "ml_trainer",
        "label": "ML trainer",
        "description": (
            "Retrains the follow-back prediction model every 24 h and "
            "recomputes all predictions."
        ),
    },
    {
        "key": "company",
        "label": "Companies",
        "description": (
            "Enriches @-mentioned company/org profiles from the GitHub "
            "API, one every 5 minutes."
        ),
    },
    {
        "key": "followback_check",
        "label": "Followback check",
        "description": (
            "Hourly health check that confirmed mutual-follow users "
            "still follow back."
        ),
    },
    {
        "key": "attention",
        "label": "Attention",
        "description": (
            "Hourly poll of the owner's event timeline — records who "
            "starred / forked / opened issues or PRs on your repos and "
            "adds new interactors to the pipeline."
        ),
    },
    {
        "key": "reciprocal",
        "label": "Reciprocal",
        "description": (
            "Answers attention with attention: follows new interactors "
            "and stars their most relevant repository, paced like follows "
            "(20–30 min) within the shared daily budget."
        ),
    },
    {
        "key": "snapshot",
        "label": "Snapshots",
        "description": (
            "Records the daily snapshot of the owner's followers / "
            "following / public repos."
        ),
    },
)
