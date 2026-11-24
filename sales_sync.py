"""Shop-side sales sync: reads SALES vouchers and CREDIT NOTES from Tally (tally_sales.py), one CALENDAR MONTH at a time, and pushes
them to the website's POST /admin/intake/sales, gently, one small step per feeder cycle. Called from feeder.py once per cycle
(SalesSync.step).

What is pushed, and nothing else: an anonymous voucher key, the day, "sale" or "return", the item name, a whole-number quantity and
a hash of the company name. A push covers a whole calendar month (first to last day, also for the current month) and REPLACES the
website's lines for those days, so repeating a push changes nothing.

Order of work (at most ONE step per cycle; the first cycle after startup does nothing):
  a. INCREMENTAL  the current month, every SALES_INCREMENTAL_MINUTES (a fresh state counts as due: the first step ever)
  b. BACKFILL     one earlier month per step, NEWEST FIRST (last month, the month before, ...), at least 2 cycles after the previous
                  one, down to the month that contains (today - SALES_BACKFILL_DAYS)
  c. NIGHTLY      the current month and the 2 before it, once a day after SALES_NIGHTLY_HOUR (heals edited and deleted vouchers)
Because history is loaded newest-first, the website always holds ONE unbroken stretch of months that ends with the current month.

The website only receives what changed: after reading a month a fingerprint of its lines is compared with the one stored for that
month, and an unchanged month is not pushed again (except once every 24 hours).

Nothing runs on import: no thread, no network call. Creating a SalesSync only reads its small state file.
"""
import calendar
import copy
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("feeder")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "sales_sync_state.json")
STATE_VERSION = 2

NIGHTLY_MONTHS = 3                    # nightly re-reads the current month and the 2 before it
MIN_CYCLES_BETWEEN_MONTHS = 2
SLOW_READ_SECONDS, FAST_READ_SECONDS = 15.0, 10.0   # a month read slower than this is slow: the incremental interval doubles
MAX_QUIET_SECONDS = 2 * 3600          # the current month is read at least this often even when the stock did not move
DEFAULT_SHOP_HOURS = "10:00-19:30"    # Monday to Saturday; history and the nightly re-check wait for the end of the shop day
MAX_INTERVAL_MINUTES = 240            # the slow guard never stretches the incremental beyond 4 hours
MAX_INTERVAL_FACTOR = 64
REPUSH_AFTER_SECONDS = 24 * 3600      # an unchanged month is still pushed once in 24 hours
MAX_PUSH_LINES = 25000                # the website accepts 30,000
MAX_PUSH_BYTES = 2400000              # the website accepts 3 MB
PAUSE_404_SECONDS = 3600
PAUSE_409_SECONDS = 24 * 3600
MAX_BACKOFF_CYCLES = 60
LOG_AGAIN_SECONDS = 3600

STATE_KEYS = ("version", "backfill_done", "backfill_cursor", "backfill_floor", "months", "last_incremental_utc", "last_nightly_day",
              "company", "website_missing_until_utc", "blocked_until_utc", "fail_streak")
_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


@dataclass
class SalesSettings:
    mode: str = "on"                  # on | off | dryrun
    backfill_days: int = 365
    incremental_minutes: int = 30
    nightly_hour: int = 3
    shop_hours: str = DEFAULT_SHOP_HOURS    # "" = no limit

    @classmethod
    def from_environment(cls):
        def number(name, default, low, high, too_low_means_default=False):
            try:
                value = int(float(os.environ.get(name, "").strip()))
            except ValueError:
                return default
            if too_low_means_default and value < low:
                return default
            return max(low, min(high, value))

        mode = os.environ.get("SALES_SYNC", "on").strip().lower()
        if mode not in ("on", "off", "dryrun"):
            mode = "on"
        try:
            nightly = int(float(os.environ.get("SALES_NIGHTLY_HOUR", "").strip()))
        except ValueError:
            nightly = 3
        hours = os.environ.get("SALES_SHOP_HOURS", DEFAULT_SHOP_HOURS).strip()
        try:
            parse_shop_hours(hours)
        except ValueError:
            hours = DEFAULT_SHOP_HOURS
        return cls(
            mode=mode,
            backfill_days=number("SALES_BACKFILL_DAYS", 365, 1, 400),
            incremental_minutes=number("SALES_INCREMENTAL_MINUTES", 30, 1, 1440, too_low_means_default=True),
            nightly_hour=nightly if 0 <= nightly <= 23 else 3,
            shop_hours=hours)


