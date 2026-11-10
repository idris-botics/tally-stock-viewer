"""Incremental, verified backup of mappings.db + S.S IMAGE + small JSON
caches to a Google Drive folder, authenticated as a real Google account
via OAuth (installed-app flow) -- not a Service Account. A Service
Account has zero personal Drive storage quota of its own: it can create
folders (metadata-only, no storage cost) but every real file upload fails
with a "Service Accounts do not have storage quota" 403 -- confirmed for
real, not just from docs. See build_drive_service() below and
GDRIVE_OAUTH_* in config.py for the one-time setup this requires.

Design mirrors existing conventions elsewhere in this codebase rather than
inventing new ones:
  - Change detection uses the same cheap (mtime, size) fingerprint idea as
    app.py's _file_fingerprint(), not a full-content hash on every run.
  - Backup file naming/pruning philosophy matches database.py's
    _backup_database_file() (timestamped, safe to call repeatedly).
  - Scheduling is a self-rescheduling threading.Timer, the same pattern as
    app.py's schedule_item_export().
  - "Not configured" is a clean no-op (log once, never raise, never
    schedule), the same fallback discipline as SYSTEM_ACCESS_TOKEN /
    ACCOUNTS_ACCESS_PASSWORD in config.py.

Manifest file (data/.cloud_backup_manifest.json) is the local source of
truth for what Drive is believed to hold: relative_path -> {drive_file_id,
size, mtime_ns, hash (optional, lazily filled)}. It is saved after every
single file operation succeeds, so an interrupted run never has to redo
work it already finished.
"""
import concurrent.futures
import gc
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, time as dt_time

from config import Config

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGE_ROOT = os.path.join(DATA_DIR, "S.S IMAGE")
MANIFEST_PATH = os.path.join(DATA_DIR, ".cloud_backup_manifest.json")

# Small JSON caches -- see cloud_backup investigation: these are cheap to
# include and complete the "restore everything exactly as it was" story,
# even though they're technically re-derivable from Tally on the next Full
# Refresh. Paths match the exact filenames app.py already uses.
INCLUDED_JSON_FILES = [
    "car_master.json",
    "main_hierarchy.json",
    "item stock list.auto.json",
]
INCLUDED_DB_FILE = "mappings.db"
# The live database is never uploaded directly (it is in WAL mode: recent
# commits live in the -wal file and a raw copy can be torn mid-checkpoint).
# Each run takes a consistent snapshot with SQLite's online backup API into
# this scratch folder and uploads THAT; it is removed when the run ends.
SNAPSHOT_DIR = os.path.join(DATA_DIR, ".cloud_backup_snapshot")

# Mass-deletion guard: a run that would move more than
# max(DELETION_GUARD_MIN, DELETION_GUARD_FRACTION * tracked files) files to
# Drive Trash holds ALL of its deletions back. Config.BACKUP_MAX_DELETIONS
# overrides the limit.
DELETION_GUARD_MIN = 20
DELETION_GUARD_FRACTION = 0.02

DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Pacing: a floor between consecutive Drive API calls so a large run doesn't
# fire requests as fast as possible. Well under Drive's per-user limits.
MIN_CALL_INTERVAL_SECONDS = 0.15
MAX_RETRY_ATTEMPTS = 6
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 60.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

# Drive's quota errors don't always arrive as 429 -- confirmed for real:
# one production run got a 403 (not 429) with reason "userRateLimitExceeded"
# on a file upload, which RETRYABLE_STATUS_CODES alone never retried (403
# is otherwise a real, permanent failure -- e.g. a genuine permissions
# problem -- so the status code alone can't tell them apart; the reason
# string can). Google's own Drive API quota docs list both of these 403
# reasons as the rate-limit ones meant to be retried with backoff exactly
# like 429, never the many other, permanent reasons a 403 can carry.
RETRYABLE_403_REASONS = {"userRateLimitExceeded", "rateLimitExceeded"}

SYNC_LOCK = threading.Lock()  # same acquire-in-caller / release-in-finally convention as app.py's FULL_REFRESH_LOCK/EXPORT_LOCK
_timer = None
_last_call_time = 0.0
_last_call_lock = threading.Lock()

_status = {
    "running": False,
    "last_run": None,  # filled after first run: dict, see _build_run_summary()
    "last_success_at": None,  # ISO timestamp of the last run whose status was "success" -- distinct from last_run, which can be a more recent failed/partial attempt. Drives the nightly scheduler's catch-up rule (see schedule()).
    "last_catchup_attempted_at": None,  # ISO timestamp of the last time schedule() ARMED a catch-up run (set at decision time, not at run completion, so even a restart seconds later already sees it) -- regardless of that attempt's outcome. Lets _compute_startup_plan() tell "already tried a catch-up this window" apart from "last sync wasn't a success", so a persistent partial/failed sync can't make catch-up re-fire on every restart -- see _compute_startup_plan()'s docstring.
}
_status_lock = threading.Lock()

# Scheduled-run skip tracking, surfaced on the System panel instead of only
# ever landing in the log file -- a skip (not yet authorized, or a sync
# already running at that exact moment) never touches _status["last_run"]
# (no run actually happened), so without this an admin checking the panel
# would just see whatever the previous real run's result was, with no hint
# that today's scheduled attempt never ran at all.
_SKIPPED_RUNS_CAPACITY = 20
_skipped_runs_lock = threading.Lock()
_skipped_runs = []  # most recent last; each: {"timestamp", "reason", "message"}

# _status["last_run"] and _skipped_runs both used to be in-memory only --
# an admin restarting the app (pull_and_restart/restart_app_only, a crash,
# a Windows reboot) lost the last-run summary and the skipped-run history
# from the System panel entirely, even though the real backup (the
# manifest, the files on Drive) was completely untouched. That looked like
# "the cloud backup data disappeared" even though nothing was actually
# lost -- persisted here the same way (temp file + os.replace) as
# MANIFEST_PATH above, loaded once at import time. _progress (the live,
# in-flight sync state) deliberately still isn't persisted -- it's
# meaningless after a restart, "not running" is exactly the right default.
STATUS_PATH = os.path.join(DATA_DIR, ".cloud_backup_status.json")
_persisted_status_lock = threading.Lock()  # guards the on-disk file only, separate from _status_lock/_skipped_runs_lock so saving never has to nest another state lock inside itself


def _save_persisted_status():
    with _status_lock:
        last_run_snapshot = dict(_status["last_run"]) if _status["last_run"] else None
    with _skipped_runs_lock:
        skipped_runs_snapshot = list(_skipped_runs)
    with _status_lock:
        last_success_at_snapshot = _status.get("last_success_at")
        last_catchup_attempted_at_snapshot = _status.get("last_catchup_attempted_at")
    payload = {
        "last_run": last_run_snapshot,
        "skipped_runs": skipped_runs_snapshot,
        "last_success_at": last_success_at_snapshot,
        "last_catchup_attempted_at": last_catchup_attempted_at_snapshot,
    }
    try:
        with _persisted_status_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp_path = f"{STATUS_PATH}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, STATUS_PATH)
    except Exception:
        # Best-effort -- the in-memory state (what the System panel actually
        # reads right now) is already correct either way; only surviving a
        # restart is at risk if this fails, not this run's own correctness.
        logger.exception("Failed to persist cloud backup status to %s", STATUS_PATH)


def _load_persisted_status():
    if not os.path.isfile(STATUS_PATH):
        return
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        logger.exception("Failed to load persisted cloud backup status, starting fresh: %s", STATUS_PATH)
        return
    if not isinstance(data, dict):
        return
    with _status_lock:
        _status["last_run"] = data.get("last_run")
        _status["last_success_at"] = data.get("last_success_at")
        _status["last_catchup_attempted_at"] = data.get("last_catchup_attempted_at")
    with _skipped_runs_lock:
        skipped = data.get("skipped_runs")
        if isinstance(skipped, list):
            _skipped_runs[:] = skipped[-_SKIPPED_RUNS_CAPACITY:]


def _record_skipped_run(reason, message):
    with _skipped_runs_lock:
        _skipped_runs.append({
            "timestamp": datetime.now().isoformat(),
            "reason": reason,  # "not_authorized" | "already_running"
            "message": message,
        })
        if len(_skipped_runs) > _SKIPPED_RUNS_CAPACITY:
            del _skipped_runs[0]
    _save_persisted_status()


def get_skipped_runs():
    with _skipped_runs_lock:
        return list(_skipped_runs)


_load_persisted_status()

# Cooperative cancellation for the "Stop Sync" button (Part 3): checked
# before starting each new file/folder operation, never mid-write, so a
# stopped run always leaves the manifest reflecting only genuinely
# completed items and is safe to resume on the next Sync Now / scheduled run.
_cancel_event = threading.Event()

# Live progress for the System panel (Part 4) -- same lightweight
# in-memory-dict + polling-endpoint shape as full_refresh_status /
# _BADGE_REGEN_STATUS in app.py. Updating this dict is cheap (no disk I/O),
# so it's safe to touch on every single file without slowing the sync down.
_PROGRESS_LOG_CAPACITY = 25  # most-recent action lines kept for the live log
_progress_lock = threading.Lock()
_progress = {
    "running": False,
    "stop_requested": False,
    "current": None,
    "total": 0,
    "processed": 0,
    "uploaded": 0,
    "updated": 0,
    "deleted": 0,
    "skipped": 0,
    "failed": 0,
    "log": [],
}


def _progress_reset(total, skipped):
    with _progress_lock:
        _progress.update({
            "running": True,
            "stop_requested": False,
            "current": None,
            "total": total,
            "processed": 0,
            "uploaded": 0,
            "updated": 0,
            "deleted": 0,
            "skipped": skipped,
            "failed": 0,
            "log": [],
        })


