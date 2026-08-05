"""User-timezone helpers.

The dashboard stores the user's timezone as an IANA name in the
``settings`` table (key ``timezone``).  Everything that needs a *calendar
day* — the daily follow-limit counter, the Overview day windows, the
activity timeline buckets, the midnight snapshot and the followers chart
labels — derives its day boundary from this zone, so e.g. a UTC+3 user
sees the day roll at 00:00 local instead of 03:00 (UTC midnight).

Storage format: IANA name (standard for web apps — handles DST).  The
built-in default when unset is ``UTC``; the dashboard UI auto-detects
the browser timezone and saves it, but the value can always be set
manually.

All timestamp comparisons in the DB are UTC ISO strings, so the helpers
here convert between local calendar days and UTC instants.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"
_SETTINGS_KEY = "timezone"


def get_timezone_name(db):
    """IANA name stored in settings, or ``DEFAULT_TIMEZONE`` when unset."""
    try:
        name = db.get_setting(_SETTINGS_KEY)
    except Exception:
        # Settings table missing (migration not applied yet) — fall back.
        name = None
    if not name:
        return DEFAULT_TIMEZONE
    return name


def get_timezone(db):
    """``zoneinfo.ZoneInfo`` for the stored timezone.

    Never raises for bad data — an invalid stored name falls back to UTC.
    """
    try:
        return ZoneInfo(get_timezone_name(db))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def set_timezone(db, name):
    """Validate *name* as an IANA zone and persist it.

    Raises ``ZoneInfoNotFoundError`` for unknown names (the API layer
    turns that into a 400).  Returns the stored name.
    """
    ZoneInfo(name)  # validate before writing
    db.set_setting(_SETTINGS_KEY, name)
    return name


def utc_offset_minutes(db):
    """Current UTC offset (in minutes) of the user's timezone, e.g. 180."""
    tz = get_timezone(db)
    return int(datetime.now(tz).utcoffset().total_seconds() / 60)


def parse_utc(value):
    """Parse an ISO timestamp into an aware UTC datetime.

    Accepts strings with or without an explicit offset (naive values are
    assumed to be UTC, matching the codebase convention).
    """
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_midnight_utc(db, days_ago=0):
    """UTC instant of local midnight *days_ago* local days ago.

    ``days_ago=0`` → the start of the current local day; ``days_ago=1``
    → the start of yesterday.  Returns a tz-aware UTC datetime — ready to
    compare against the UTC ISO timestamps stored in the DB.
    """
    tz = get_timezone(db)
    local_now = datetime.now(tz)
    day = local_now.date() - timedelta(days=days_ago)
    return datetime(
        day.year, day.month, day.day, tzinfo=tz
    ).astimezone(timezone.utc)