def parse_shop_hours(text):
    """(opens, closes) as times from "10:00-19:30", or None for an empty text (no limit). Raises ValueError for anything else."""
    if not text or not text.strip():
        return None
    opens, closes = (datetime.strptime(part.strip(), "%H:%M").time() for part in text.split("-"))
    if opens >= closes:
        raise ValueError("the shop opens after it closes")
    return opens, closes


def sales_url(sync_url):
    """The sales intake address, derived from the stock push address (CLOUD_SYNC_URL), keeping the scheme, host and any prefix
    in front of /admin/intake/."""
    parts = urlsplit(sync_url or "")
    index = parts.path.find("/admin/intake/")
    prefix = parts.path[:index] if index >= 0 else ""
    return urlunsplit((parts.scheme, parts.netloc, prefix + "/admin/intake/sales", "", ""))


def make_default_pusher():
    """The real push: requests.post to the sales address with the same token as the stock push. Never sends a force header."""
    import requests
    from config import Config
    url, token = sales_url(Config.CLOUD_SYNC_URL), Config.CLOUD_SYNC_TOKEN

    def push(payload):
        response = requests.post(url, json=payload, headers={"X-Intake-Token": token}, timeout=60)
        return response.status_code, response.text
    return push


class RealClock:
    def monotonic(self):
        return time.monotonic()

    def utc_now(self):
        return datetime.now(timezone.utc)

    def local_now(self):
        return datetime.now()


# ------------------------------------------------------------------------------------------------ months
def month_key(year, month):
    return f"{year:04d}-{month:02d}"


def parse_month(key):
    year, month = key.split("-")
    return int(year), int(month)


def previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_bounds(year, month):
    """(first day, last day) of the calendar month."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def fingerprint(lines):
    """A hash of a month's lines (sorted, so the order Tally answers in does not matter)."""
    rows = sorted(f"{line['voucher_key']}|{line['day'].isoformat()}|{line['kind']}|{line['item']}|{line['qty']}" for line in lines)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _parse_utc(text):
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_month(text):
    return isinstance(text, str) and _MONTH_RE.fullmatch(text) is not None


def _is_date(text):
    try:
        date.fromisoformat(text)
        return True
    except (TypeError, ValueError):
        return False


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _months_are_valid(value):
    return isinstance(value, dict) and all(
        _is_month(key) and isinstance(entry, dict) and isinstance(entry.get("fingerprint"), str) and _parse_utc(entry.get("pushed_utc")) is not None
        for key, entry in value.items())


def _state_is_valid(data):
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return False
    checks = {
        "backfill_done": lambda v: isinstance(v, bool),
        "backfill_cursor": _is_month, "backfill_floor": _is_month, "months": _months_are_valid,
        "last_incremental_utc": lambda v: v is None or _parse_utc(v) is not None,
        "last_nightly_day": lambda v: v is None or _is_date(v),
        "company": lambda v: v is None or isinstance(v, str),
        "website_missing_until_utc": lambda v: v is None or _parse_utc(v) is not None,
        "blocked_until_utc": lambda v: v is None or _parse_utc(v) is not None,
        "fail_streak": lambda v: _is_int(v) and v >= 0,
    }
    return all(checks[key](data[key]) for key in checks if key in data)


def _line_payload(line):
    return {"v": line["voucher_key"], "d": line["day"].isoformat(), "k": line["kind"], "i": line["item"], "q": line["qty"]}


def split_range(lines, day_from, day_to):
    """[(from, to, [payload lines])]: contiguous date sub-ranges that together tile day_from..day_to exactly, each with at most
    MAX_PUSH_LINES lines and about MAX_PUSH_BYTES of JSON. One piece unless the month is large. Newest piece first."""
    by_day = {}
    for line in lines:
        if day_from <= line["day"] <= day_to:
            by_day.setdefault(line["day"], []).append(_line_payload(line))
    pieces, start, current, size = [], day_from, [], 0
    day = day_from
    while day <= day_to:
        todays = by_day.get(day, [])
        todays_size = sum(len(json.dumps(item)) + 2 for item in todays)
        if current and (len(current) + len(todays) > MAX_PUSH_LINES or size + todays_size > MAX_PUSH_BYTES):
            pieces.append((start, day - timedelta(days=1), current))
            start, current, size = day, [], 0
        current += todays
        size += todays_size
        day += timedelta(days=1)
    pieces.append((start, day_to, current))
    pieces.reverse()
    return pieces


