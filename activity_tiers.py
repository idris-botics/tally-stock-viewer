"""Tiered storage for the user_activity log (Usage Charts).

  raw rows      user_activity, kept for the raw window (ACTIVITY_RETENTION_DAYS,
                default 90). Full detail; the charts use them for ranges up to the window.
  summaries     activity_daily / activity_hourly / activity_top_monthly -- tiny,
                permanent, computed once per finished IST day / month. The charts use
                them for 6 months / 1 year / All time.
  archive       whole IST months older than the raw window go to the separate Drive
                folder (activity_archive.py) and only then leave the table.

Rules this module guarantees:
  * A day is summarised only after that IST day has fully passed, and only once
    (a marker row in activity_summary_done); an existing summary is never recomputed,
    so it can never be overwritten by a recomputation over already-pruned rows.
  * Every day is committed in its own transaction: an interrupted backfill simply
    resumes with the days that have no marker.
  * Visits use the one shared rule (database.ACTIVITY_VISIT_GAP_MINUTES) -- the
    summary stores visit counts per (day, dealer) under the visit's START day.
  * The hourly and top-car/search summaries cover dealers only (role='customer' at
    the time the day is summarised) so admin testing never pollutes them.
    activity_daily keeps every user so the role split stays possible later.
  * Nothing here logs car names or search text.
"""
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone

import database as db

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(minutes=330)
TOP_N = 50
DEFAULT_RETENTION_DAYS = 90
_ensure_lock = threading.Lock()


def retention_days():
    """Raw window in days (env ACTIVITY_RETENTION_DAYS, default 90, never below 7)."""
    try:
        value = int(os.environ.get("ACTIVITY_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        value = DEFAULT_RETENTION_DAYS
    return max(7, value)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def ist_today(now_utc=None):
    return ((now_utc or _utc_now()) + IST_OFFSET).date()


def day_bounds_utc(day):
    """[start, end) of an IST calendar day as naive-UTC ISO strings. IST midnight
    is 18:30 UTC the evening before, so 18:29 UTC belongs to the earlier IST day
    and 18:31 UTC to the next."""
    d = day if isinstance(day, date) else date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day) - IST_OFFSET
    return _iso(start), _iso(start + timedelta(days=1))


def month_bounds_utc(month):
    """[start, end) of an IST calendar month ('YYYY-MM') as naive-UTC ISO strings."""
    y, m = (int(x) for x in month.split("-"))
    first = datetime(y, m, 1) - IST_OFFSET
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return _iso(first), _iso(datetime(ny, nm, 1) - IST_OFFSET)


def month_of(day):
    d = day if isinstance(day, date) else date.fromisoformat(day)
    return f"{d.year:04d}-{d.month:02d}"


def days_in_month(month):
    y, m = (int(x) for x in month.split("-"))
    d = date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d)
        d += timedelta(days=1)
    return out


def _done_keys(conn, prefix):
    return {r[0][len(prefix):] for r in conn.execute(
        "SELECT key FROM activity_summary_done WHERE key LIKE ?", (prefix + "%",))}


def compute_day(day, now_utc=None):
    """Summarise one finished IST day. Returns False (and changes nothing) when the
    day is not over yet or already has a summary. All writes happen in one transaction
    together with the marker, so a crash leaves either the whole day or nothing."""
    d = day if isinstance(day, date) else date.fromisoformat(day)
    if d >= ist_today(now_utc):
        return False
    key = "d:" + d.isoformat()
    start, end = day_bounds_utc(d)
    with db.get_connection() as conn:
        if conn.execute("SELECT 1 FROM activity_summary_done WHERE key = ?", (key,)).fetchone():
            return False
        counts = {}
        hours = {}
        for user_id, action, hour, role in conn.execute(
            "SELECT a.user_id, a.action, CAST(strftime('%H', a.at_utc, ?) AS INTEGER), u.role "
            "FROM user_activity a LEFT JOIN users u ON u.id = a.user_id "
            "WHERE a.at_utc >= ? AND a.at_utc < ?", (db.IST_SQL, start, end),
        ):
            counts[(user_id, action)] = counts.get((user_id, action), 0) + 1
            if role == "customer":
                hours[hour] = hours.get(hour, 0) + 1
        lookback = _iso(datetime.fromisoformat(start) - timedelta(minutes=db.ACTIVITY_VISIT_GAP_MINUTES))
        visits = {}
        for user_id, _d, n in conn.execute(
            db.visit_starts_sql("at_utc < ?", group_by_user=True),
            (lookback, end, start, db.ACTIVITY_VISIT_GAP_MINUTES * 60),
        ):
            visits[user_id] = n
        conn.execute("DELETE FROM activity_daily WHERE day_ist = ?", (d.isoformat(),))
        conn.execute("DELETE FROM activity_hourly WHERE day_ist = ?", (d.isoformat(),))
        conn.executemany(
            "INSERT INTO activity_daily (day_ist, user_id, action, count, visits) VALUES (?, ?, ?, ?, 0)",
            [(d.isoformat(), uid, act, n) for (uid, act), n in counts.items()],
        )
        # Visit counts live on a sentinel row (action = '') per dealer-day, so
        # SUM(count) and SUM(visits) are both plain sums.
        conn.executemany(
            "INSERT INTO activity_daily (day_ist, user_id, action, count, visits) VALUES (?, ?, '', 0, ?)",
            [(d.isoformat(), uid, n) for uid, n in visits.items()],
        )
        conn.executemany(
            "INSERT INTO activity_hourly (day_ist, hour_ist, count) VALUES (?, ?, ?)",
            [(d.isoformat(), h, n) for h, n in hours.items()],
        )
        conn.execute(
            "INSERT INTO activity_summary_done (key, computed_at) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET computed_at = excluded.computed_at",
            (key, _iso(now_utc or _utc_now())),
        )
    return True


