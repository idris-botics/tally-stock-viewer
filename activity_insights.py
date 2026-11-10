"""Extra panels for the Usage Charts: weekday x hour heatmap, weekday comparison (Sunday vs the rest),
new vs returning dealers per week, dormant dealers, and the previous-period comparison.

Read-only, every query aggregated and parameterised, no raw rows leave the server. Works from the
permanent summaries (activity_daily / activity_hourly) and so covers FINISHED IST days only (through
yesterday); the one exception is "today" in the dormant list, where a dealer who is active right now is
obviously not dormant. Dealers only (role='customer', plus dealers deleted since): admin activity is
excluded and counted separately.

This module deliberately knows nothing about the raw-window/archive code: the endpoint calls build()
inside a try/except, so a failure here can only hide these panels, never the charts.
"""
from datetime import date, datetime, timedelta, timezone

import database as db

IST = timedelta(minutes=330)
DORMANT_DAYS = 14
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_LIST = 60

_DEALER = "(u.id IS NULL OR u.role = 'customer')"
_MONDAY = "date({col}, '-' || ((CAST(strftime('%w', {col}) AS INTEGER) + 6) % 7) || ' days')"


def _today(now_utc=None):
    return ((now_utc or datetime.now(timezone.utc).replace(tzinfo=None)) + IST).date()