def _progress_log_line(line):
    with _progress_lock:
        _progress["current"] = line
        _progress["log"].append(line)
        if len(_progress["log"]) > _PROGRESS_LOG_CAPACITY:
            del _progress["log"][0]


def _progress_note(action, relative_path):
    _progress_log_line(f"{action}: {relative_path}")


def _progress_note_failure(relative_path, short_error):
    """Appends a second, outcome-bearing line after the item's initial
    "Uploading: X" / "Updating: X" / "Deleting: X" line, so the live log on
    the System panel reads as a real transcript instead of leaving a failed
    item looking identical to one still in flight. short_error is the same
    text also folded into the run summary's error string -- see
    _short_error_text() -- so the reason is visible without needing to open
    the log file."""
    _progress_log_line(f"Failed: {relative_path} -- {short_error}")


def _short_error_text(exc, limit=180):
    """A one-line, UI-safe rendering of a real exception -- collapsed
    whitespace (HttpError's str() often spans multiple lines) and capped
    length, prefixed with the exception's type since the message alone is
    sometimes just a bare code (e.g. some HttpError bodies)."""
    text = " ".join(str(exc).split()) or repr(exc)
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"


def _progress_finish_item(counter_key):
    with _progress_lock:
        _progress["processed"] += 1
        _progress[counter_key] += 1


def _progress_stop():
    with _progress_lock:
        _progress["running"] = False
        _progress["current"] = None


def get_progress():
    with _progress_lock:
        return dict(_progress, log=list(_progress["log"]))


def request_stop():
    """Signals the in-progress sync, if any, to stop cleanly at its next
    safe checkpoint (see _cancel_event usage in run_sync below). No-op if
    nothing is currently running."""
    with _progress_lock:
        if not _progress["running"]:
            return {"stopping": False, "running": False}
        _progress["stop_requested"] = True
    _cancel_event.set()
    return {"stopping": True, "running": True}


def is_configured():
    """Only checks that the OAuth client secrets file and a token path/
    backup folder are set -- GDRIVE_OAUTH_TOKEN_PATH itself is allowed to
    not exist yet (that's exactly the first-run state build_drive_service()
    handles by opening the one-time browser consent flow)."""
    client_secrets_path = Config.GDRIVE_OAUTH_CLIENT_SECRETS_PATH
    token_path = Config.GDRIVE_OAUTH_TOKEN_PATH
    folder_id = Config.GDRIVE_BACKUP_FOLDER_ID
    if not client_secrets_path or not token_path or not folder_id:
        return False
    if not os.path.isfile(client_secrets_path):
        logger.warning("GDRIVE_OAUTH_CLIENT_SECRETS_PATH is set but file does not exist: %s", client_secrets_path)
        return False
    return True


def _log_not_configured_once():
    logger.info(
        "Cloud backup is not configured (GDRIVE_OAUTH_CLIENT_SECRETS_PATH / "
        "GDRIVE_OAUTH_TOKEN_PATH / GDRIVE_BACKUP_FOLDER_ID missing or invalid) -- "
        "skipping, feature disabled."
    )


def get_status():
    with _status_lock:
        snapshot = dict(_status)
        snapshot["last_run"] = dict(_status["last_run"]) if _status["last_run"] else None
    snapshot["configured"] = is_configured()
    snapshot["progress"] = get_progress()
    snapshot["skipped_runs"] = get_skipped_runs()
    snapshot["next_scheduled_backup"] = get_next_scheduled_backup_iso()
    return snapshot


# ---------------------------------------------------------------------------
# File enumeration (Part 1 include list)
# ---------------------------------------------------------------------------

def _relative_path(full_path):
    return os.path.relpath(full_path, DATA_DIR).replace("\\", "/")


def _database_path():
    return Config.DB_PATH if os.path.isabs(Config.DB_PATH) else os.path.join(BASE_DIR, Config.DB_PATH)


