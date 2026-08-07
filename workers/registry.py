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
        "key": "graph_discovery",
        "label": "Graph discovery",
        "description": (
            "Walks the follower graph at a calm rate (≤500 req/h) — one "
            "bounded pass per hour to grow the network."
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
        "key": "snapshot",
        "label": "Snapshots",
        "description": (
            "Records the daily snapshot of the owner's followers / "
            "following / public repos."
        ),
    },
)
