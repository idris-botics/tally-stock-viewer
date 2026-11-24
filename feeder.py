"""Minimal Tally-to-cloud feeder.

Runs on the office machine that can reach Tally. It does NOT start the
website (no Flask server, no waitress) and needs no mappings.db, no image
folder, and no login/admin routes. It only:

  1. pulls the car master, hierarchy and item stock from Tally, and
  2. pushes that data to the cloud site (CLOUD_SYNC_URL / CLOUD_SYNC_TOKEN).

The Tally fetching and the cloud push are the site's own, already-hardened
functions in app.py (self-healing retries, XML sanitizing, multiple-instance
detection); this file only decides WHEN to call them. Importing app.py starts
no threads and, with FEEDER_MODE=1 (set below), never touches the database.

Schedule (same as the full site): a full refresh (car master + hierarchy +
stock) on startup, then a stock export every TALLY_EXPORT_INTERVAL seconds
(180 by default). The site only runs a full refresh at startup or when an
admin clicks Full Refresh; nobody can click that here, so this also repeats it
every FEEDER_FULL_REFRESH_HOURS (default 6) so new cars/items reach the cloud.

New items: the stock export silently drops in-stock items that are not in the
hierarchy yet. When one shows up (a name that was not already unmatched after the
last refresh) the feeder runs a full refresh right away, at most once per
FEEDER_NEW_ITEM_MIN_MINUTES (default 10), doubling that gap (max 1 hour) after a
failed or slow refresh (FEEDER_SLOW_REFRESH_SECONDS, default 300; a normal one takes ~30 s). The shop can
also force one: a file called refresh_now.flag in this folder (double-click
refresh_feeder_now.bat) is noticed within a couple of seconds and removed after a
successful refresh. Every full refresh logs its reason (startup, timer, new item,
manual) and how long Tally took.

Every cycle is wrapped in try/except, so an unreachable Tally or a failed push
just logs and retries next cycle. Run it forever under Windows Task Scheduler
(see FEEDER_SETUP.md), which restarts it if the process ever dies.
"""
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# The site's cache files use paths relative to the project folder.
os.chdir(BASE_DIR)
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

# Must be set before app.py is imported.
os.environ["FEEDER_MODE"] = "1"
os.environ.setdefault("LOG_FILE", "logs/feeder.log")

import app as site  # noqa: E402  (module import only; nothing is served or scheduled)
from config import Config  # noqa: E402

import logging  # noqa: E402

logger = logging.getLogger("feeder")

FULL_REFRESH_INTERVAL_SECONDS = int(float(os.environ.get("FEEDER_FULL_REFRESH_HOURS", "6")) * 3600)
NEW_ITEM_MIN_SECONDS = int(float(os.environ.get("FEEDER_NEW_ITEM_MIN_MINUTES", "10")) * 60)
NEW_ITEM_MAX_GAP_SECONDS = 3600
SLOW_REFRESH_SECONDS = float(os.environ.get("FEEDER_SLOW_REFRESH_SECONDS", "300"))
REFRESH_FLAG = os.path.join(BASE_DIR, "refresh_now.flag")
FLAG_POLL_SECONDS = 2


def push_to_cloud():
    """The site's own push (never raises; logs its own success/failure)."""
    site._push_data_to_cloud()


def run_full_refresh(reason="timer"):
    """Car master + hierarchy + stock from Tally, saved locally, then pushed.
    Same steps and order as the site's run_full_refresh_job(), minus the
    in-memory reload the website itself needs. Logs why it ran and how long
    each Tally export took; returns the total Tally seconds."""
    logger.info("Full refresh starting: reason=%s", reason)
    started = time.perf_counter()
    try:
        t0 = time.perf_counter()
        car_names = site.fetch_car_master_from_tally()
        site.save_car_master_to_file(car_names)
        t_cars = time.perf_counter() - t0
        t0 = time.perf_counter()
        flat_rows = site.fetch_main_hierarchy_from_tally()
        site.save_main_hierarchy_to_file(flat_rows)
        t_hier = time.perf_counter() - t0
        t0 = time.perf_counter()
        site.fetch_item_stock_flat()
        t_stock = time.perf_counter() - t0
    except Exception:
        logger.warning("Full refresh FAILED: reason=%s, after %.1fs of Tally work", reason, time.perf_counter() - started)
        raise
    tally_seconds = time.perf_counter() - started
    logger.info(
        "Full refresh from Tally completed: reason=%s, Tally export %.1fs (car master %.1fs, hierarchy %.1fs, stock %.1fs)",
        reason, tally_seconds, t_cars, t_hier, t_stock,
    )
    push_to_cloud()
    return tally_seconds


def _flag_mtime():
    try:
        return os.stat(REFRESH_FLAG).st_mtime_ns
    except OSError:
        return None


def _clear_flag(seen_mtime):
    """Remove the manual flag, unless the shop asked again while we were refreshing."""
    if seen_mtime is not None and _flag_mtime() == seen_mtime:
        try:
            os.remove(REFRESH_FLAG)
            logger.info("Manual refresh flag cleared")
        except OSError as exc:
            logger.warning("Could not remove %s: %s", REFRESH_FLAG, exc)