def _snapshot_database(db_path):
    """A consistent copy of the live database (SQLite online backup API: sees
    committed WAL content, never a torn page), written to SNAPSHOT_DIR as a
    plain rollback-journal file and integrity-checked before anything may
    upload it. Raises on any problem, so a bad snapshot can never replace the
    previous good copy on Drive."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    target = os.path.join(SNAPSHOT_DIR, INCLUDED_DB_FILE)
    for stale in (target, target + "-wal", target + "-shm", target + "-journal"):
        if os.path.exists(stale):
            os.remove(stale)
    source = sqlite3.connect(db_path, timeout=Config.DB_TIMEOUT)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")  # one self-contained file, nothing in a -wal
            result = dest.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"snapshot failed quick_check: {result[0] if result else 'no result'}")
        finally:
            dest.close()
    finally:
        source.close()
    return target


def _remove_snapshot():
    shutil.rmtree(SNAPSHOT_DIR, ignore_errors=True)


def _iter_included_files(db_snapshot_path=None, skip_db=False):
    """Yields (relative_path, absolute_path) for every file this backup
    includes. relative_path uses forward slashes and is relative to data/,
    e.g. "mappings.db" or "S.S IMAGE/7'D MAT/0.jpg".

    The database is yielded as db_snapshot_path (a run's consistent snapshot)
    when given, never as the live file once run_sync takes snapshots; skip_db
    leaves it out entirely (snapshot failed -- see run_sync, which also makes
    sure that is never mistaken for 'the database was deleted')."""
    db_path = _database_path()
    if not skip_db:
        if db_snapshot_path:
            yield INCLUDED_DB_FILE, db_snapshot_path
        elif os.path.isfile(db_path):
            yield INCLUDED_DB_FILE, db_path

    for name in INCLUDED_JSON_FILES:
        full_path = os.path.join(DATA_DIR, name)
        if os.path.isfile(full_path):
            yield name, full_path

    if os.path.isdir(IMAGE_ROOT):
        for root, _dirs, files in os.walk(IMAGE_ROOT):
            for filename in files:
                full_path = os.path.join(root, filename)
                if not os.path.isfile(full_path):
                    continue
                yield _relative_path(full_path), full_path


def _file_stat_signature(full_path):
    stats = os.stat(full_path)
    return stats.st_size, stats.st_mtime_ns


def _content_hash(full_path):
    hasher = hashlib.sha256()
    with open(full_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _empty_manifest():
    return {"version": 1, "files": {}, "folders": {}}


def load_manifest():
    if not os.path.isfile(MANIFEST_PATH):
        return _empty_manifest()
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return _empty_manifest()
        data.setdefault("files", {})
        data.setdefault("folders", {})
        return data
    except Exception:
        logger.exception("Failed to load cloud backup manifest, starting fresh: %s", MANIFEST_PATH)
        return _empty_manifest()


def save_manifest(manifest):
    """Atomic write (temp file + os.replace) so a crash mid-write never
    corrupts the manifest that already-completed file operations rely on."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = f"{MANIFEST_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, MANIFEST_PATH)


# ---------------------------------------------------------------------------
# Change classification (Part 3)
# ---------------------------------------------------------------------------

def _md5_of_file(full_path):
    hasher = hashlib.md5()
    with open(full_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def classify_changes(manifest, current_files, drive_md5_by_id=None):
    """current_files: dict of relative_path -> absolute_path (from
    _iter_included_files, materialized so it can be scanned twice).

    Returns (new_paths, changed_paths, deleted_paths, unchanged_count,
    computed_hashes). Only touches disk for size+mtime (cheap stat), except
    for files whose size is unchanged but mtime differs -- an ambiguous case
    (e.g. a benign touch/copy that preserved size) where a real content hash
    decides whether it's actually a re-upload-worthy change, rather than
    trusting mtime alone or, worse, hashing every file on every run.

    When an ambiguous file turns out unchanged, its manifest entry's
    mtime_ns/hash are updated in place immediately (caller saves the
    manifest) so the *same* touched-but-identical file doesn't get re-hashed
    on every future run forever. When it turns out genuinely changed, the
    hash is returned in computed_hashes so the upload step can persist it
    without hashing the (possibly large) file a second time.

    drive_md5_by_id (optional, from this run's Drive listing): lets a file
    that has no stored hash -- most image entries -- be compared by MD5
    against what Drive already holds when only its mtime differs (e.g. files
    copied back after a restore). Equal => unchanged, no re-upload."""
    manifest_files = manifest.get("files", {})
    new_paths = []
    changed_paths = []
    unchanged_count = 0
    computed_hashes = {}

    for relative_path, full_path in current_files.items():
        try:
            size, mtime_ns = _file_stat_signature(full_path)
        except OSError:
            logger.warning("Skipping unreadable file during classification: %s", full_path)
            continue

        entry = manifest_files.get(relative_path)
        if entry is None:
            new_paths.append(relative_path)
            continue

        if entry.get("size") != size:
            changed_paths.append(relative_path)
            continue

        if entry.get("mtime_ns") == mtime_ns:
            unchanged_count += 1
            continue

        # Ambiguous: same size, different mtime. Fall back to a real hash
        # instead of assuming either way.
        if not entry.get("hash") and drive_md5_by_id:
            drive_md5 = drive_md5_by_id.get(entry.get("drive_file_id"))
            if drive_md5 and _md5_of_file(full_path) == drive_md5:
                unchanged_count += 1
                entry["mtime_ns"] = mtime_ns
                continue

        current_hash = _content_hash(full_path)
        if entry.get("hash") == current_hash:
            unchanged_count += 1
            entry["mtime_ns"] = mtime_ns
            entry["hash"] = current_hash
        else:
            changed_paths.append(relative_path)
            computed_hashes[relative_path] = current_hash

    deleted_paths = [rel for rel in manifest_files if rel not in current_files]
    return new_paths, changed_paths, deleted_paths, unchanged_count, computed_hashes


# ---------------------------------------------------------------------------
# Drive API plumbing (rate-limit aware)
# ---------------------------------------------------------------------------

class NotAuthorizedError(Exception):
    """Raised by build_drive_service() when there's no usable OAuth token.

    Deliberately never triggers a browser flow itself -- that used to live
    inline here, and a closed/abandoned browser tab during a background
    Sync Now left the sync thread frozen forever inside the unbounded
    wait for the OAuth callback (SYNC_LOCK held, Stop Sync unreachable
    since the thread never got as far as its cancellation checkpoints).
    The one-time authorization now lives entirely in authorize() below,
    completely separate from SYNC_LOCK and run_sync()."""


def _load_saved_credentials():
    """Loads a previously-saved token if present and parseable. No network
    calls, no browser -- just a local file read, so this is always fast
    and safe to call from anywhere (status checks included)."""
    token_path = Config.GDRIVE_OAUTH_TOKEN_PATH
    if not token_path or not os.path.isfile(token_path):
        return None
    from google.oauth2.credentials import Credentials

    try:
        return Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        logger.warning("Cloud backup: OAuth token file at %s is unreadable", token_path)
        return None


def _save_credentials(credentials):
    token_path = Config.GDRIVE_OAUTH_TOKEN_PATH
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())


def is_authorized():
    """No network calls -- true if there's a saved token that's either
    still valid or has a refresh_token to try. Doesn't guarantee a refresh
    will actually succeed (e.g. access was revoked) -- that surfaces as a
    normal sync failure through the existing error-logging path, same as
    any other Drive API error."""
    credentials = _load_saved_credentials()
    if credentials is None:
        return False
    return bool(credentials.valid or credentials.refresh_token)


def build_drive_service():
    """Authenticates as the actual dedicated Google account via OAuth,
    which has normal 15GB personal Drive storage -- unlike a Service
    Account (see module docstring). Only ever loads a previously-saved
    token and, if it's expired, refreshes it via the refresh token (a
    bounded network call, not a browser interaction) -- it NEVER opens a
    browser itself. If there's no usable token at all, raises
    NotAuthorizedError immediately instead of attempting the one-time
    consent flow inline -- see authorize() for that, and run_sync()'s
    NotAuthorizedError handling for how a sync fails fast and cleanly
    when this happens."""
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    credentials = _load_saved_credentials()
    if credentials is None:
        raise NotAuthorizedError(
            "Google Drive is not authorized yet -- click 'Authorize Google Drive' on the System panel first."
        )

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            refresh_request = Request()
            try:
                credentials.refresh(refresh_request)  # bounded (120s default), not an indefinite wait
            finally:
                try:
                    refresh_request.session.close()   # google-auth's Request owns a requests.Session
                except Exception:
                    pass
            _save_credentials(credentials)
        else:
            raise NotAuthorizedError(
                "Google Drive authorization is no longer valid and can't be refreshed -- "
                "click 'Authorize Google Drive' again."
            )

    return _register_service("main", build("drive", "v3", credentials=credentials, cache_discovery=False))


# ---------------------------------------------------------------------------
# One-time authorization (dedicated action, separate from SYNC_LOCK/run_sync)
# ---------------------------------------------------------------------------

AUTH_LOCK = threading.Lock()  # same acquire-in-caller/release-in-finally convention as SYNC_LOCK
AUTHORIZE_TIMEOUT_SECONDS = 180

_auth_status_lock = threading.Lock()
_auth_status = {
    "in_progress": False,
    "last_result": None,  # {"ok": bool, "error": str|None, "finished_at": iso}
}


def get_auth_status():
    with _auth_status_lock:
        snapshot = dict(_auth_status)
        snapshot["last_result"] = dict(_auth_status["last_result"]) if _auth_status["last_result"] else None
    snapshot["authorized"] = is_authorized()
    snapshot["configured"] = is_configured()
    return snapshot


def authorize(timeout_seconds=AUTHORIZE_TIMEOUT_SECONDS):
    """Dedicated, standalone one-time OAuth consent flow -- the only place
    in this module that opens a browser. Never touches SYNC_LOCK and is
    never called from run_sync()/build_drive_service(), so an abandoned
    browser tab here can never block a sync or leave that lock stuck.

    Caller is responsible for holding AUTH_LOCK for the duration of this
    call (same convention as SYNC_LOCK -- see
    system_cloud_backup_authorize() in app.py), which just prevents two
    concurrent authorize() attempts, not sync/authorize interference.

    Hard-times-out after timeout_seconds (default 3 minutes): if consent
    isn't completed in time -- tab closed, ignored, whatever -- the local
    callback server stops listening on its own (google_auth_oauthlib's
    built-in timeout_seconds support) and this returns a clean
    {"ok": False, "error": "...timed out..."} result instead of hanging
    forever, with the listening socket properly closed either way."""
    from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

    with _auth_status_lock:
        _auth_status["in_progress"] = True

    result = {"ok": False, "error": "Unknown error"}
    try:
        client_secrets_path = Config.GDRIVE_OAUTH_CLIENT_SECRETS_PATH
        if not client_secrets_path or not os.path.isfile(client_secrets_path):
            result = {"ok": False, "error": "GDRIVE_OAUTH_CLIENT_SECRETS_PATH is not set or the file does not exist."}
            return result

        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
        logger.info("Cloud backup: starting authorization (opening browser, %ss timeout)", timeout_seconds)
        try:
            credentials = flow.run_local_server(port=0, timeout_seconds=timeout_seconds)
        except WSGITimeoutError:
            logger.warning(
                "Cloud backup: authorization timed out after %ss (browser tab closed or never completed)",
                timeout_seconds,
            )
            result = {"ok": False, "error": "Authorization timed out -- try again."}
            return result
        except Exception as exc:
            logger.exception("Cloud backup: authorization attempt failed")
            result = {"ok": False, "error": f"Authorization failed: {_short_error_text(exc)}"}
            return result

        _save_credentials(credentials)
        logger.info("Cloud backup: authorization completed successfully")
        result = {"ok": True}
        return result
    finally:
        with _auth_status_lock:
            _auth_status["in_progress"] = False
            _auth_status["last_result"] = dict(result, finished_at=datetime.now().isoformat())


def _pace():
    global _last_call_time
    with _last_call_lock:
        now = time.monotonic()
        wait = MIN_CALL_INTERVAL_SECONDS - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()


def _http_error_reasons(exc):
    """Extracts every machine-readable reason code (e.g.
    "userRateLimitExceeded") from an HttpError's parsed body, defensively --
    error_details' shape depends on which key Google's response happened to
    use (see HttpError._get_reason() upstream), so this only ever returns
    what it can confidently identify as reason strings, never raises on an
    unexpected shape."""
    details = getattr(exc, "error_details", None)
    if isinstance(details, list):
        return {item.get("reason") for item in details if isinstance(item, dict) and item.get("reason")}
    if isinstance(details, dict) and details.get("reason"):
        return {details["reason"]}
    return set()


def _is_retryable_http_error(exc, status):
    if status in RETRYABLE_STATUS_CODES:
        return True
    # 403 covers many permanent failures (real permissions problems included)
    # alongside Drive's rate-limit ones -- only retry the specific reasons
    # Google documents as transient, never a bare "status == 403".
    if status == 403 and _http_error_reasons(exc) & RETRYABLE_403_REASONS:
        return True
    return False


def call_with_backoff(request_factory, **execute_kwargs):
    """request_factory: zero-arg callable returning a fresh googleapiclient
    request object (must be fresh per attempt -- request objects are
    single-use). Retries with exponential backoff + jitter for:
      - 429/5xx HttpErrors, and 403 HttpErrors whose reason is one of
        RETRYABLE_403_REASONS (confirmed for real: Drive returned a 403
        "userRateLimitExceeded" for one file in one production run -- a
        bare status-code check alone never retries any 403, permanent ones
        included, so the reason string is what tells them apart);
      - transient network-level failures below the HTTP layer (TimeoutError,
        ConnectionError) -- confirmed for real: a bare TimeoutError from
        ssl.SSLSocket.read() while waiting for Drive's response to a
        resumable upload PUT is NOT an HttpError, so it used to bypass this
        function's retry logic entirely.
    Both of the above permanently failed one file each in the same
    8,047-file production run before this fix. Any other exception still
    raises immediately.

    For a resumable upload specifically, either failure can occur AFTER
    Drive already received the bytes but before the confirmation response
    was read -- retrying then starts a FRESH resumable session (request_
    factory() is always called again fresh), which can occasionally leave
    a real duplicate on Drive. Accepted trade-off, not silently unsafe: the
    next run's reconciliation (_reconcile_manifest_with_drive) detects and
    reports same-path duplicates, always keeping the oldest as canonical
    and never auto-deleting -- an admin sees it. That's strictly better
    than the previous behavior of silently dropping the file for up to a
    full day until the next scheduled run retried it."""
    from googleapiclient.errors import HttpError

    delay = RETRY_BASE_DELAY_SECONDS
    last_error = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        _pace()
        try:
            return request_factory().execute(**execute_kwargs)
        except HttpError as exc:
            status = exc.resp.status if getattr(exc, "resp", None) is not None else None
            last_error = exc
            if _is_retryable_http_error(exc, status) and attempt < MAX_RETRY_ATTEMPTS:
                sleep_for = min(delay, RETRY_MAX_DELAY_SECONDS) + random.uniform(0, 0.5)
                logger.warning(
                    "Drive API call failed with status %s (attempt %s/%s) -- retrying in %.1fs",
                    status, attempt, MAX_RETRY_ATTEMPTS, sleep_for,
                )
                time.sleep(sleep_for)
                delay *= 2
                continue
            raise
        except (TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt < MAX_RETRY_ATTEMPTS:
                sleep_for = min(delay, RETRY_MAX_DELAY_SECONDS) + random.uniform(0, 0.5)
                logger.warning(
                    "Drive API call failed with a transient network error (%s: %s) (attempt %s/%s) -- "
                    "retrying in %.1fs",
                    type(exc).__name__, exc, attempt, MAX_RETRY_ATTEMPTS, sleep_for,
                )
                time.sleep(sleep_for)
                delay *= 2
                continue
            raise
    raise last_error


def _escape_drive_query_value(value):
    """Drive query string literals are single-quoted; a literal backslash or
    single quote inside must be backslash-escaped (per Drive API query
    syntax) -- folder names here can contain either (e.g. "7'D MAT")."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _get_or_create_folder(service, manifest, relative_dir):
    """relative_dir uses forward slashes, e.g. "S.S IMAGE/7'D MAT", or ""
    for the backup root itself. Mirrors the local data/ structure inside
    Drive (Part 3.3) so the backup is human-browsable, not a flat dump.
    Folder IDs are cached in manifest["folders"] so repeat runs never
    re-query Drive for folders they already know about.

    A cache hit is trusted without a verifying round-trip (that would cost
    an extra Drive call per folder on every run for a case that's normally
    never true) -- staleness is instead caught where it actually surfaces:
    a 404 from the create call below when this folder's contents turn out
    to have a since-deleted parent. See _create_folder_self_healing()."""
    if relative_dir in ("", "."):
        return Config.GDRIVE_BACKUP_FOLDER_ID

    folders = manifest.setdefault("folders", {})
    cached = folders.get(relative_dir)
    if cached:
        return cached

    parent_dir, folder_name = os.path.split(relative_dir)
    parent_id = _get_or_create_folder(service, manifest, parent_dir)

    query = (
        f"name = '{_escape_drive_query_value(folder_name)}' and '{parent_id}' in parents "
        f"and mimeType = '{DRIVE_FOLDER_MIME_TYPE}' and trashed = false"
    )
    existing = call_with_backoff(
        lambda: service.files().list(q=query, fields="files(id, name)", pageSize=1, spaces="drive")
    )
    matches = existing.get("files", [])
    if matches:
        folder_id = matches[0]["id"]
    else:
        folder_id = _create_folder_self_healing(service, manifest, relative_dir, parent_dir, parent_id, folder_name)
        logger.info("Created Drive folder for %s", relative_dir)

    folders[relative_dir] = folder_id
    save_manifest(manifest)
    return folder_id


def _create_folder_self_healing(service, manifest, relative_dir, parent_dir, parent_id, folder_name, allow_recovery=True):
    """Creates folder_name under parent_id. Drive rejects a create whose
    parents=[id] no longer exists with a 404 -- the exact shape of the bug
    this fixes (manifest["folders"][parent_dir] cached an id for a folder
    that was since deleted directly on Drive, e.g. by hand). On that
    specific failure, the stale cache entry is dropped and the parent is
    resolved fresh (which may itself recurse into the same recovery one
    level further up, if the drift goes deeper than one folder) before
    retrying once -- instead of aborting the whole run for one item."""
    from googleapiclient.errors import HttpError

    metadata = {"name": folder_name, "mimeType": DRIVE_FOLDER_MIME_TYPE, "parents": [parent_id]}
    try:
        created = call_with_backoff(lambda: service.files().create(body=metadata, fields="id"))
    except HttpError as exc:
        if allow_recovery and getattr(exc.resp, "status", None) == 404 and parent_dir not in ("", "."):
            logger.warning(
                "Cloud backup: manifest-cached folder id for %r is stale (404 from Drive) -- "
                "dropping it and recreating before retrying %r",
                parent_dir, relative_dir,
            )
            manifest.get("folders", {}).pop(parent_dir, None)
            save_manifest(manifest)
            fresh_parent_id = _get_or_create_folder(service, manifest, parent_dir)
            return _create_folder_self_healing(
                service, manifest, relative_dir, parent_dir, fresh_parent_id, folder_name, allow_recovery=False
            )
        raise
    return created["id"]


def _update_existing_file(service, drive_file_id, full_path):
    from googleapiclient.http import MediaFileUpload

    call_with_backoff(
        lambda: service.files().update(
            fileId=drive_file_id, media_body=MediaFileUpload(full_path, resumable=True)
        )
    )
    return drive_file_id


def _create_file_self_healing(service, manifest, relative_path, parent_dir, parent_id, full_path, allow_recovery=True):
    """Mirrors _create_folder_self_healing() for the file-create call: if
    parent_id is a manifest-cached folder id that Drive no longer recognizes
    (404 -- the parent folder was deleted directly on Drive after being
    cached, without any child folder create along the way to notice it
    sooner), drop that cache entry, resolve the parent fresh, and retry once."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    metadata = {"name": os.path.basename(relative_path), "parents": [parent_id]}
    try:
        created = call_with_backoff(
            lambda: service.files().create(
                body=metadata, media_body=MediaFileUpload(full_path, resumable=True), fields="id"
            )
        )
    except HttpError as exc:
        if allow_recovery and getattr(exc.resp, "status", None) == 404 and parent_dir not in ("", "."):
            logger.warning(
                "Cloud backup: manifest-cached folder id for %r is stale (404 from Drive) -- "
                "dropping it and recreating before retrying upload of %s",
                parent_dir, relative_path,
            )
            manifest.get("folders", {}).pop(parent_dir, None)
            save_manifest(manifest)
            fresh_parent_id = _get_or_create_folder(service, manifest, parent_dir)
            return _create_file_self_healing(
                service, manifest, relative_path, parent_dir, fresh_parent_id, full_path, allow_recovery=False
            )
        raise
    return created["id"]


def _upload_or_update_file(service, manifest, relative_path, full_path, content_hash=None):
    from googleapiclient.errors import HttpError

    parent_dir = os.path.dirname(relative_path)
    parent_id = _get_or_create_folder(service, manifest, parent_dir)

    # A fresh MediaFileUpload per attempt (not shared across retries) --
    # the underlying file stream shouldn't be reused after a failed attempt.
    entry = manifest["files"].get(relative_path)
    drive_file_id = None
    if entry and entry.get("drive_file_id"):
        try:
            drive_file_id = _update_existing_file(service, entry["drive_file_id"], full_path)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) != 404:
                raise
            # Stale manifest-cached file id -- e.g. the file was deleted (or
            # replaced) directly on Drive since the manifest last saw it.
            # Fall through to create a fresh file below instead of failing
            # this item outright (Part 1: self-healing against state drift).
            logger.warning(
                "Cloud backup: manifest-cached file id for %s is stale (404 from Drive) -- "
                "recreating instead of updating",
                relative_path,
            )

    if drive_file_id is None:
        drive_file_id = _create_file_self_healing(service, manifest, relative_path, parent_dir, parent_id, full_path)

    size, mtime_ns = _file_stat_signature(full_path)
    new_entry = {"drive_file_id": drive_file_id, "size": size, "mtime_ns": mtime_ns}
    if content_hash is None and relative_path == INCLUDED_DB_FILE:
        # The snapshot is rebuilt every run, so its mtime always differs; a
        # stored hash is what lets an unchanged database be recognised
        # (instead of being re-uploaded every night).
        content_hash = _content_hash(full_path)
    if content_hash is not None:
        # Only ever set for files that have hit the ambiguous same-size/
        # different-mtime path in classify_changes -- lets a future touch
        # of this same file resolve from a stat alone next time round,
        # rather than plain files getting a hash field they'll never need.
        new_entry["hash"] = content_hash
    manifest["files"][relative_path] = new_entry
    save_manifest(manifest)


def _delete_file(service, manifest, relative_path):
    """Moves the Drive copy to Drive's Trash (recoverable for 30 days) --
    never a permanent files().delete. Every listing in this module asks for
    trashed = false, so a trashed file simply stops existing as far as
    reconciliation, verification and change detection are concerned; if it
    is restored from Trash later, reconciliation adopts it again whenever a
    matching local file exists."""
    from googleapiclient.errors import HttpError

    entry = manifest["files"].get(relative_path)
    if entry and entry.get("drive_file_id"):
        try:
            call_with_backoff(lambda: service.files().update(
                fileId=entry["drive_file_id"], body={"trashed": True}, fields="id"
            ))
        except HttpError as exc:
            if getattr(exc.resp, "status", None) != 404:
                raise
            # already gone on Drive's side -- fine, just drop it locally too
    del manifest["files"][relative_path]
    save_manifest(manifest)


# ---------------------------------------------------------------------------
# Verification (Part 5)
# ---------------------------------------------------------------------------

# Bounded concurrency for the verification listing below -- enough to keep
# several folder-listing calls' network round-trips overlapping instead of
# sitting idle between them, while _pace()'s own MIN_CALL_INTERVAL_SECONDS
# floor (unchanged, still respected by every single call) keeps the actual
# dispatch rate well under Drive's per-user limits regardless of thread count.
LIST_CONCURRENCY = 15

_thread_local = threading.local()

# Every googleapiclient service built for a run: the main one (apply phase) and one per listing-pool
# thread. Each service is a reference CYCLE that owns an open socket (httplib2 keeps the connection
# alive), so letting it go out of scope does not close anything: the sockets stay open until a cyclic
# GC pass happens to run. On Render (Python 3.14, incremental GC) every run left 16 more sockets open
# (15 pool threads + the main service) and RSS grew ~70 MB per run. They are closed explicitly instead
# -- see _close_services(), _list_drive_tree() and run_sync()'s finally.
_services_lock = threading.Lock()
_services = {"main": [], "pool": []}


def _register_service(kind, service):
    with _services_lock:
        _services[kind].append(service)
    return service


def _close_services(kind=None):
    """Closes (http.close()) and forgets the registered Drive clients. Never raises; returns how many
    were closed."""
    with _services_lock:
        kinds = [kind] if kind else list(_services)
        victims = [service for k in kinds for service in _services[k]]
        for k in kinds:
            _services[k] = []
    closed = 0
    for service in victims:
        try:
            close = getattr(service, "close", None)
            if close:
                close()
                closed += 1
        except Exception:
            logger.debug("Cloud backup: closing a Drive client failed", exc_info=True)
    return closed


def _release_run_resources():
    """Runs on EVERY exit path of run_sync (success, partial, held-back, stopped, failed, not
    authorized): close all Drive clients, drop the thread-local reference, collect leftover cycles."""
    try:
        _close_services()
        _thread_local.drive_service = None
    except Exception:
        logger.debug("Cloud backup: releasing run resources failed", exc_info=True)
    gc.collect()


def _resource_snapshot():
    """Open file descriptors / sockets / RSS of this process, for the 'sync finished' line. Reuses the
    /proc breakdown helper from app.py when it is loaded (Linux/Render); otherwise falls back to psutil.
    Never raises."""
    snap = {"fds": None, "sockets": None, "rss_mb": None}
    try:
        import psutil
        proc = psutil.Process()
        snap["rss_mb"] = round(proc.memory_info().rss / 1048576, 1)
        snap["fds"] = proc.num_fds() if hasattr(proc, "num_fds") else proc.num_handles()
        app_module = sys.modules.get("app")
        helper = getattr(app_module, "_fd_breakdown", None)
        breakdown = helper() if helper else None
        if breakdown:
            snap["sockets"] = breakdown.get("sockets")
        else:
            getter = getattr(proc, "net_connections", None) or proc.connections
            snap["sockets"] = len(getter(kind="inet"))
    except Exception:
        pass
    return snap


def _log_run_resources(status, before, after):
    logger.info(
        "Cloud backup sync finished (%s): open fds %s -> %s, sockets %s -> %s, RSS %s -> %s MB (before -> after this run)",
        status, before["fds"], after["fds"], before["sockets"], after["sockets"], before["rss_mb"], after["rss_mb"],
    )
    if before["sockets"] is not None and after["sockets"] is not None and after["sockets"] - before["sockets"] > 2:
        logger.warning(
            "Cloud backup: %s more sockets are open after this run than before it -- possible connection leak "
            "(they should be back to the starting level)", after["sockets"] - before["sockets"],
        )


def _get_thread_local_drive_service():
    """A fresh googleapiclient service -- and therefore a fresh, unshared
    httplib2 transport -- per thread, used only by the concurrent
    verification listing below. Necessary because googleapiclient's http
    transport is NOT thread-safe: sharing one `service` object across
    threads was confirmed for real to corrupt the SSL connection
    (ssl.SSLError: DECRYPTION_FAILED_OR_BAD_RECORD_MAC), not just serialize
    or slow down. Cheap to build -- no network call, since cache_discovery
    is off but well-known APIs like Drive v3 ship a bundled static
    discovery doc -- and reuses the same already-valid/refreshed
    credentials already loaded from disk this run, so no extra OAuth
    traffic per thread."""
    service = getattr(_thread_local, "drive_service", None)
    if service is None:
        from googleapiclient.discovery import build

        credentials = _load_saved_credentials()
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        _register_service("pool", service)
        _thread_local.drive_service = service
    return service


def _list_one_folder_entries(folder_id, relative_dir):
    """Lists every direct child of one folder (all pages) -- both the file
    entries at this level (each tagged with its full relative path, so the
    caller never needs a second pass to reconstruct it) and the immediate
    subfolders to recurse into. Uses a thread-local service (see above),
    still through call_with_backoff for the same per-call retry/backoff
    behavior as every other Drive API call in this module.

    Requests id/size/md5Checksum/createdTime (not just id/mimeType/size
    like the old count-only listing) -- md5Checksum and createdTime are
    what let reconciliation (below) tell a genuine content match from a
    coincidence, and pick which of several same-path duplicates is the
    original."""
    service = _get_thread_local_drive_service()
    file_entries = []   # (relative_path, {id, size, md5, created_time})
    subfolders = []     # (folder_id, relative_dir)
    page_token = None
    while True:
        response = call_with_backoff(
            lambda: service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size, md5Checksum, createdTime)",
                pageSize=1000,
                pageToken=page_token,
                spaces="drive",
            )
        )
        for item in response.get("files", []):
            name = item.get("name") or ""
            child_path = f"{relative_dir}/{name}" if relative_dir else name
            if item.get("mimeType") == DRIVE_FOLDER_MIME_TYPE:
                subfolders.append((item["id"], child_path))
            else:
                file_entries.append((child_path, {
                    "id": item["id"],
                    "size": int(item.get("size") or 0),
                    "md5": item.get("md5Checksum"),
                    "created_time": item.get("createdTime") or "",
                }))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return file_entries, subfolders