def compute_month_tops(month, final, now_utc=None):
    """Top 50 cars and searches (dealers only) for one IST month. Safe to redo while the
    month is still running (replaced in one transaction); once `final`, a marker stops
    any later recomputation."""
    start, end = month_bounds_utc(month)
    with db.get_connection() as conn:
        if conn.execute("SELECT 1 FROM activity_summary_done WHERE key = ?", ("m:" + month,)).fetchone():
            return False
        dealer = "a.user_id IN (SELECT id FROM users WHERE role = 'customer')"
        cars = conn.execute(
            f"SELECT a.detail, COUNT(*) AS n FROM user_activity a WHERE a.at_utc >= ? AND a.at_utc < ? AND {dealer} "
            "AND a.action = 'view_car' AND a.detail IS NOT NULL AND a.detail != '' "
            "GROUP BY a.detail ORDER BY n DESC, a.detail LIMIT ?", (start, end, TOP_N)).fetchall()
        searches = conn.execute(
            f"SELECT LOWER(TRIM(a.detail)) AS q, COUNT(*) AS n FROM user_activity a WHERE a.at_utc >= ? AND a.at_utc < ? AND {dealer} "
            "AND a.action = 'search' AND a.detail IS NOT NULL AND TRIM(a.detail) != '' "
            "GROUP BY q ORDER BY n DESC, q LIMIT ?", (start, end, TOP_N)).fetchall()
        conn.execute("DELETE FROM activity_top_monthly WHERE month = ?", (month,))
        conn.executemany(
            "INSERT INTO activity_top_monthly (month, kind, label, count) VALUES (?, ?, ?, ?)",
            [(month, "car", r[0], r[1]) for r in cars] + [(month, "search", r[0], r[1]) for r in searches],
        )
        if final:
            conn.execute("INSERT INTO activity_summary_done (key, computed_at) VALUES (?, ?) "
                         "ON CONFLICT(key) DO NOTHING", ("m:" + month, _iso(now_utc or _utc_now())))
    return True


def ensure_summaries(now_utc=None, max_days=None, quick=False):
    """Backfill / catch up: summarise every finished IST day that has no marker (from the
    oldest raw row up to yesterday), and refresh the monthly tops. Idempotent and resumable;
    never loads a table into memory (one day at a time). Returns {'days': n, 'months': n}.
    quick=True (used per chart request) returns at once when yesterday is already summarised
    and leaves the monthly tops to the daily run."""
    now = now_utc or _utc_now()
    with _ensure_lock:
        done_days = months_done = 0
        if quick:
            with db.get_connection() as conn:
                if conn.execute("SELECT 1 FROM activity_summary_done WHERE key = ?",
                                ("d:" + (ist_today(now) - timedelta(days=1)).isoformat(),)).fetchone():
                    return {"days": 0, "months": 0}
        with db.get_connection() as conn:
            first = conn.execute("SELECT MIN(at_utc) FROM user_activity").fetchone()[0]
            marked_days = _done_keys(conn, "d:")
            marked_months = _done_keys(conn, "m:")
        if not first:
            return {"days": 0, "months": 0}
        today = ist_today(now)
        day = (datetime.fromisoformat(first) + IST_OFFSET).date()
        while day < today:
            if day.isoformat() not in marked_days:
                if max_days is not None and done_days >= max_days:
                    return {"days": done_days, "months": months_done}
                if compute_day(day, now):
                    done_days += 1
            day += timedelta(days=1)
        if quick:
            return {"days": done_days, "months": 0}
        month = month_of((datetime.fromisoformat(first) + IST_OFFSET).date())
        current = month_of(today)
        while month <= current:
            if month not in marked_months:
                final = month < current   # the month has completely passed
                if compute_month_tops(month, final, now):
                    months_done += 1
            y, m = (int(x) for x in month.split("-"))
            month = f"{y + (m == 12):04d}-{(m % 12) + 1:02d}"
        return {"days": done_days, "months": months_done}


