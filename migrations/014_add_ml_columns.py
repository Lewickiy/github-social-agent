"""Migration 014 — Add ML-related columns to users table.

Adds:
  * users.github_profile_json — full GitHub API user response as JSON
  * users.ml_follow_prediction — ML model prediction (NULL/0/1)
"""


def up(conn):
    columns = [
        row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    ]

    if "github_profile_json" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN github_profile_json TEXT")

    if "ml_follow_prediction" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN ml_follow_prediction INTEGER DEFAULT NULL"
        )

    conn.commit()
    print("  Added github_profile_json, ml_follow_prediction to users.")


def down(conn):
    # SQLite < 3.35 has limited DROP COLUMN support — no-op.
    pass