def _list_drive_tree(root_folder_id):
    """ONE full recursive listing of the backup folder (Part 1), walked
    concurrently (bounded to LIST_CONCURRENCY folders in flight) -- same
    dispatch shape as the old count-only listing this replaces, every
    folder still listed exactly once. Returns relative_path -> [ {id,
    size, md5, created_time}, ... ]; a list per path, not a single dict,
    because Drive allows more than one file with the same name in the same
    folder -- real duplicates already on Drive that reconciliation has to
    recognize rather than silently pick one of at random.

    This single listing is reused for BOTH reconciliation and the run's
    verification (see run_sync) instead of listing twice -- the old code
    re-listed Drive from scratch again after every sync just to verify,
    which for a large/duplicated folder (one real incident hit 16,000+
    files) was real, avoidable, repeated work and part of what caused a
    memory-limit crash during verification.

    Raises on any listing failure (network, auth, quota -- after
    call_with_backoff's own retries are exhausted). Callers decide what
    "no usable listing this run" means for them; see run_sync, which skips
    reconciliation and the projected verification below rather than
    guessing from partial data."""
    entries = {}
    pending = {(root_folder_id, "")}
    in_flight = {}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=LIST_CONCURRENCY) as pool:
            while pending or in_flight:
                while pending and len(in_flight) < LIST_CONCURRENCY:
                    folder_id, relative_dir = pending.pop()
                    future = pool.submit(_list_one_folder_entries, folder_id, relative_dir)
                    in_flight[future] = (folder_id, relative_dir)

                done, _pending = concurrent.futures.wait(in_flight.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    in_flight.pop(future)
                    file_entries, subfolders = future.result()
                    for relative_path, info in file_entries:
                        entries.setdefault(relative_path, []).append(info)
                    pending.update(subfolders)
    finally:
        # The pool's threads are finished here (the with-block joins them), so every per-thread Drive
        # client can be closed now -- success or error -- instead of waiting for the garbage collector.
        _close_services("pool")

    return entries


def _canonicalize_drive_listing(drive_listing):
    """Picks the oldest Drive file per relative path as the one
    reconciliation and verification below treat as canonical, and reports
    every extra (younger) file at that same path as a duplicate (Part 4)
    -- never touched or deleted, just surfaced so an admin can clean them
    up by hand if they want to. createdTime is an RFC3339 string, which
    sorts correctly as plain text."""
    canonical = {}
    duplicates = []
    for relative_path, drive_entries in drive_listing.items():
        if len(drive_entries) > 1:
            ordered = sorted(drive_entries, key=lambda entry: entry.get("created_time") or "")
            canonical[relative_path] = ordered[0]
            duplicates.append({
                "relative_path": relative_path,
                "kept_drive_file_id": ordered[0]["id"],
                "duplicate_drive_file_ids": [entry["id"] for entry in ordered[1:]],
            })
        else:
            canonical[relative_path] = drive_entries[0]
    return canonical, duplicates


def _reconcile_manifest_with_drive(manifest, current_files, drive_listing):
    """Closes the two real drift directions a stale manifest can fall into
    (see this module's docstring... actually see the two real incidents
    that motivated this function):

      (a) Drive already has files the manifest doesn't know about -- e.g.
          a fresh/empty manifest pointed at an already-populated Drive
          folder (a new deployment reusing an existing backup folder).
          Without this, every one of those paths looks "new" to
          classify_changes and gets uploaded a second time, creating real
          duplicates on Drive (one real run went from an expected 8,044
          files to 16,102).
      (b) the manifest references Drive file ids that no longer exist --
          the Drive folder was emptied, or files removed, by hand.
          Without this, classify_changes trusts the stale manifest
          entry's (size, mtime) and treats the file as unchanged forever,
          so a sync never notices anything is wrong; only a fresh
          verification listing caught it, after the fact, with nothing
          done to actually fix it.

    Only ever mutates manifest["files"] in memory -- the caller saves it.
    Never deletes or modifies anything on Drive itself; a size mismatch is
    handled by updating that file in place next (via the existing
    drive_file_id, see _upload_or_update_file's own "update over create"
    preference), never by creating a second copy."""
    manifest_files = manifest.setdefault("files", {})
    canonical, duplicates = _canonicalize_drive_listing(drive_listing)

    # (b) manifest -> Drive: drop any manifest entry whose recorded Drive
    # file id isn't present ANYWHERE in the current listing (a file id is
    # globally unique, not just meaningful at its own path) -- classify_
    # changes then sees no entry at all and correctly treats it as new.
    all_known_drive_ids = {entry["id"] for entries in drive_listing.values() for entry in entries}
    dropped = 0
    for relative_path in list(manifest_files.keys()):
        drive_file_id = manifest_files[relative_path].get("drive_file_id")
        if drive_file_id and drive_file_id not in all_known_drive_ids:
            del manifest_files[relative_path]
            dropped += 1

    # (a) Drive -> manifest: for every path Drive already has that isn't
    # currently tracked (never was, or step (b) just dropped it) AND that
    # corresponds to a real local file we're actually backing up right
    # now, adopt the canonical Drive copy instead of re-uploading.
    #
    # Deliberately skipped when there's no matching local file: adopting
    # it would hand that path straight to the existing deleted_paths logic
    # on the very next run (anything in the manifest but not in
    # current_files gets deleted from Drive) -- silently turning an
    # unexplained Drive-only file into an automatic deletion, which this
    # fix explicitly must never do.
    adopted = 0
    for relative_path, drive_info in canonical.items():
        if relative_path in manifest_files or relative_path not in current_files:
            continue
        try:
            local_size, local_mtime_ns = _file_stat_signature(current_files[relative_path])
        except OSError:
            continue

        if drive_info["size"] == local_size:
            # Matches on size (md5 too, recorded for a future ambiguous
            # same-size/different-mtime check when Drive returned one) --
            # safe to mark unchanged outright, using the real local mtime
            # so classify_changes' fast path accepts it immediately
            # instead of hashing this file on this and every future run.
            entry = {"drive_file_id": drive_info["id"], "size": drive_info["size"], "mtime_ns": local_mtime_ns}
            if drive_info.get("md5"):
                entry["hash"] = drive_info["md5"]
        else:
            # Sizes differ -- genuinely out of date on one side. Recording
            # DRIVE's size (not local) here is what makes classify_changes
            # detect this as "changed" against the real local file on its
            # very next comparison; the apply phase's _upload_or_update_
            # file() already prefers an in-place update over a fresh
            # create whenever entry["drive_file_id"] is set, so this
            # becomes an update, never a second copy, with no changes
            # needed there at all. mtime_ns is set to a value no real
            # os.stat() can ever produce so it's never mistaken for a
            # real match.
            entry = {"drive_file_id": drive_info["id"], "size": drive_info["size"], "mtime_ns": -1}
        manifest_files[relative_path] = entry
        adopted += 1

    return {"dropped": dropped, "adopted": adopted, "duplicates": duplicates}


def _verify_from_listing(drive_listing, manifest, uploaded_paths, updated_paths, deleted_paths):
    """Builds this run's verification from the ALREADY-fetched pre-sync
    listing (Part 1 -- the same one reconciliation used above), projected
    forward by exactly the operations this run actually performed, instead
    of a second full recursive Drive listing. One listing per run, reused
    for both jobs, is the whole point of Part 1 -- see _list_drive_tree's
    docstring for the incident this avoids repeating.

    Only reasons about paths this run actually touched (successfully --
    uploaded_paths/updated_paths/deleted_paths are the post-success lists
    the caller built, not the pre-attempt ones); anything neither touched
    nor already reconciled keeps its pre-sync canonical entry, which is
    exactly correct since nothing happened to it this run."""
    canonical, _duplicates = _canonicalize_drive_listing(drive_listing)
    projected = dict(canonical)

    manifest_files = manifest.get("files", {})
    for relative_path in uploaded_paths + updated_paths:
        entry = manifest_files.get(relative_path)
        if entry:
            projected[relative_path] = {"id": entry.get("drive_file_id"), "size": entry.get("size", 0)}
    for relative_path in deleted_paths:
        projected.pop(relative_path, None)

    manifest_count = len(manifest_files)
    manifest_size = sum(entry.get("size", 0) for entry in manifest_files.values())
    drive_count = len(projected)
    drive_size = sum(entry.get("size", 0) for entry in projected.values())

    ok = (drive_count == manifest_count) and (drive_size == manifest_size)
    discrepancy = None
    if not ok:
        discrepancy = (
            f"Manifest claims {manifest_count} file(s) / {manifest_size} bytes, "
            f"but Drive is projected to actually have {drive_count} file(s) / {drive_size} bytes "
            f"(projected from this run's own listing + operations, not a second live check)."
        )
        logger.warning("Cloud backup verification MISMATCH: %s", discrepancy)
    else:
        logger.info("Cloud backup verification OK: %s file(s), %s bytes", drive_count, drive_size)

    return {
        "ok": ok,
        "manifest_file_count": manifest_count,
        "manifest_total_size_bytes": manifest_size,
        "drive_file_count": drive_count,
        "drive_total_size_bytes": drive_size,
        "discrepancy": discrepancy,
        "method": "projected_from_initial_listing",
    }


def reset_manifest():
    """Admin action (Part 6): clears the local manifest entirely, so the
    NEXT sync starts from zero local tracking and reconciles fresh against
    whatever Drive actually has (adopting real existing files instead of
    re-uploading them -- see _reconcile_manifest_with_drive above) rather
    than uploading the whole catalog again. The manual escape hatch for
    when an admin already knows local tracking has drifted and doesn't
    want to wait for a scheduled run to discover it.

    Refuses while a sync is actively running, same acquire-in-caller /
    release-in-finally convention as SYNC_LOCK everywhere else in this
    module -- clearing the manifest out from under an in-flight sync would
    race its own save_manifest() calls and could resurrect entries the
    sync had already dropped."""
    if not SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "A sync is currently running -- wait for it to finish first."}
    try:
        save_manifest(_empty_manifest())
        logger.info("Cloud backup: manifest reset to empty by admin action")
        return {"ok": True}
    finally:
        SYNC_LOCK.release()


