"""Shop-side sales sync: reads SALES vouchers and CREDIT NOTES from Tally (tally_sales.py) and pushes them to the website's
POST /admin/intake/sales, gently, one small step per feeder cycle. Called from feeder.py once per cycle (SalesSync.step).

What is pushed, and nothing else: an anonymous voucher key, the day, "sale" or "return", the item name, a whole-number quantity and
a hash of the company name. Every push REPLACES the website's lines for the days it covers, so repeating a push changes nothing.

Order of work (at most ONE step per cycle; the first cycle after startup does nothing):
  a. INCREMENTAL  the last 3 days (today-2 .. today), every SALES_INCREMENTAL_MINUTES (a fresh state counts as due: the first step ever)
  b. BACKFILL     one chunk of history, NEWEST FIRST, at least 2 cycles after the previous chunk, until the oldest day is reached
  c. NIGHTLY      the last 36 days (today-35 .. today) once a day after SALES_NIGHTLY_HOUR (heals changed and deleted vouchers)
Because history is loaded newest-first, the website always holds ONE unbroken stretch of days that ends today.

Nothing runs on import: no thread, no network call. Creating a SalesSync only reads its small state file.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("feeder")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "sales_sync_state.json")

INCREMENTAL_BACK_DAYS = 2             # incremental covers today-2 .. today
NIGHTLY_BACK_DAYS = 35                # nightly covers today-35 .. today
BACKFILL_START_OFFSET = 3             # the first history chunk ends this many days before today (the incremental covers the rest)
MIN_CYCLES_BETWEEN_CHUNKS = 2
CHUNK_START, CHUNK_MIN, CHUNK_MAX = 31, 1, 60
SLOW_READ_SECONDS, FAST_READ_SECONDS = 40.0, 10.0
MAX_PUSH_LINES = 25000                # the website accepts 30,000
MAX_PUSH_BYTES = 2400000              # the website accepts 3 MB
PAUSE_404_SECONDS = 3600
PAUSE_409_SECONDS = 24 * 3600
MAX_BACKOFF_CYCLES = 60
LOG_AGAIN_SECONDS = 3600

STATE_KEYS = ("backfill_done", "backfill_cursor", "backfill_floor", "chunk_days", "last_incremental_utc", "last_nightly_day",
              "company", "website_missing_until_utc", "blocked_until_utc", "fail_streak")


@dataclass
class SalesSettings:
    mode: str = "on"                  # on | off | dryrun
    backfill_days: int = 365
    incremental_minutes: int = 15
    nightly_hour: int = 3

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
        return cls(
            mode=mode,
            backfill_days=number("SALES_BACKFILL_DAYS", 365, 1, 400),
            incremental_minutes=number("SALES_INCREMENTAL_MINUTES", 15, 1, 1440, too_low_means_default=True),
            nightly_hour=nightly if 0 <= nightly <= 23 else 3)


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


def _parse_utc(text):
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_date(text):
    try:
        date.fromisoformat(text)
        return True
    except (TypeError, ValueError):
        return False


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _state_is_valid(data):
    if not isinstance(data, dict):
        return False
    checks = {
        "backfill_done": lambda v: isinstance(v, bool),
        "backfill_cursor": _is_date, "backfill_floor": _is_date,
        "chunk_days": lambda v: _is_int(v) and CHUNK_MIN <= v <= CHUNK_MAX,
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
    MAX_PUSH_LINES lines and about MAX_PUSH_BYTES of JSON. One piece unless the range is large. Newest piece first."""
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
        self._since_chunk = MIN_CYCLES_BETWEEN_CHUNKS
        self._last_logged = {}
        self._off_logged = False
        self.state = self._load_state()

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
        floor = today - timedelta(days=self.settings.backfill_days)
        cursor = today - timedelta(days=BACKFILL_START_OFFSET)
        return {"backfill_done": cursor < floor, "backfill_cursor": cursor.isoformat(), "backfill_floor": floor.isoformat(),
                "chunk_days": CHUNK_START, "last_incremental_utc": None, "last_nightly_day": None, "company": None,
                "website_missing_until_utc": None, "blocked_until_utc": None, "fail_streak": 0}

    def _load_state(self):
        fresh = self._fresh_state()
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return fresh
        except (OSError, ValueError):
            data = None
        if not _state_is_valid(data):
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
    def step(self, stock_ok=True):
        """One cycle's worth of sales work. Returns "incremental", "backfill" or "nightly" when it attempted a step, else None."""
        if self.settings.mode == "off":
            if not self._off_logged:
                self._off_logged = True
                logger.info("Sales sync: off")
            return None
        first = not self._started
        self._started = True
        self._since_chunk += 1
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
        elif not self.state["backfill_done"] and self._since_chunk >= MIN_CYCLES_BETWEEN_CHUNKS:
            label = "backfill"
        elif self._nightly_due(local):
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

    def _incremental_due(self, now):
        last = _parse_utc(self.state["last_incremental_utc"])
        return last is None or (now - last).total_seconds() >= self.settings.incremental_minutes * 60

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
            self._since_chunk = 0
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
        # 2. the range for this step
        if label == "incremental":
            day_from, day_to = today - timedelta(days=INCREMENTAL_BACK_DAYS), today
        elif label == "nightly":
            day_from, day_to = today - timedelta(days=NIGHTLY_BACK_DAYS), today
        else:
            cursor = date.fromisoformat(self.state["backfill_cursor"])
            floor = date.fromisoformat(self.state["backfill_floor"])
            day_from, day_to = max(cursor - timedelta(days=self.state["chunk_days"] - 1), floor), cursor
        # 3. read (this is the only part that holds Tally, and the only part that is timed)
        started = self.clock.monotonic()
        try:
            lines = self.reader.fetch_sales_lines(day_from, day_to)
        except tally_sales.TallyTimeout:
            if label == "backfill":
                self._halve_chunk()
            self._log_limited("slow", "Sales sync: Tally was too slow for this range; will try again with less")
            self._failed()
            return label
        except tally_sales.TallyUnreachable:
            self._log_limited("tally", "Sales sync: Tally is not reachable; will try again")
            self._failed()
            return label
        except tally_sales.TallyReadError:
            self._log_limited("read", "Sales sync: Tally could not give the sales for this range; will try again")
            self._failed()
            return label
        read_seconds = self.clock.monotonic() - started
        if label == "backfill" and read_seconds > SLOW_READ_SECONDS:
            self._halve_chunk()
        # 4. push (or, in a dry run, only say what would be pushed)
        pieces = split_range(lines, day_from, day_to)
        total = sum(len(piece[2]) for piece in pieces)
        if self.settings.mode == "dryrun":
            logger.info("Sales sync (dry run): %s %s..%s would push %d lines", label, day_from, day_to, total)
        else:
            for piece_from, piece_to, piece_lines in pieces:
                payload = {"company": chash, "from": piece_from.isoformat(), "to": piece_to.isoformat(), "lines": piece_lines}
                try:
                    status, text = self.pusher(payload)
                except Exception as exc:
                    self._log_limited("net", "Sales sync: could not reach the website (%s)", type(exc).__name__)
                    self._failed()
                    return label
                if status != 200:
                    self._website_said(status, text, now)
                    return label
            logger.info("Sales sync: %s %s..%s pushed %d lines", label, day_from, day_to, total)
        # 5. success: move the state forward
        self.state["fail_streak"] = 0
        self._skip_cycles = 0
        if label == "incremental":
            self.state["last_incremental_utc"] = now.isoformat()
        elif label == "nightly":
            self.state["last_nightly_day"] = today.isoformat()
        else:
            if read_seconds < FAST_READ_SECONDS:
                self.state["chunk_days"] = min(CHUNK_MAX, int(self.state["chunk_days"] * 1.5))
            cursor = day_from - timedelta(days=1)
            self.state["backfill_cursor"] = cursor.isoformat()
            if cursor < date.fromisoformat(self.state["backfill_floor"]):
                self.state["backfill_done"] = True
                logger.info("Sales sync: backfill complete (%d days)", self.settings.backfill_days)
        self._save_state()
        return label

    # ---------------------------------------------------------------- failures
    def _halve_chunk(self):
        self.state["chunk_days"] = max(CHUNK_MIN, self.state["chunk_days"] // 2)

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
            self._halve_chunk()
            self._log_limited("413", "Sales sync: the website said the push was too large; the next one will be smaller")
            self._failed(count=False)
        elif status == 503:
            self._log_limited("503", "Sales sync: the website is busy; will retry next cycle")
            self._failed(count=False)
        elif status in (401, 403):
            self._log_limited("auth", "Sales sync: the website did not accept the token (HTTP %d); check CLOUD_SYNC_TOKEN", status)
            self._failed()
        else:
            self._log_limited("http", "Sales sync: the website answered HTTP %d", status)
            self._failed()