def _wait(seconds, handled_flag_mtime):
    """Sleep up to `seconds`, but wake early for a NEW manual flag (a flag we already
    tried and failed on does not cut the wait short, so a broken Tally is not hammered)."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(FLAG_POLL_SECONDS, remaining))
        mtime = _flag_mtime()
        if mtime is not None and mtime != handled_flag_mtime:
            return


_sales = {"sync": None, "last_error": None}
_stock_seen = {"fingerprint": None}
SALES_ERROR_LOG_SECONDS = 600


def stock_changed_since_last_cycle():
    """True when the stock the feeder just saved differs from the one it saved the cycle before. It uses the site's own content hash
    of the three cache files, so Tally's row order or an identical rewrite is not a change. Any doubt (an error, a first look) counts
    as a change. The sales sync only reads Tally again when this is True (or after two quiet hours)."""
    try:
        fingerprint = site._compute_feeder_fingerprint()
    except Exception:
        return True
    changed = fingerprint != _stock_seen["fingerprint"]
    _stock_seen["fingerprint"] = fingerprint
    return changed


def run_sales_step(stock_ok, stock_changed=True):
    """One cycle's worth of the shop-side sales sync (sales_sync.py). Imported lazily, so a broken or missing module can never
    stop the stock feeder. Never raises. Returns the seconds the sales work took (0.0 when it did nothing)."""
    started = time.monotonic()
    try:
        if _sales["sync"] is None:
            import sales_sync
            _sales["sync"] = sales_sync.SalesSync.from_environment()
        label = _sales["sync"].step(stock_ok, stock_changed)
    except Exception as exc:
        now = time.monotonic()
        if _sales["last_error"] is None or now - _sales["last_error"] >= SALES_ERROR_LOG_SECONDS:
            _sales["last_error"] = now
            logger.warning("Sales sync error: %s", type(exc).__name__)
        return 0.0
    return (time.monotonic() - started) if label else 0.0


class NewItemWatch:
    """Decides when an in-stock item missing from the hierarchy justifies a full refresh.
    Names still unmatched after a refresh are 'known' (e.g. stock outside any car
    group) and never trigger again, so only genuinely new names cause a refresh."""

    def __init__(self):
        self.known = frozenset()
        self.last_attempt = None
        self.gap = NEW_ITEM_MIN_SECONDS
        self._logged = None

    def after_full_refresh(self):
        self.known = frozenset(site.last_item_export_unmatched)

    def wanted(self):
        """True if the stock export just fetched has new unmatched names and the throttle allows a refresh."""
        new = frozenset(site.last_item_export_unmatched) - self.known
        if not new:
            return False
        wait = 0 if self.last_attempt is None else self.last_attempt + self.gap - time.monotonic()
        if wait > 0:
            if self._logged != new:
                logger.info("%d new item name(s) not in the hierarchy (e.g. %s); refresh throttled, allowed in %.0fs",
                            len(new), sorted(new)[0], wait)
                self._logged = new
            return False
        logger.info("%d new item name(s) not in the hierarchy (e.g. %s)", len(new), sorted(new)[0])
        return True

    def attempt_started(self):
        self.last_attempt = time.monotonic()

    def attempt_finished(self, ok, tally_seconds):
        if ok and tally_seconds <= SLOW_REFRESH_SECONDS:
            self.gap = NEW_ITEM_MIN_SECONDS
            return
        self.gap = min(self.gap * 2, NEW_ITEM_MAX_GAP_SECONDS)
        logger.warning("New-item refresh %s; next one allowed in %ss",
                       ("was slow (%.1fs)" % tally_seconds) if ok else "failed", self.gap)


def main():
    if not Config.CLOUD_SYNC_URL or not Config.CLOUD_SYNC_TOKEN:
        logger.error(
            "CLOUD_SYNC_URL and CLOUD_SYNC_TOKEN must both be set in .env -- "
            "there is nothing for the feeder to do without them. Exiting."
        )
        return 2

    import machine_role
    logger.info("Feeder starting: machine role=%s", machine_role.get_role())
    logger.info(
        "Feeder started: Tally=%s, cloud=%s, stock every %ss, full refresh every %ss",
        Config.TALLY_URL, Config.CLOUD_SYNC_URL, site.ITEM_EXPORT_INTERVAL, FULL_REFRESH_INTERVAL_SECONDS,
    )

    last_full_refresh_ok = None
    watch = NewItemWatch()
    while True:
        flag = _flag_mtime()
        if last_full_refresh_ok is None:
            reason = "startup"
        elif time.monotonic() - last_full_refresh_ok >= FULL_REFRESH_INTERVAL_SECONDS:
            reason = "timer"
        elif flag is not None:
            reason = "manual"
        else:
            reason = None
        cycle_ok = False
        try:
            if reason:
                run_full_refresh(reason)
                last_full_refresh_ok = time.monotonic()
                watch.after_full_refresh()
                _clear_flag(flag)
            else:
                site.fetch_item_stock_flat()
                if watch.wanted():
                    watch.attempt_started()
                    try:
                        seconds = run_full_refresh("new item")
                    except Exception as exc:
                        logger.exception("New-item refresh failed: %s", exc)
                        watch.attempt_finished(False, 0)
                        push_to_cloud()   # still send this cycle's normal stock
                    else:
                        watch.after_full_refresh()   # the 6-hour timer is left as it was
                        watch.attempt_finished(True, seconds)
                else:
                    push_to_cloud()
            cycle_ok = True
        except Exception as exc:  # one bad cycle must never end the process
            logger.exception(
                "%s failed (will retry in %ss): %s",
                ("Full refresh (%s)" % reason) if reason else "Stock export", site.ITEM_EXPORT_INTERVAL, exc,
            )
        sales_seconds = run_sales_step(cycle_ok, stock_changed_since_last_cycle())     # at most one small sales step per cycle; never raises
        _wait(max(1.0, site.ITEM_EXPORT_INTERVAL - sales_seconds) if sales_seconds > 0 else site.ITEM_EXPORT_INTERVAL, flag)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Feeder stopped")