# ---------------------------------------------------------------------------
# Sync run (Parts 3-5)
# ---------------------------------------------------------------------------

def _max_deletions_allowed(tracked_count):
    if Config.BACKUP_MAX_DELETIONS is not None:
        return Config.BACKUP_MAX_DELETIONS
    return max(DELETION_GUARD_MIN, int(tracked_count * DELETION_GUARD_FRACTION))


def _build_run_summary(started_at, status, added=0, updated=0, deleted=0, skipped=0, error=None, verification=None, phase_timing=None, reconciliation=None, deletions_held_back=0):
    return {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "status": status,  # "success" | "partial" | "failed" | "stopped" | "not_authorized"
        "added": added,
        "updated": updated,
        "deleted": deleted,
        "skipped": skipped,
        "error": error,
        "verification": verification,
        "phase_timing": phase_timing,
        "reconciliation": reconciliation,  # {dropped, adopted, duplicates: [...]} | None (listing failed/skipped)
        "deletions_held_back": deletions_held_back,  # >0: the mass-deletion guard skipped every deletion this run
    }


def run_sync(triggered_by="schedule"):
    """Runs one full incremental sync. Safe to call from either the
    scheduled timer or the System panel's manual "Sync Now" button.

    Caller is responsible for holding SYNC_LOCK for the duration of this
    call (same convention as app.py's run_full_refresh_job() and
    FULL_REFRESH_LOCK) -- see schedule()'s _job below and
    system_cloud_backup_sync_now() in app.py for the two call sites. This
    keeps "is a sync already running" a single atomic acquire at the call
    site, rather than a check-then-act race between two triggers (a second
    tab, a race on page refresh, a scheduled run landing mid-manual-run).

    Cooperative cancellation: checks _cancel_event before starting each new
    file/folder operation (never mid-write) and stops cleanly at the next
    one if it's set, so a stopped run's manifest reflects only genuinely
    completed items and next run resumes the rest normally."""
    if not is_configured():
        _log_not_configured_once()
        return {"configured": False}

    started_at = datetime.now().isoformat()
    resources_before = _resource_snapshot()
    _cancel_event.clear()
    with _status_lock:
        _status["running"] = True
    with _progress_lock:
        # Set True here, not just inside _progress_reset() below -- that
        # call happens only after classify_changes()/save_manifest() have
        # already run, which for a large S.S IMAGE folder (thousands of
        # files to stat) can take real seconds. Without this, a Stop Sync
        # click during that enumeration window would find request_stop()
        # still seeing "running": False and silently drop the request.
        _progress["running"] = True
        _progress["stop_requested"] = False

    added = updated = deleted_count = 0
    errors = []
    cancelled = False
    verification = None
    deletions_held_back = 0
    held_back_message = None
    try:
        logger.info("Cloud backup sync starting (triggered_by=%s)", triggered_by)
        service = build_drive_service()
        manifest = load_manifest()

        # Phase timing (Part 7): logged separately so a slow run's actual
        # bottleneck is visible without guessing. listing is the one real
        # recursive Drive walk (Part 1, reused below for both reconciliation
        # and verification -- see _list_drive_tree's docstring); change
        # detection is local disk I/O (stat every included file); apply is
        # real Drive API calls for only what actually changed. These can
        # have very different costs at real scale.
        # Consistent database snapshot (never the live WAL-mode file). If it
        # fails, the database is left out of this run AND protected from the
        # deletion pass below, so the previous good Drive copy stays put.
        snapshot_path = None
        snapshot_failed = False
        if os.path.isfile(_database_path()):
            try:
                snapshot_path = _snapshot_database(_database_path())
            except Exception as exc:
                snapshot_failed = True
                logger.exception("Cloud backup: database snapshot failed -- keeping the previous Drive copy")
                errors.append(f"database snapshot failed ({_short_error_text(exc)}); previous Drive copy kept")
        current_files = dict(_iter_included_files(db_snapshot_path=snapshot_path, skip_db=snapshot_failed))

        phase_started = time.monotonic()
        reconciliation = None
        listing_error = None
        drive_listing = None
        try:
            drive_listing = _list_drive_tree(Config.GDRIVE_BACKUP_FOLDER_ID)
        except Exception as exc:
            listing_error = _short_error_text(exc)
            logger.exception(
                "Cloud backup: initial Drive listing failed -- skipping reconciliation and "
                "projected verification this run (sync itself still proceeds against the "
                "manifest as-is)"
            )
        listing_seconds = time.monotonic() - phase_started

        if drive_listing is not None:
            reconciliation = _reconcile_manifest_with_drive(manifest, current_files, drive_listing)
            save_manifest(manifest)
            logger.info(
                "Cloud backup: reconciliation dropped %s stale manifest entr(ies), adopted %s "
                "existing Drive file(s) instead of re-uploading, found %s duplicate path(s) on "
                "Drive (reported only -- nothing deleted) (listing took %.2fs)",
                reconciliation["dropped"], reconciliation["adopted"], len(reconciliation["duplicates"]),
                listing_seconds,
            )

        phase_started = time.monotonic()
        drive_md5_by_id = None
        if drive_listing is not None:
            canonical_listing, _dupes = _canonicalize_drive_listing(drive_listing)
            drive_md5_by_id = {info["id"]: info["md5"] for info in canonical_listing.values() if info.get("md5")}
        new_paths, changed_paths, deleted_paths, unchanged_count, computed_hashes = classify_changes(
            manifest, current_files, drive_md5_by_id
        )
        if snapshot_failed:
            deleted_paths = [p for p in deleted_paths if p != INCLUDED_DB_FILE]

        # Mass-deletion guard: a run that would trash an unusually large number
        # of files (empty/unmounted image folder, wrong data dir...) holds ALL
        # deletions back. Uploads/updates proceed; the manifest keeps every
        # held-back entry, so once the files return the next run is clean.
        allowed_deletions = _max_deletions_allowed(len(manifest.get("files", {})))
        if len(deleted_paths) > allowed_deletions:
            deletions_held_back = len(deleted_paths)
            held_back_message = (
                f"{deletions_held_back} deletions held back: local files missing "
                f"(limit {allowed_deletions} per run; set BACKUP_MAX_DELETIONS to allow)"
            )
            logger.warning("Cloud backup: %s", held_back_message)
            deleted_paths = []
        # Persists any in-place mtime_ns/hash fixes classify_changes made
        # for ambiguous-but-actually-unchanged files, even if nothing below
        # ends up needing to run (e.g. every other file was also unchanged).
        save_manifest(manifest)

        change_detection_seconds = time.monotonic() - phase_started
        logger.info(
            "Cloud backup: %s new, %s changed, %s deleted, %s unchanged (change detection took %.2fs)",
            len(new_paths), len(changed_paths), len(deleted_paths), unchanged_count, change_detection_seconds,
        )
        _progress_reset(len(deleted_paths) + len(new_paths) + len(changed_paths), unchanged_count)

        phase_started = time.monotonic()
        uploaded_ok = []
        updated_ok = []
        deleted_ok = []

        for relative_path in deleted_paths:
            if _cancel_event.is_set():
                cancelled = True
                break
            _progress_note("Deleting", relative_path)
            try:
                _delete_file(service, manifest, relative_path)
                deleted_count += 1
                deleted_ok.append(relative_path)
                _progress_finish_item("deleted")
            except Exception as exc:
                logger.exception("Cloud backup: failed to delete %s from Drive", relative_path)
                short_error = _short_error_text(exc)
                errors.append(f"delete failed: {relative_path} ({short_error})")
                _progress_note_failure(relative_path, short_error)
                _progress_finish_item("failed")

        for relative_path in new_paths:
            if cancelled:
                break
            if _cancel_event.is_set():
                cancelled = True
                break
            _progress_note("Uploading", relative_path)
            try:
                _upload_or_update_file(service, manifest, relative_path, current_files[relative_path])
                added += 1
                uploaded_ok.append(relative_path)
                _progress_finish_item("uploaded")
            except Exception as exc:
                logger.exception("Cloud backup: failed to upload %s", relative_path)
                short_error = _short_error_text(exc)
                errors.append(f"upload failed: {relative_path} ({short_error})")
                _progress_note_failure(relative_path, short_error)
                _progress_finish_item("failed")

        for relative_path in changed_paths:
            if cancelled:
                break
            if _cancel_event.is_set():
                cancelled = True
                break
            _progress_note("Updating", relative_path)
            try:
                _upload_or_update_file(
                    service, manifest, relative_path, current_files[relative_path],
                    content_hash=computed_hashes.get(relative_path),
                )
                updated += 1
                updated_ok.append(relative_path)
                _progress_finish_item("updated")
            except Exception as exc:
                logger.exception("Cloud backup: failed to update %s", relative_path)
                short_error = _short_error_text(exc)
                errors.append(f"update failed: {relative_path} ({short_error})")
                _progress_note_failure(relative_path, short_error)
                _progress_finish_item("failed")

        apply_seconds = time.monotonic() - phase_started
        logger.info("Cloud backup: apply phase (upload/update/delete) took %.2fs", apply_seconds)

        verification_seconds = 0.0
        if cancelled:
            status = "stopped"
            logger.info("Cloud backup sync stopped by request (triggered_by=%s)", triggered_by)
        else:
            phase_started = time.monotonic()
            if drive_listing is not None:
                verification = _verify_from_listing(drive_listing, manifest, uploaded_ok, updated_ok, deleted_ok)
            verification_seconds = time.monotonic() - phase_started
            if errors or deletions_held_back:
                status = "partial"
            elif verification is None:
                # Listing failed up front -- honest about not actually
                # knowing whether Drive matches, rather than claiming
                # "success" on faith the way the old code effectively did
                # whenever its own fresh verification listing happened to
                # fail (it just left `errors` empty and reported "success").
                status = "partial"
            elif not verification["ok"]:
                status = "partial"
            else:
                status = "success"
            logger.info(
                "Cloud backup sync finished: %s (verification took %.2fs)",
                status, verification_seconds,
            )
        logger.info(
            "Cloud backup: phase timing -- listing=%.2fs change_detection=%.2fs apply=%.2fs verification=%.2fs total=%.2fs",
            listing_seconds, change_detection_seconds, apply_seconds, verification_seconds,
            listing_seconds + change_detection_seconds + apply_seconds + verification_seconds,
        )

        summary = _build_run_summary(
            started_at, status,
            added=added, updated=updated, deleted=deleted_count, skipped=unchanged_count,
            error="; ".join(errors + ([held_back_message] if held_back_message else [])) if (errors or held_back_message) else (f"Drive listing failed: {listing_error}" if listing_error else None),
            deletions_held_back=deletions_held_back,
            verification=verification,
            phase_timing={
                "listing_seconds": round(listing_seconds, 2),
                "change_detection_seconds": round(change_detection_seconds, 2),
                "apply_seconds": round(apply_seconds, 2),
                "verification_seconds": round(verification_seconds, 2),
            },
            reconciliation=reconciliation,
        )
    except NotAuthorizedError as exc:
        # build_drive_service() never opens a browser itself (see
        # authorize() for the dedicated, separate action that does) -- so
        # this is a fast, immediate failure, not a hang, whether triggered
        # manually or from the scheduled timer.
        logger.warning("Cloud backup sync aborted -- not authorized: %s", exc)
        summary = _build_run_summary(
            started_at, "not_authorized",
            added=added, updated=updated, deleted=deleted_count,
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Cloud backup sync failed")
        summary = _build_run_summary(
            started_at, "failed",
            added=added, updated=updated, deleted=deleted_count,
            error=str(exc),
        )
    finally:
        _remove_snapshot()
        _release_run_resources()
        with _status_lock:
            _status["running"] = False
            _status["last_run"] = summary
            if summary and summary.get("status") == "success":
                _status["last_success_at"] = summary["finished_at"]
        _save_persisted_status()
        _progress_stop()
        _cancel_event.clear()

    _log_run_resources((summary or {}).get("status"), resources_before, _resource_snapshot())
    return {"configured": True, "running": False, "last_run": summary}


# ---------------------------------------------------------------------------
# Scheduling -- fixed daily IST slot (Config.CLOUD_BACKUP_DAILY_TIME,
# default "02:00"), replacing the old "run shortly after startup, then
# every CLOUD_BACKUP_INTERVAL seconds" behavior. CLOUD_BACKUP_INTERVAL is
# now only a fallback, used when CLOUD_BACKUP_DAILY_TIME is unset or
# unparseable (see _schedule_interval_fallback()).
#
# IST is a fixed UTC+5:30 offset applied by hand (IST_OFFSET below), not a
# real tz-database lookup -- this codebase already treats every naive
# datetime.now() as UTC by convention (see formatIST() in static/shared.js:
# it reads a naive ISO timestamp as UTC and renders it in Asia/Kolkata),
# which only works because both deployment targets (Render's container,
# and the office PC) keep their system clock on UTC. _now() below is a
# clearly-named alias for that same convention, not a real conversion --
# and the one seam the scheduling-math tests monkeypatch to simulate
# arbitrary restart times without waiting on real threading.Timer delays.
# ---------------------------------------------------------------------------

IST_OFFSET = timedelta(hours=5, minutes=30)
CATCHUP_STALE_THRESHOLD = timedelta(hours=30)
CATCHUP_DELAY_SECONDS = 10 * 60
CATCHUP_WINDOW_START = dt_time(20, 0)  # 20:00 IST
CATCHUP_WINDOW_END = dt_time(7, 0)  # 07:00 IST (window wraps past midnight)

_next_scheduled_run_utc = None  # what get_next_scheduled_backup_iso() reports


def _now():
    return datetime.now()


def _to_ist(dt_utc):
    return dt_utc + IST_OFFSET


def _to_utc(dt_ist):
    return dt_ist - IST_OFFSET


def _parse_daily_time(value):
    """"HH:MM" -> datetime.time, or None if unset/blank/malformed (callers
    fall back to the legacy CLOUD_BACKUP_INTERVAL behavior in that case)."""
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) != 2:
            raise ValueError("expected HH:MM")
        return dt_time(int(parts[0]), int(parts[1]))
    except ValueError:
        logger.warning(
            "Invalid CLOUD_BACKUP_DAILY_TIME=%r (expected HH:MM) -- falling back to CLOUD_BACKUP_INTERVAL",
            value,
        )
        return None


