"""Migration 012 — Backfill companies from existing users.company field.

Scans all users whose company field contains @-prefixed GitHub
organization references and creates companies + user_companies
entries for them.  api_fetched_at is left NULL so the background
worker will pick them up for data enrichment.
"""

import re
from datetime import datetime, timezone

_COMPANY_RE = re.compile(r"@([a-zA-Z0-9_-]+)")


def up(conn):
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT username, company FROM users "
        "WHERE company IS NOT NULL AND company LIKE '%@%'"
    ).fetchall()

    added_companies = 0
    added_links = 0

    for username, company_text in rows:
        for match in _COMPANY_RE.finditer(company_text):
            login = match.group(1).lower()

            # Insert company if missing
            cur = conn.execute(
                "INSERT OR IGNORE INTO companies (login, created_at) VALUES (?, ?)",
                (login, now),
            )
            if cur.rowcount > 0:
                added_companies += 1

            comp_id = conn.execute(
                "SELECT id FROM companies WHERE login = ?", (login,)
            ).fetchone()[0]

            # Link user ↔ company
            cur = conn.execute(
                "INSERT OR IGNORE INTO user_companies (username, company_id) VALUES (?, ?)",
                (username, comp_id),
            )
            if cur.rowcount > 0:
                added_links += 1

    conn.commit()
    print(f"  Backfill: {added_companies} companies, {added_links} user-company links.")


def down(conn):
    conn.execute("DELETE FROM user_companies")
    conn.execute("DELETE FROM companies")
    conn.commit()