class SalesSync:
    def __init__(self, state_path=STATE_PATH, reader=None, pusher=None, settings=None, clock=None):
        self.state_path = state_path
        self.settings = settings or SalesSettings.from_environment()
        self.clock = clock or RealClock()
        self._reader = reader
        self._pusher = pusher
        self._started = False
        self._skip_cycles = 0
        self._since_month = MIN_CYCLES_BETWEEN_MONTHS
        self._interval_factor = 1
        self._stock_moved = True                        # the very first current-month read happens at once
        self._last_logged = {}
        self._off_logged = False
        self.state = self._load_state()                 # the REAL state: only a real ("on") step ever changes or saves it
        self._dry_state = None                          # a dry run walks this throw-away copy instead

    @classmethod
    def from_environment(cls):
        return cls()

    # ---------------------------------------------------------------- logging
    def _log_limited(self, key, message, *args, level=logging.INFO, seconds=LOG_AGAIN_SECONDS):
        now = self.clock.monotonic()
        last = self._last_logged.get(key)
        if last is None or now - last >= seconds:
            self._last_logged[key] = now
            logger.log(level, message, *args)

    # ---------------------------------------------------------------- state
    def _fresh_state(self):
        today = self.clock.local_now().date()
        floor_day = today - timedelta(days=self.settings.backfill_days)
        floor = (floor_day.year, floor_day.month)
        cursor = previous_month(today.year, today.month)
        return {"version": STATE_VERSION, "backfill_done": cursor < floor, "backfill_cursor": month_key(*cursor),
                "backfill_floor": month_key(*floor), "months": {}, "last_incremental_utc": None, "last_nightly_day": None,
                "company": None, "website_missing_until_utc": None, "blocked_until_utc": None, "fail_streak": 0}

    def _load_state(self):
        fresh = self._fresh_state()
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return fresh
        except (OSError, ValueError):
            data = None
        if not _state_is_valid(data):                     # unreadable, damaged or another version: start fresh (no migration)
            logger.info("Sales sync: state file unreadable, starting fresh")
            return fresh
        fresh.update({key: data[key] for key in STATE_KEYS if key in data})
        return fresh

    def _save_state(self):
        if self.settings.mode == "dryrun":
            return                                  # a dry run leaves no trace: switching to "on" later starts from scratch
        temp = self.state_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({key: self.state[key] for key in STATE_KEYS}, handle, indent=1)
            os.replace(temp, self.state_path)
        except OSError:
            self._log_limited("save", "Sales sync: could not save its state file", level=logging.WARNING)

    # ---------------------------------------------------------------- the step
    def step(self, stock_ok=True, stock_changed=True):
        """One cycle's worth of sales work. `stock_changed` says whether the stock the feeder just exported differs from the cycle
        before: the current month is only read again when it did (or after MAX_QUIET_SECONDS). Returns "incremental", "backfill" or "nightly" when it attempted a step, else None.
        A dry run reads Tally and logs, but works on a throw-away copy of the state: the real state (and its file) never moves, and
        when the mode changes the next step starts from the real state again."""
        if self.settings.mode == "off":
            if not self._off_logged:
                self._off_logged = True
                logger.info("Sales sync: off")
            return None
        if self.settings.mode != "dryrun":
            if self._dry_state is not None:              # just switched from a dry run: forget everything it did
                self._dry_state = None
                self._skip_cycles, self._since_month, self._interval_factor, self._stock_moved = 0, MIN_CYCLES_BETWEEN_MONTHS, 1, True
            return self._step(stock_ok, stock_changed)
        real = self.state
        self.state = self._dry_state if self._dry_state is not None else copy.deepcopy(real)
        try:
            return self._step(stock_ok, stock_changed)
        finally:
            self._dry_state, self.state = self.state, real      # keep the copy for the next dry step (a company change may replace it)

    def _step(self, stock_ok, stock_changed):
        self._stock_moved = self._stock_moved or bool(stock_changed)      # remembered until the current month has been read
        first = not self._started
        self._started = True
        self._since_month += 1
        if first or not stock_ok:
            return None
        if self._skip_cycles > 0:
            self._skip_cycles -= 1
            return None
        now = self.clock.utc_now()
        if self._paused(now):
            return None
        local = self.clock.local_now()
        if self._incremental_due(now):
            label = "incremental"
        elif not self.state["backfill_done"] and self._since_month >= MIN_CYCLES_BETWEEN_MONTHS and not self._shop_is_open(local):
            label = "backfill"
        elif self._nightly_due(local) and not self._shop_is_open(local):
            label = "nightly"
        else:
            return None
        return self._run(label, now, local.date())

    def _paused(self, now):
        for key in ("website_missing_until_utc", "blocked_until_utc"):
            until = _parse_utc(self.state[key])
            if until is not None and until > now:
                return True
        return False

    def interval_minutes(self):
        """The incremental interval: the configured one, stretched (up to 4 hours) while Tally is slow to read."""
        base = self.settings.incremental_minutes
        return max(base, min(MAX_INTERVAL_MINUTES, base * self._interval_factor))

    def _incremental_due(self, now):
        """The current month is read when the stock moved since the last read and the interval has passed, or when it has been
        quiet for MAX_QUIET_SECONDS (never less than the interval itself, which the slow guard may have stretched). The very first
        read (a fresh state) happens at once."""
        last = _parse_utc(self.state["last_incremental_utc"])
        if last is None:
            return True
        age = (now - last).total_seconds()
        interval = self.interval_minutes() * 60
        return age >= max(MAX_QUIET_SECONDS, interval) or (self._stock_moved and age >= interval)

    def _shop_is_open(self, local):
        """True during shop hours (Monday to Saturday): backfill and the nightly step wait. Sunday counts as closed; an empty
        setting means no limit."""
        try:
            window = parse_shop_hours(self.settings.shop_hours)
        except ValueError:
            window = parse_shop_hours(DEFAULT_SHOP_HOURS)
        if window is None or local.weekday() == 6:
            return False
        return window[0] <= local.time() < window[1]

    def _nightly_due(self, local):
        return self.state["last_nightly_day"] != local.date().isoformat() and local.hour >= self.settings.nightly_hour

    @property
    def reader(self):
        if self._reader is None:
            import tally_sales
            self._reader = tally_sales.default_reader()
        return self._reader

    @property
    def pusher(self):
        if self._pusher is None:
            self._pusher = make_default_pusher()
        return self._pusher

    def _run(self, label, now, today):
        import tally_sales
        if label == "backfill":
            self._since_month = 0
        # 1. which company is Tally showing?
        try:
            name = self.reader.company_name()
        except tally_sales.TallyReadError:
            self._log_limited("tally", "Sales sync: Tally is not reachable; will try again")
            self._failed()
            return label
        if not name:
            self._log_limited("company", "Sales sync: could not read the company name from Tally; nothing is sent until it can")
            self._failed()
            return label
        chash = tally_sales.company_hash(name)
        if self.state["company"] not in (None, chash):
            self.state = self._fresh_state()
            self.state["company"] = chash
            logger.info("Sales sync: Tally is showing a different company; history will be loaded again")
            self._save_state()
            return None
        self.state["company"] = chash
        # 2. which months does this step cover?
        if label == "incremental":
            months = [(today.year, today.month)]
        elif label == "nightly":
            months = [(today.year, today.month)]
            while len(months) < NIGHTLY_MONTHS:
                months.append(previous_month(*months[-1]))
        else:
            months = [parse_month(self.state["backfill_cursor"])]
        # 3. read, compare and push each month; one failure ends the step
        for year, month in months:
            if not self._sync_month(label, year, month, now):
                return label
        # 4. success: move the state forward
        self.state["fail_streak"] = 0
        self._skip_cycles = 0
        if label in ("incremental", "nightly"):
            self._stock_moved = False                     # the current month has just been read
        if label == "incremental":
            self.state["last_incremental_utc"] = now.isoformat()
        elif label == "nightly":
            self.state["last_nightly_day"] = today.isoformat()
        else:
            cursor = previous_month(*parse_month(self.state["backfill_cursor"]))
            self.state["backfill_cursor"] = month_key(*cursor)
            if cursor < parse_month(self.state["backfill_floor"]):
                self.state["backfill_done"] = True
                logger.info("Sales sync: backfill complete (%d days)", self.settings.backfill_days)
        self._save_state()
        return label

    def _sync_month(self, label, year, month, now):
        """Reads one calendar month from Tally and pushes it unless it is unchanged. True when the month is dealt with, False when it
        failed (the failure is already recorded)."""
        import tally_sales
        key = month_key(year, month)
        first_day, last_day = month_bounds(year, month)
        started = self.clock.monotonic()                        # only the Tally read is timed, never the push
        try:
            lines, stats = self.reader.fetch_month(year, month)
        except tally_sales.TallyTimeout:
            self._log_limited("slow", "Sales sync: Tally was too slow to read %s; will try again", key)
            self._failed()
            return False
        except tally_sales.TallyUnreachable:
            self._log_limited("tally", "Sales sync: Tally is not reachable; will try again")
            self._failed()
            return False
        except tally_sales.TallyReadError:
            self._log_limited("read", "Sales sync: Tally could not give the sales for %s; will try again", key)
            self._failed()
            return False
        read_seconds = self.clock.monotonic() - started
        self._note_read_time(read_seconds)
        digest = fingerprint(lines)
        read_note = "read %.1f s, %.1f MB" % (read_seconds, (stats.get("bytes") or 0) / 1048576.0)     # so a slow Tally shows in the log
        if self.settings.mode == "dryrun":
            logger.info("Sales sync (dry run): %s %s read %d lines in %.1f s (fingerprint %s), would push them", label, key, len(lines),
                        read_seconds, digest[:8])
            return True
        entry = self.state["months"].get(key)
        pushed_at = _parse_utc(entry["pushed_utc"]) if entry else None
        if entry and entry["fingerprint"] == digest and pushed_at and (now - pushed_at).total_seconds() < REPUSH_AFTER_SECONDS:
            logger.info("Sales sync: %s %s unchanged, no push (%d lines, %s)", label, key, len(lines), read_note)
            return True
        for piece_from, piece_to, piece_lines in split_range(lines, first_day, last_day):
            payload = {"company": self.state["company"], "from": piece_from.isoformat(), "to": piece_to.isoformat(), "lines": piece_lines}
            try:
                status, text = self.pusher(payload)
            except Exception as exc:
                self._log_limited("net", "Sales sync: could not reach the website (%s)", type(exc).__name__)
                self._forget(key)
                self._failed()
                return False
            if status != 200:
                self._forget(key)
                self._website_said(status, text, now)
                return False
        self.state["months"][key] = {"fingerprint": digest, "pushed_utc": now.isoformat()}
        logger.info("Sales sync: %s %s pushed %d lines (%s)", label, key, len(lines), read_note)
        return True

    # ---------------------------------------------------------------- the slow guard
    def _note_read_time(self, seconds):
        if seconds > SLOW_READ_SECONDS:
            self._interval_factor = min(self._interval_factor * 2, MAX_INTERVAL_FACTOR)
            self._log_limited("slow_read", "Sales sync: Tally needed %.0f s to read a month; the incremental interval is now %d minutes",
                              seconds, self.interval_minutes(), level=logging.WARNING)
        elif seconds < FAST_READ_SECONDS:
            self._interval_factor = 1

    # ---------------------------------------------------------------- failures
    def _forget(self, key):
        """The website did not take this month: forget its fingerprint so the next step pushes it again."""
        self.state["months"].pop(key, None)

    def _failed(self, count=True):
        """A step failed: nothing moves forward. Counted failures back off for min(2**streak, 60) cycles."""
        if count:
            self.state["fail_streak"] += 1
            self._skip_cycles = min(2 ** min(self.state["fail_streak"], 6), MAX_BACKOFF_CYCLES)
        self._save_state()

    @staticmethod
    def _reason(text):
        try:
            data = json.loads(text)
            reason = data.get("error") if isinstance(data, dict) else None
        except ValueError:
            reason = None
        return reason[:120] if isinstance(reason, str) else "no reason given"

    def _website_said(self, status, text, now):
        if status == 404:
            self.state["website_missing_until_utc"] = (now + timedelta(seconds=PAUSE_404_SECONDS)).isoformat()
            self._log_limited("404", "Sales sync: waiting for the website to be updated")
            self._failed(count=False)
        elif status == 409:
            self.state["blocked_until_utc"] = (now + timedelta(seconds=PAUSE_409_SECONDS)).isoformat()
            self._log_limited("409", "Sales sync refused: company differs from the one the website already knows")
            self._failed(count=False)
        elif status == 422:
            self._log_limited("422", "Sales sync refused by the website (422): %s", self._reason(text))
            self._failed()
        elif status == 413:
            self._log_limited("413", "Sales sync: the website said the push was too large")
            self._failed()
        elif status == 503:
            self._log_limited("503", "Sales sync: the website is busy; will retry next cycle")
            self._failed(count=False)
        elif status in (401, 403):
            self._log_limited("auth", "Sales sync: the website did not accept the token (HTTP %d); check CLOUD_SYNC_TOKEN", status)
            self._failed()
        else:
            self._log_limited("http", "Sales sync: the website answered HTTP %d", status)
            self._failed()