def _next_daily_slot_utc(now_utc, slot_time_ist):
    """The next occurrence of slot_time_ist (a datetime.time, IST) strictly
    after now_utc, as a naive UTC-convention datetime."""
    now_ist = _to_ist(now_utc)
    candidate_ist = datetime.combine(now_ist.date(), slot_time_ist)
    if candidate_ist <= now_ist:
        candidate_ist += timedelta(days=1)
    return _to_utc(candidate_ist)


def _in_catchup_window(now_ist):
    """20:00-07:00 IST, wrapping past midnight."""
    t = now_ist.time()
    return t >= CATCHUP_WINDOW_START or t < CATCHUP_WINDOW_END


def _is_last_success_stale(now_utc, last_success_at_iso):
    if not last_success_at_iso:
        return True
    try:
        last_success_dt = datetime.fromisoformat(last_success_at_iso)
    except ValueError:
        return True
    return (now_utc - last_success_dt) > CATCHUP_STALE_THRESHOLD


def _current_catchup_window_start_utc(now_utc):
    """The UTC-convention datetime at which the catch-up window (20:00-07:00
    IST, wrapping past midnight) CONTAINING now_utc began. E.g. now=02:00
    IST belongs to the window that started at 20:00 IST the previous
    calendar day, not "today's" 20:00 (which hasn't happened yet). Used by
    _catchup_already_attempted_this_window() to tell "already tried a
    catch-up during tonight's window" apart from "a new window has started
    since" -- restarts within the same window must not re-arm catch-up,
    but a genuinely new night must."""
    now_ist = _to_ist(now_utc)
    if now_ist.time() >= CATCHUP_WINDOW_START:
        window_start_ist = datetime.combine(now_ist.date(), CATCHUP_WINDOW_START)
    else:
        window_start_ist = datetime.combine(now_ist.date() - timedelta(days=1), CATCHUP_WINDOW_START)
    return _to_utc(window_start_ist)