def _d(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def _monday(day):
    return day - timedelta(days=day.weekday())


def first_tracked_day():
    """The first IST day anything was recorded (summaries or raw rows), or None. Charts start here
    instead of showing weeks of zeros from before tracking began."""
    with db.get_connection() as conn:
        summ = conn.execute("SELECT MIN(day_ist) FROM activity_daily").fetchone()[0]
        raw = conn.execute("SELECT MIN(at_utc) FROM user_activity").fetchone()[0]
    days = []
    if summ:
        days.append(date.fromisoformat(summ))
    if raw:
        days.append((datetime.fromisoformat(raw) + IST).date())
    return min(days) if days else None


def weekday_comparison(rows, first_day, last_day):
    """rows: [(day_iso, actions, active_dealers, visits)] for finished days. One entry per
    weekday with its average per calendar day (days with no activity count as zero)."""
    per = [{"actions": 0, "dealer_days": 0, "visits": 0, "days": 0} for _ in range(7)]
    d = first_day
    while d <= last_day:
        per[d.weekday()]["days"] += 1
        d += timedelta(days=1)
    for day, actions, dealers, visits in rows:
        w = per[_d(day).weekday()]
        w["actions"] += int(actions or 0)
        w["dealer_days"] += int(dealers or 0)
        w["visits"] += int(visits or 0)
    out = []
    for i, w in enumerate(per):
        n = w["days"]
        out.append({
            "weekday": WEEKDAY_NAMES[i], "days": n, "actions": w["actions"], "visits": w["visits"],
            "avg_actions": round(w["actions"] / n, 1) if n else None,
            "avg_dealers": round(w["dealer_days"] / n, 1) if n else None,
            "avg_visits": round(w["visits"] / n, 1) if n else None,
        })
    return out


def build(first_day, last_day, user_id=None, compare=True, now_utc=None):
    """first_day..last_day: the IST days the chart covers (last_day may be today). Returns a dict;
    panels that cannot be answered for the choice (e.g. heatmap for a single dealer) are None."""
    today = _today(now_utc)
    first_day, last_day = _d(first_day), _d(last_day)
    nominal_first = first_day                      # the previous-period comparison uses the requested window
    started = first_tracked_day()
    if started and started > first_day:
        first_day = started                        # no fake zero days from before tracking began
    fin_last = min(last_day, today - timedelta(days=1))
    out = {"as_of": today.isoformat(), "first_day": first_day.isoformat(), "last_day": fin_last.isoformat(),
           "finished_days_only": True, "weekdays": None, "heatmap": None, "new_returning": None,
           "dormant": None, "last_active": None, "previous": None, "admin_actions": 0}
    if user_id is None:
        scope_sql, scope_args = _DEALER, ()
    else:
        scope_sql, scope_args = "d.user_id = ?", (int(user_id),)
    a, b = first_day.isoformat(), fin_last.isoformat()

    with db.get_connection() as conn:
        if fin_last >= first_day:
            day_rows = conn.execute(
                "SELECT d.day_ist, SUM(d.count), COUNT(DISTINCT d.user_id), SUM(d.visits) FROM activity_daily d "
                f"LEFT JOIN users u ON u.id = d.user_id WHERE d.day_ist >= ? AND d.day_ist <= ? AND {scope_sql} GROUP BY d.day_ist",
                (a, b) + scope_args).fetchall()
            out["weekdays"] = weekday_comparison(day_rows, first_day, fin_last)
            out["admin_actions"] = int(conn.execute(
                "SELECT COALESCE(SUM(d.count), 0) FROM activity_daily d JOIN users u ON u.id = d.user_id "
                "WHERE d.day_ist >= ? AND d.day_ist <= ? AND u.role = 'admin'", (a, b)).fetchone()[0])

            if user_id is None:
                grid = [[0] * 24 for _ in range(7)]
                for wd, hour, n in conn.execute(
                    "SELECT CAST(strftime('%w', day_ist) AS INTEGER), hour_ist, SUM(count) FROM activity_hourly "
                    "WHERE day_ist >= ? AND day_ist <= ? GROUP BY 1, 2", (a, b)):
                    grid[(wd + 6) % 7][int(hour)] = int(n)
                out["heatmap"] = grid

                weeks = conn.execute(
                    "WITH ev AS (SELECT DISTINCT d.user_id AS uid, d.day_ist AS day FROM activity_daily d "
                    f"LEFT JOIN users u ON u.id = d.user_id WHERE d.action != '' AND {_DEALER}), "
                    "fst AS (SELECT uid, MIN(day) AS fday FROM ev GROUP BY uid), "
                    f"wk AS (SELECT DISTINCT uid, {_MONDAY.format(col='day')} AS w FROM ev WHERE day >= ? AND day <= ?) "
                    f"SELECT wk.w, SUM(CASE WHEN {_MONDAY.format(col='fst.fday')} = wk.w THEN 1 ELSE 0 END), COUNT(*) "
                    "FROM wk JOIN fst ON fst.uid = wk.uid GROUP BY wk.w ORDER BY wk.w", (a, b)).fetchall()
                by_week = {w: (int(n), int(t)) for w, n, t in weeks}
                series, w = [], _monday(first_day)
                while w <= fin_last:
                    n, t = by_week.get(w.isoformat(), (0, 0))
                    series.append({"week": w.isoformat(), "new": n, "returning": t - n})
                    w += timedelta(days=7)
                out["new_returning"] = series

        # last active (finished days from the summaries, plus "today" from the raw table)
        if user_id is None:
            last_by_user = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT u.id, u.username, MAX(d.day_ist) FROM users u LEFT JOIN activity_daily d "
                "ON d.user_id = u.id AND d.action != '' WHERE u.role = 'customer' GROUP BY u.id")}
            start_today = (datetime(today.year, today.month, today.day) - IST).isoformat(timespec="seconds")
            seen_today = {r[0] for r in conn.execute(
                "SELECT DISTINCT user_id FROM user_activity WHERE at_utc >= ? "
                "AND user_id IN (SELECT id FROM users WHERE role = 'customer')", (start_today,))}
            dormant, never, recent = [], [], 0
            for uid, (name, last) in last_by_user.items():
                if uid in seen_today:
                    last = today.isoformat()
                if not last:
                    never.append({"user_id": uid, "username": name})
                    continue
                ago = (today - date.fromisoformat(last)).days
                if ago >= DORMANT_DAYS:
                    dormant.append({"user_id": uid, "username": name, "last_active": last, "days_ago": ago})
                else:
                    recent += 1
            dormant.sort(key=lambda r: (r["days_ago"], r["username"].lower()))
            never.sort(key=lambda r: r["username"].lower())
            out["dormant"] = {"threshold_days": DORMANT_DAYS, "dormant_total": len(dormant), "dormant": dormant[:MAX_LIST],
                              "never_total": len(never), "never_active": never[:MAX_LIST], "recent": recent}
        else:
            row = conn.execute("SELECT MAX(day_ist) FROM activity_daily WHERE user_id = ? AND action != ''", (int(user_id),)).fetchone()
            start_today = (datetime(today.year, today.month, today.day) - IST).isoformat(timespec="seconds")
            if conn.execute("SELECT 1 FROM user_activity WHERE user_id = ? AND at_utc >= ? LIMIT 1", (int(user_id), start_today)).fetchone():
                last = today.isoformat()
            else:
                last = row[0]
            out["last_active"] = {"date": last, "days_ago": (today - date.fromisoformat(last)).days} if last else {"date": None, "days_ago": None}

        # previous period of the same length immediately before the first day
        if compare:
            length = (last_day - nominal_first).days + 1
            p_last, p_first = nominal_first - timedelta(days=1), nominal_first - timedelta(days=length)
            covered = conn.execute(
                "SELECT COUNT(*) FROM activity_summary_done WHERE key >= ? AND key <= ?",
                ("d:" + p_first.isoformat(), "d:" + p_last.isoformat())).fetchone()[0]
            if covered:
                tot = conn.execute(
                    "SELECT COALESCE(SUM(d.count), 0), COUNT(DISTINCT CASE WHEN d.action != '' THEN d.user_id END), COALESCE(SUM(d.visits), 0) "
                    f"FROM activity_daily d LEFT JOIN users u ON u.id = d.user_id WHERE d.day_ist >= ? AND d.day_ist <= ? AND {scope_sql}",
                    (p_first.isoformat(), p_last.isoformat()) + scope_args).fetchone()
                out["previous"] = {"first_day": p_first.isoformat(), "last_day": p_last.isoformat(), "days": length,
                                   "days_with_data": int(covered), "total_actions": int(tot[0]), "active_users": int(tot[1]), "visits": int(tot[2])}
    return out
