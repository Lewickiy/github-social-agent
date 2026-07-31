# GitHub Social Agent

A Python tool for discovering, analysing, and following GitHub users who match
your technology stack and interests.  It traverses follower graphs, collects
repository data (languages, topics, activity), scores developers by similarity
to your own profile, and can automatically follow the best matches.

On every startup the tool automatically syncs the owner's repos, languages,
and topics from GitHub so that similarity scoring always uses fresh baseline
data — no manual setup step required.

---

## Features

| Feature | Description |
|---|---|
| **Follower-graph discovery** | Starting from your account, discovers users through follower chains |
| **Repository collection** | Fetches repos, languages, topics, and activity recency for every user |
| **Owner-based scoring (0–100)** | Compares each user against your own GitHub profile (auto-synced on startup) |
| **Silent mode** | Stealth collection with human-like delays and jitter to avoid detection |
| **Rate-limit resilience** | Automatic retry (3 × 5/10/15 min + 2 h cooldown) on HTTP 401/403 |
| **Graceful shutdown** | Ctrl+C finishes the current user, commits all data, then exits |
| **File logging** | All operational events logged to `github_social.log` |
| **Automatic following** | Follows top-scored users respecting a configurable daily limit |

---

## Quick start

```bash
# 1. Install dependencies
pip install requests

# 2. Set your GitHub token (or edit config.py)
export GITHUB_TOKEN="ghp_..."

# 3. Initialise the database
python main.py --migrate

# 4. Discover users through your follower graph
python main.py --collect-users

# 5. Collect repos, languages, and topics for all discovered users
python main.py --collect-users-rep

# 6. Score everyone (0–100 based on similarity to you)
python main.py --score

# 7. See the top matches
python main.py --top

# 8. Follow the best matches
python main.py --follow
```

> **Note:** Your own profile (repos, languages, topics) is synced automatically
> on every startup — you don't need to run `--collect-self` manually.  It is
> still available as an explicit command if you want to force a refresh.

Or use the **all-in-one silent mode** (steps 6 + 7 with stealth delays):

```bash
python main.py --silent
```

---

## Commands

| Command | Description |
|---|---|
| `--collect-self` | Collect your own repos, languages, and topics. Marks you as the **owner** — the baseline for all scoring. |
| `--collect-users` | Phase 1: traverse your follower graph and discover new users. |
| `--collect-users-rep` | Phase 2: fetch repos, languages, and topics for every known user. |
| `--collect` | Run both phases (discover + collect repos). |
| `--silent` | Collect repos + score with human-like delays (stealth mode). |
| `--score` | Score / re-score all users against your profile. |
| `--follow` | Follow top-scored users (respects daily limit). |
| `--top` | Print the top 50 unscored users. |
| `--profile USER` | Show a detailed developer profile. |
| `--migrate` | Apply pending database migrations. |
| `--migrate-status` | Show which migrations have been applied. |

---

## Scoring algorithm (0–100)

The score has two parts:

### Base score (0–35)

| Criterion | Points |
|---|---|
| Repositories > 5 | +5 |
| Repositories > 20 | +10 (total 15) |
| Followers > 20 | +5 |
| Followers > 100 | +10 (total 15) |
| Has a bio | +5 |

### Similarity score (0–65) — owner data

Compares each user to **your** collected profile (repos, languages, topics)
which is synced from GitHub automatically on every startup.

| Criterion | Max pts | Method |
|---|---|---|
| **Language similarity** | 30 | Histogram intersection of language distributions |
| **Topic similarity** | 20 | Jaccard index of topic sets |
| **Repo recency** | 15 | Days since most recently updated repository |

#### Language similarity (histogram intersection)

Both you and the target have a language distribution (percentages summing to
100).  The score is the sum of per-language minimums:

```
overlap = Σ  min(your_pct[lang], their_pct[lang])
language_score = int((overlap / 100) * 30)
```

#### Topic similarity (Jaccard index)

```
jaccard = |your_topics ∩ their_topics| / |your_topics ∪ their_topics|
topic_score = int(jaccard * 20)
```

#### Repository recency

| Days since last repo update | Points |
|---|---|
| ≤ 7 | 15 |
| ≤ 30 | 10 |
| ≤ 90 | 5 |
| > 90 | 0 |

---

## Configuration (`config.py`)

```python
# GitHub API
TOKEN        = os.environ.get("GITHUB_TOKEN", "ghp_...")
MY_USERNAME  = "Lewickiy"          # Your GitHub username
API          = "https://api.github.com"

# Bot settings
DAILY_FOLLOW_LIMIT = 90            # Max follows per day
FOLLOW_DELAY       = 60            # Seconds between follow actions
DATABASE           = "data/github_social.db"
LOG_FILE           = "github_social.log"

# Silent mode — human-like intervals (seconds)
SILENT_DELAY_BETWEEN_USERS    = 120  # Between users (2 min)
SILENT_DELAY_BETWEEN_REPOS    = 15   # Between repos of one user
SILENT_DELAY_BETWEEN_REQUESTS = 5    # Between API calls within a repo
SILENT_DELAY_BETWEEN_SCORES   = 30   # Between scoring users
```

### Environment variables

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | Personal access token (overrides `config.py` default) |

---

## Database

SQLite database (`data/github_social.db`) with the following tables:

The database lives in its own `data/` directory.  In Docker the whole
`./data` folder is mounted as `/app/data`, so the SQLite WAL/SHM files always
land next to the DB file on the host — the container, host scripts, and
PyCharm/DBeaver all share the exact same files.

| Table | Description |
|---|---|
| `users` | Discovered users, scores, owner flag |
| `actions` | Follow/unfollow log |
| `repositories` | Repos per user |
| `languages` | Unique language names |
| `repository_languages` | M2M: repo ↔ language (with weight %) |
| `topics` | Unique topic names |
| `repository_topics` | M2M: repo ↔ topic |
| `migrations` | Migration tracking |

### Migrations

```bash
python main.py --migrate          # apply pending
python main.py --migrate-status   # show status
```

---

## Logging

All operational events (rate limits, retries, scoring, shutdowns) are logged at
DEBUG level to `github_social.log` (in `./logs/` when running under Docker, via
`/app/logs/github_social.log`).  The console shows only WARNING and above.

---

## Graceful shutdown

Press **Ctrl+C** once → the program finishes the current user, commits all data,
and exits cleanly.

Press **Ctrl+C** twice → force exit immediately.

`SIGTERM` is handled the same as the first `Ctrl+C`.

---

## Typical workflow

```bash
# First run — initialise the database
python main.py --migrate

# Discover users from your follower network
python main.py --collect-users

# Stealth-collect repos + score (background-friendly)
python main.py --silent

# Check results
python main.py --top
python main.py --profile some_user

# Follow the best matches
python main.py --follow
```

> Your owner profile is synced from GitHub automatically on every command —
> no manual `--collect-self` step needed.

> **Note (Docker):** make sure `./data` and `./logs` exist and are writable by
> the container user before `docker compose up` (`mkdir -p data logs`).  Docker
> creates missing bind-mount sources as root, which would block writes from the
> container (UID 1000).