def month_summary_gaps(conn, month):
    """What is still missing before this month's raw rows may be removed: the IST days
    that have raw rows but no daily summary, plus the month's tops marker. Empty list =
    safe."""
    start, end = month_bounds_utc(month)
    have = _done_keys(conn, "d:")
    gaps = [r[0] for r in conn.execute(
        "SELECT DISTINCT date(at_utc, ?) FROM user_activity WHERE at_utc >= ? AND at_utc < ?",
        (db.IST_SQL, start, end)) if r[0] not in have]
    if month not in _done_keys(conn, "m:"):
        gaps.append("month:" + month)
    return sorted(gaps)


# ---------------------------------------------------------------------------
# Reads for the Usage Charts panel (summary tiers). Same shape as the raw path.
# ---------------------------------------------------------------------------

def first_summarised_day():
    with db.get_connection() as conn:
        row = conn.execute("SELECT MIN(day_ist) FROM activity_daily").fetchone()
    return row[0]


def get_summary_stats(first_day, last_day, user_id=None, weekly=False):
    """Aggregates from the summaries for IST days first_day..last_day inclusive (ISO
    strings). user_id=None: all dealers (role='customer', plus dealers since deleted);
    else that one user. Returns the same keys as database.get_activity_stats() plus
    'hours'=None when it cannot be answered (hourly is not stored per dealer)."""
    if user_id is None:
        user_sql, user_args = "(u.id IS NULL OR u.role = 'customer')", ()
        scope_sql, scope_args = "u.role = 'customer'", ()
    else:
        user_sql, user_args = "d.user_id = ?", (int(user_id),)
        scope_sql, scope_args = "u.id = ?", (int(user_id),)
    bucket = ("date(d.day_ist, '-' || ((CAST(strftime('%w', d.day_ist) AS INTEGER) + 6) % 7) || ' days')"
              if weekly else "d.day_ist")
    rng = "d.day_ist >= ? AND d.day_ist <= ?"
    with db.get_connection() as conn:
        daily = conn.execute(
            f"SELECT {bucket} AS b, SUM(d.count), COUNT(DISTINCT d.user_id), SUM(d.visits) "
            "FROM activity_daily d LEFT JOIN users u ON u.id = d.user_id "
            f"WHERE {rng} AND {user_sql} GROUP BY b", (first_day, last_day) + user_args).fetchall()
        hours = None
        if user_id is None:
            hours = conn.execute(
                "SELECT hour_ist, SUM(count) FROM activity_hourly WHERE day_ist >= ? AND day_ist <= ? GROUP BY hour_ist",
                (first_day, last_day)).fetchall()
        per_user = conn.execute(
            "SELECT u.id, u.username, d.action, SUM(d.count) FROM users u "
            "LEFT JOIN activity_daily d ON d.user_id = u.id AND d.day_ist >= ? AND d.day_ist <= ? AND d.action != '' "
            f"WHERE {scope_sql} GROUP BY u.id, d.action", (first_day, last_day) + scope_args).fetchall()
        gone = []
        if user_id is None:   # dealers deleted since: their history stays in the totals
            gone = conn.execute(
                "SELECT d.user_id, d.action, SUM(d.count) FROM activity_daily d "
                "WHERE d.day_ist >= ? AND d.day_ist <= ? AND d.action != '' "
                "AND d.user_id NOT IN (SELECT id FROM users) GROUP BY d.user_id, d.action",
                (first_day, last_day)).fetchall()
        cars = searches = None
        if user_id is None:
            months = (month_of(first_day), month_of(last_day))
            cars = conn.execute(
                "SELECT label, SUM(count) AS n FROM activity_top_monthly WHERE kind = 'car' AND month >= ? AND month <= ? "
                "GROUP BY label ORDER BY n DESC, label LIMIT 10", months).fetchall()
            searches = conn.execute(
                "SELECT label, SUM(count) AS n FROM activity_top_monthly WHERE kind = 'search' AND month >= ? AND month <= ? "
                "GROUP BY label ORDER BY n DESC, label LIMIT 10", months).fetchall()
    per_user_rows = [tuple(r) for r in per_user] + [(uid, f"(deleted dealer #{uid})", act, n) for uid, act, n in gone]
    return {
        "daily": [tuple(r) for r in daily],
        "hours": None if hours is None else [tuple(r) for r in hours],
        "per_user": per_user_rows,
        "cars": None if cars is None else [tuple(r) for r in cars],
        "searches": None if searches is None else [tuple(r) for r in searches],
    }