def _catchup_already_attempted_this_window(now_utc, last_catchup_attempt_iso):
    if not last_catchup_attempt_iso:
        return False
    try:
        last_attempt_dt = datetime.fromisoformat(last_catchup_attempt_iso)
    except ValueError:
        return False
    return last_attempt_dt >= _current_catchup_window_start_utc(now_utc)


def _compute_startup_plan(now_utc, last_success_at_iso, last_catchup_attempt_iso, slot_time_ist):
    """Startup-only decision (see schedule()): whether to do a catch-up run
    or just wait for the next normal daily slot. A pure function of (now,
    last_success_at, last_catchup_attempt, slot_time) so the scheduling MATH
    is directly unit-testable against a fake clock without waiting on real
    threading.Timer delays. Returns (delay_seconds, kind, next_slot_utc)
    where kind is "catchup" or "slot".

    Catch-up requires all three: last success stale (or missing), currently
    in the 20:00-07:00 IST window, AND no catch-up already attempted since
    THIS window began (see _current_catchup_window_start_utc()) -- that
    third condition is what makes this safe across restarts, not just
    within one continuous process. Without it, a sync that keeps ending
    "partial" (never "success") would make _is_last_success_stale() stay
    permanently True, so EVERY restart landing in the window would re-arm
    catch-up, and the schedule could never advance to the real daily slot.
    The attempted-marker is set at decision time in schedule() (not at run
    completion), deliberately independent of whether that attempt
    ultimately succeeds, fails, or ends partial -- only whether one was
    tried this window."""
    next_slot_utc = _next_daily_slot_utc(now_utc, slot_time_ist)
    if (
        _is_last_success_stale(now_utc, last_success_at_iso)
        and _in_catchup_window(_to_ist(now_utc))
        and not _catchup_already_attempted_this_window(now_utc, last_catchup_attempt_iso)
    ):
        return (float(CATCHUP_DELAY_SECONDS), "catchup", next_slot_utc)
    return (max(0.0, (next_slot_utc - now_utc).total_seconds()), "slot", next_slot_utc)


