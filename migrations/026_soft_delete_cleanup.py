"""Migration 026 — sanitize existing DELETED users (soft-delete cleanup).

Until now ``Database.mark_deleted`` only stamped the DELETED status and
logged the event; the ``users`` row (score / counts / bio) and its
repository / company footprint were left untouched.  This migration
applies the same sanitization the (upgraded) ``mark_deleted`` performs
for new deletions to every user whose current status is DELETED:

  * score → 0, public_repos / followers / followers_count → 0,
    bio / company → NULL, ML prediction → NULL,
  * ``scored_at`` → the moment the account was deleted (first DELETED
    transition in ``user_status_history``) so the timeline stays
    truthful,
  * repositories together with their language/topic links and company
    links are removed (``companies`` rows are global — other users may
    reference them, so they are kept),
  * followers / following / public_repos inside the cached
    ``github_profile_json`` are zeroed (static fields such as the
    avatar are kept so the row still renders).

The ``users`` row itself is never deleted — status history and the
activity feed stay intact (soft delete).
"""


def up(conn):
    # Current DELETED users — the same "current status" definition the
    # rest of the system uses (materialised user_current_status table).
    deleted = (
        "SELECT username FROM user_current_status WHERE status = 'DELETED'"
    )

    # Zero the profile columns.  scored_at gets the deletion time (the
    # first DELETED transition) rather than "now", so historical rows
    # keep a truthful timestamp.
    conn.execute(
        f"""
        UPDATE users SET
            score        = 0,
            public_repos = 0,
            followers    = 0,
            followers_count = 0,
            bio          = NULL,
            company      = NULL,
            scored_at    = COALESCE(
                (SELECT h.changed_at FROM user_status_history h
                 WHERE h.username = users.username AND h.status = 'DELETED'
                 ORDER BY h.id ASC LIMIT 1),
                scored_at
            ),
            ml_follow_prediction = NULL
        WHERE username IN ({deleted})
        """
    )

    # Zero the mutable counts inside the cached profile JSON while
    # keeping static fields.  json_remove first (so keys never hold
    # stale values), then json_set to pin them at 0.  Guarded by
    # json_valid + object check — corrupt or non-object caches are
    # left as-is.
    conn.execute(
        f"""
        UPDATE users SET
            github_profile_json = CASE
                WHEN json_valid(github_profile_json)
                 AND json_type(github_profile_json) = 'object'
                THEN json_set(
                         json_remove(github_profile_json,
                                     '$.followers', '$.following',
                                     '$.public_repos'),
                         '$.followers', 0,
                         '$.following', 0,
                         '$.public_repos', 0)
                ELSE github_profile_json
            END
        WHERE username IN ({deleted})
        """
    )

    # Remove repositories and their language/topic links.  No FK cascade
    # exists between repositories and repository_languages/topics, so the
    # join tables must be cleared first.
    conn.execute(
        f"""
        DELETE FROM repository_languages
        WHERE repository_id IN (
            SELECT r.id FROM repositories r
            JOIN user_current_status c ON c.username = r.user_id
            WHERE c.status = 'DELETED'
        )
        """
    )
    conn.execute(
        f"""
        DELETE FROM repository_topics
        WHERE repository_id IN (
            SELECT r.id FROM repositories r
            JOIN user_current_status c ON c.username = r.user_id
            WHERE c.status = 'DELETED'
        )
        """
    )
    conn.execute(
        f"""
        DELETE FROM repositories
        WHERE user_id IN ({deleted})
        """
    )

    # Remove company links for deleted users.
    conn.execute(
        f"""
        DELETE FROM user_companies
        WHERE username IN ({deleted})
        """
    )

    conn.commit()


def down(conn):
    """No-op — sanitization removes data and cannot be reversed."""
    conn.commit()