def get_next_scheduled_backup_iso():
    return _next_scheduled_run_utc.isoformat() if _next_scheduled_run_utc else None


def _archive_activity_after_backup(outcome):
    """Archives expired user_activity months to their own Drive folder -- only right after a
    SUCCESSFUL scheduled backup, and only while the caller still holds SYNC_LOCK, so it can
    never overlap a backup (same "already running" skip as the backup itself). Never raises."""
    try:
        if not outcome or (outcome.get("last_run") or {}).get("status") != "success":
            return
        import activity_archive
        activity_archive.run_archive()
    except Exception as exc:
        logger.error("Activity archive could not run: %s", type(exc).__name__)


def _attempt_scheduled_run():
    """Shared by both scheduling modes (daily-slot and the legacy interval
    fallback): try run_sync(), respecting authorization and SYNC_LOCK,
    recording a skip reason (never raising) if it can't run right now --
    e.g. a slot landing while Sync Now (or the previous scheduled run) is
    still in progress."""
    if not is_authorized():
        message = "Not yet authorized -- run 'Authorize Google Drive' once from the System panel"
        logger.info("Skipping scheduled cloud backup -- %s", message)
        _record_skipped_run("not_authorized", message)
    elif not SYNC_LOCK.acquire(blocking=False):
        message = "A sync was already running at the scheduled time"
        logger.info("Skipping scheduled cloud backup -- %s", message)
        _record_skipped_run("already_running", message)
    else:
        try:
            outcome = run_sync(triggered_by="schedule")
            _archive_activity_after_backup(outcome)
        except Exception:
            logger.exception("Scheduled cloud backup run raised unexpectedly")
        finally:
            SYNC_LOCK.release()


def _run_scheduled_job_once(slot_time_ist):
    """Fires once per armed timer, for both the one-time startup catch-up
    and every normal daily slot: attempt the run, then always reschedule
    against the real NEXT daily slot -- the catch-up rule is a one-time
    startup exception only (see schedule()), never re-evaluated here, so a
    sync that keeps failing overnight can't busy-loop retrying every 10
    minutes."""
    global _timer, _next_scheduled_run_utc

    _attempt_scheduled_run()

    next_slot_utc = _next_daily_slot_utc(_now(), slot_time_ist)
    _next_scheduled_run_utc = next_slot_utc
    _timer = threading.Timer(max(0.0, (next_slot_utc - _now()).total_seconds()), lambda: _run_scheduled_job_once(slot_time_ist))
    _timer.daemon = True
    _timer.start()


def _schedule_interval_fallback(initial_delay):
    """Legacy behavior, used only when CLOUD_BACKUP_DAILY_TIME is unset or
    invalid: run after initial_delay, then every CLOUD_BACKUP_INTERVAL
    seconds after that (measured from when each run finishes, same as
    schedule_item_export)."""
    global _timer, _next_scheduled_run_utc

    def _job():
        global _timer, _next_scheduled_run_utc
        _attempt_scheduled_run()
        _next_scheduled_run_utc = _now() + timedelta(seconds=Config.CLOUD_BACKUP_INTERVAL)
        _timer = threading.Timer(Config.CLOUD_BACKUP_INTERVAL, _job)
        _timer.daemon = True
        _timer.start()

    delay = initial_delay if initial_delay and initial_delay > 0 else Config.CLOUD_BACKUP_INTERVAL
    _next_scheduled_run_utc = _now() + timedelta(seconds=delay)
    _timer = threading.Timer(delay, _job)
    _timer.daemon = True
    _timer.start()


def schedule(initial_delay=0):
    """Startup entry point -- called once from app.py's startup routine, but
    NOT just once ever: it reruns from scratch on every process restart
    (redeploy, crash, manual restart), each time re-deriving its decision
    purely from persisted state (_status), with no memory of its own past
    invocations beyond that. NEVER runs a backup immediately: the
    daily-slot path only ever schedules the next slot, and even the
    catch-up path (last success stale, currently 20:00-07:00 IST, AND no
    catch-up already attempted this window -- see _compute_startup_plan())
    waits 10 minutes rather than firing at process-start instant.
    `initial_delay` only affects the legacy CLOUD_BACKUP_INTERVAL fallback
    path -- the daily-slot path ignores it, since "next slot" is always
    computed from the real current time, not from whenever schedule()
    happened to be called."""
    global _timer, _next_scheduled_run_utc

    if not is_configured():
        _log_not_configured_once()
        return

    slot_time_ist = _parse_daily_time(Config.CLOUD_BACKUP_DAILY_TIME)
    if slot_time_ist is None:
        _schedule_interval_fallback(initial_delay)
        return

    with _status_lock:
        last_success_at_iso = _status.get("last_success_at")
        last_catchup_attempt_iso = _status.get("last_catchup_attempted_at")
    delay, kind, next_slot_utc = _compute_startup_plan(
        _now(), last_success_at_iso, last_catchup_attempt_iso, slot_time_ist
    )

    if kind == "catchup":
        logger.info(
            "Cloud backup: last success is stale (or missing), it's within the 20:00-07:00 IST catch-up "
            "window, and no catch-up has been attempted yet this window -- running once in %d minutes; "
            "the normal %s IST daily schedule resumes right after regardless of this run's outcome.",
            int(delay // 60), slot_time_ist.strftime("%H:%M"),
        )
        _next_scheduled_run_utc = _now() + timedelta(seconds=delay)
        # Recorded at decision time, not at run completion, and independent
        # of outcome -- see _status["last_catchup_attempted_at"]'s comment.
        # A restart seconds from now must already see this, or it would
        # re-arm its own catch-up on top of the one this process just armed.
        with _status_lock:
            _status["last_catchup_attempted_at"] = _now().isoformat()
        _save_persisted_status()
    else:
        logger.info(
            "Cloud backup: next scheduled run at %s IST",
            _to_ist(next_slot_utc).strftime("%Y-%m-%d %H:%M"),
        )
        _next_scheduled_run_utc = next_slot_utc

    _timer = threading.Timer(delay, lambda: _run_scheduled_job_once(slot_time_ist))
    _timer.daemon = True
    _timer.start()
