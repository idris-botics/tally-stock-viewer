"""Archive whole IST months of user_activity to a SEPARATE Google Drive folder, then (and only
then) remove exactly those rows from the live table.

Nothing may ever be lost, so for each month that is entirely older than the raw window:
  1. the month's daily/monthly summaries must exist (activity_tiers) -- otherwise refuse;
  2. the rows are streamed (chunks of CHUNK_ROWS) into data/activity_archive/YYYY-MM.jsonl.gz
     (gzip with a fixed timestamp, so the same rows always give the same bytes). The local
     file is immutable: it is never overwritten;
  3. it is uploaded to the Drive folder "SuperSeatings Activity Archive" (the root of the
     authorised Drive -- NOT inside the backup folder, which the nightly backup mirrors and
     verifies). It is uploaded under a temporary name and renamed only after its size and
     md5 were re-read from Drive and matched, so a half-finished upload can never be mistaken
     for the archive; an existing archive file on Drive is never overwritten;
  4. row count + checksum of the local file are compared with the rows still in the table;
  5. only then are the rows deleted, in ONE transaction that first re-counts them.
Any failed step deletes nothing, logs one ERROR with the exception class name only, and the
next daily run starts over. ACTIVITY_ARCHIVE_DRY_RUN=1 does steps 1-4 but never step 5.

Runs only from cloud_backup's scheduled job, inside SYNC_LOCK, after a successful backup, so
it can never overlap a backup. Every Drive client is closed in a finally.
"""
import gzip
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import activity_tiers as tiers
import database as db
from config import Config

logger = logging.getLogger(__name__)

FOLDER_NAME = "SuperSeatings Activity Archive"
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_ROOT_ID = "root"
CHUNK_ROWS = 2000
UPLOADING_SUFFIX = ".uploading"


class ArchiveError(Exception):
    """Base class; only the class name is ever logged."""


class SummaryMissing(ArchiveError): pass
class ArchiveRowsMismatch(ArchiveError): pass
class DriveVerifyFailed(ArchiveError): pass
class DriveFileConflict(ArchiveError): pass
class PruneCountMismatch(ArchiveError): pass


def _data_dir():
    return os.path.dirname(os.path.abspath(Config.DB_PATH))


def archive_dir():
    return os.path.join(_data_dir(), "activity_archive")


def folder_id_path():
    return os.path.join(_data_dir(), ".activity_archive_folder.json")


def archive_name(month):
    return f"{month}.jsonl.gz"


def dry_run_enabled():
    return os.environ.get("ACTIVITY_ARCHIVE_DRY_RUN", "").strip().lower() not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------- rows <-> lines

def _core(row):
    return [row["id"], row["user_id"], row["at_utc"], row["action"], row["detail"]]


def _line(row):
    return (json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _core_digest_update(digest, row):
    digest.update(json.dumps(_core(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def _iter_db_rows(month):
    """Rows of the month in id order, fetched CHUNK_ROWS at a time (never the whole table).
    Username is a snapshot (no password/access-code column is ever selected)."""
    start, end = tiers.month_bounds_utc(month)
    with db.get_connection() as conn:
        cursor = conn.execute(
            "SELECT a.id, a.user_id, u.username, a.at_utc, a.action, a.detail FROM user_activity a "
            "LEFT JOIN users u ON u.id = a.user_id WHERE a.at_utc >= ? AND a.at_utc < ? ORDER BY a.id",
            (start, end))
        while True:
            chunk = cursor.fetchmany(CHUNK_ROWS)
            if not chunk:
                return
            for r in chunk:
                yield {"id": r[0], "user_id": r[1], "username": r[2], "at_utc": r[3], "action": r[4], "detail": r[5]}


def _iter_archive_cores(path):
    with gzip.open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                yield _core(json.loads(line))


def rows_covered_by_archive(month, path):
    """Streams the month's live rows (id order) against the archive file (id order) and
    returns how many live rows there are, raising ArchiveRowsMismatch unless EVERY live row
    is in the file with identical id/user/time/action/detail. (The file may hold more rows
    than the table, e.g. a dealer account deleted since -- never fewer.)"""
    archived = _iter_archive_cores(path)
    live = 0
    gen = _iter_db_rows(month)
    try:
        for row in gen:
            want = _core(row)
            found = None
            for cand in archived:
                if cand[0] >= want[0]:
                    found = cand
                    break
            if found != want:
                raise ArchiveRowsMismatch("a live row is not in the archive file")
            live += 1
    finally:
        gen.close()
    return live


def scan_archive_file(path):
    """(row count, core checksum, size, md5) of an archive file, read in a stream."""
    digest, count, md5 = hashlib.sha256(), 0, hashlib.md5()
    size = 0
    with open(path, "rb") as raw:
        while True:
            block = raw.read(1 << 16)
            if not block:
                break
            md5.update(block)
            size += len(block)
    with gzip.open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                _core_digest_update(digest, json.loads(line))
                count += 1
    return count, digest.hexdigest(), size, md5.hexdigest()


def export_month(month):
    """Writes the month to <archive_dir>/<month>.jsonl.gz unless it already exists (never
    overwritten). Returns (path, created). The file is built under a temporary name, re-read
    and compared with the table before it is moved into place."""
    os.makedirs(archive_dir(), exist_ok=True)
    final = os.path.join(archive_dir(), archive_name(month))
    if os.path.exists(final):
        return final, False
    tmp = final + ".tmp"
    digest, count = hashlib.sha256(), 0
    gen = _iter_db_rows(month)
    try:
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
                for row in gen:
                    gz.write(_line(row))
                    _core_digest_update(digest, row)
                    count += 1
            raw.flush()
            os.fsync(raw.fileno())
    finally:
        gen.close()
    got_count, got_sha, _size, _md5 = scan_archive_file(tmp)
    if (got_count, got_sha) != (count, digest.hexdigest()):
        os.remove(tmp)
        raise ArchiveRowsMismatch("file does not match what was exported")
    if os.path.exists(final):          # never overwrite
        os.remove(tmp)
        return final, False
    os.replace(tmp, final)
    return final, True


# ---------------------------------------------------------------- Drive

def _escape(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _call(factory):
    import cloud_backup
    return cloud_backup.call_with_backoff(factory)


def get_archive_folder_id(service):
    """The archive folder's id: the remembered one if it is still a live folder with the exact
    name, else found by exact name in the root of the Drive (oldest wins, with a warning if
    there are several), else created. The id is remembered in a small file on the data disk."""
    remembered = None
    try:
        with open(folder_id_path(), "r", encoding="utf-8") as fh:
            remembered = (json.load(fh) or {}).get("folder_id")
    except (OSError, ValueError):
        remembered = None
    if remembered:
        try:
            meta = _call(lambda: service.files().get(fileId=remembered, fields="id,name,trashed,mimeType"))
            if meta and meta.get("name") == FOLDER_NAME and not meta.get("trashed") and meta.get("mimeType") == FOLDER_MIME:
                return remembered
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 404:
                raise
    q = (f"name = '{_escape(FOLDER_NAME)}' and '{DRIVE_ROOT_ID}' in parents "
         f"and mimeType = '{FOLDER_MIME}' and trashed = false")
    found = _call(lambda: service.files().list(q=q, fields="files(id,name,createdTime)", pageSize=100, spaces="drive")).get("files", [])
    found = [f for f in found if f.get("name") == FOLDER_NAME]
    if found:
        found.sort(key=lambda f: (f.get("createdTime") or "", f["id"]))
        if len(found) > 1:
            logger.warning("Activity archive: %d Drive folders are named %r; using the oldest", len(found), FOLDER_NAME)
        folder_id = found[0]["id"]
    else:
        created = _call(lambda: service.files().create(
            body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": [DRIVE_ROOT_ID]}, fields="id"))
        folder_id = created["id"]
        logger.info("Activity archive: created the Drive folder %r", FOLDER_NAME)
    tmp = folder_id_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"folder_id": folder_id}, fh)
    os.replace(tmp, folder_id_path())
    return folder_id


def _list_named(service, folder_id, name):
    q = f"name = '{_escape(name)}' and '{folder_id}' in parents and trashed = false"
    files = _call(lambda: service.files().list(q=q, fields="files(id,name,size,md5Checksum,createdTime)", pageSize=100, spaces="drive")).get("files", [])
    files = [f for f in files if f.get("name") == name]
    files.sort(key=lambda f: (f.get("createdTime") or "", f["id"]))
    return files


def ensure_on_drive(service, folder_id, local_path, month, local_size, local_md5):
    """Makes sure Drive holds exactly this file under its final name and that size+md5 were
    re-read from Drive and match. Returns 'uploaded' or 'already_there'. Never overwrites."""
    from googleapiclient.http import MediaIoBaseUpload

    final_name = archive_name(month)
    existing = _list_named(service, folder_id, final_name)
    if existing:
        f = existing[0]
        if str(f.get("size")) == str(local_size) and (f.get("md5Checksum") or "").lower() == local_md5:
            return "already_there"
        raise DriveFileConflict("an archive file with this name already exists on Drive and differs")

    # A leftover from an interrupted upload is moved to Drive's Trash (recoverable), never reused.
    for stale in _list_named(service, folder_id, final_name + UPLOADING_SUFFIX):
        _call(lambda: service.files().update(fileId=stale["id"], body={"trashed": True}, fields="id"))

    with open(local_path, "rb") as fh:
        payload = fh.read()
    created = _call(lambda: service.files().create(
        body={"name": final_name + UPLOADING_SUFFIX, "parents": [folder_id]},
        media_body=MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/gzip", resumable=True), fields="id"))
    meta = _call(lambda: service.files().get(fileId=created["id"], fields="id,size,md5Checksum"))
    if str(meta.get("size")) != str(local_size) or (meta.get("md5Checksum") or "").lower() != local_md5:
        raise DriveVerifyFailed("uploaded file's size/md5 do not match")
    _call(lambda: service.files().update(fileId=created["id"], body={"name": final_name}, fields="id"))
    after = _list_named(service, folder_id, final_name)
    if len(after) != 1 or str(after[0].get("size")) != str(local_size) or (after[0].get("md5Checksum") or "").lower() != local_md5:
        raise DriveVerifyFailed("final re-read from Drive does not match")
    return "uploaded"


# ---------------------------------------------------------------- months & pruning

def expired_months(now_utc=None):
    """IST months that still have raw rows and lie entirely before now - retention window,
    oldest first."""
    now = now_utc or tiers._utc_now()
    cutoff = (now - timedelta(days=tiers.retention_days())).isoformat(timespec="seconds")
    with db.get_connection() as conn:
        first = conn.execute("SELECT MIN(at_utc) FROM user_activity").fetchone()[0]
    if not first:
        return []
    month = tiers.month_of((datetime.fromisoformat(first) + tiers.IST_OFFSET).date())
    out = []
    while tiers.month_bounds_utc(month)[1] <= cutoff:
        with db.get_connection() as conn:
            start, end = tiers.month_bounds_utc(month)
            has = conn.execute("SELECT 1 FROM user_activity WHERE at_utc >= ? AND at_utc < ? LIMIT 1", (start, end)).fetchone()
        if has:
            out.append(month)
        y, m = (int(x) for x in month.split("-"))
        month = f"{y + (m == 12):04d}-{(m % 12) + 1:02d}"
    return out


def prune_month(month, expected_count):
    """Deletes the month's rows in ONE transaction, after re-counting them. Refuses without
    the month's summaries. Returns the number of rows deleted."""
    start, end = tiers.month_bounds_utc(month)
    with db.get_connection() as conn:
        gaps = tiers.month_summary_gaps(conn, month)
        if gaps:
            raise SummaryMissing(f"{len(gaps)} summary item(s) missing")
        conn.execute("BEGIN IMMEDIATE")
        now_count = conn.execute(
            "SELECT COUNT(*) FROM user_activity WHERE at_utc >= ? AND at_utc < ?", (start, end)).fetchone()[0]
        if now_count != expected_count:
            raise PruneCountMismatch("row count changed since the archive was verified")
        deleted = conn.execute("DELETE FROM user_activity WHERE at_utc >= ? AND at_utc < ?", (start, end)).rowcount
        if deleted != expected_count:
            raise PruneCountMismatch("deleted row count differs")
    return deleted


def _quarantine(path):
    """Sets an unusable local archive file aside (kept, never deleted or overwritten)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    os.replace(path, f"{path}.unusable-{stamp}")


def archive_month(service_getter, month, dry_run):
    """Full pipeline for one month. Returns rows pruned (0 on dry run)."""
    with db.get_connection() as conn:
        gaps = tiers.month_summary_gaps(conn, month)
    if gaps:
        raise SummaryMissing(f"{len(gaps)} summary item(s) missing")
    path, created = export_month(month)
    service = service_getter()
    folder_id = get_archive_folder_id(service)
    try:
        live = rows_covered_by_archive(month, path)
    except ArchiveRowsMismatch:
        # A local file from an earlier run that no longer covers the table. Safe to replace
        # only if Drive does not hold a file of that name; the old one is kept, set aside.
        if created or _list_named(service, folder_id, archive_name(month)):
            raise
        _quarantine(path)
        path, _ = export_month(month)
        live = rows_covered_by_archive(month, path)
    count, _sha, size, md5 = scan_archive_file(path)
    state = ensure_on_drive(service, folder_id, path, month, size, md5)
    logger.info("Activity archive: %s %s (%d rows in the file, verified on Drive)", month, state, count)
    if dry_run:
        return 0
    return prune_month(month, live)


def run_archive(service_factory=None, now_utc=None, dry_run=None):
    """One daily pass. Returns {'months': n, 'rows_pruned': n, 'error': class name or None}.
    Never raises."""
    dry = dry_run_enabled() if dry_run is None else dry_run
    result = {"months": 0, "rows_pruned": 0, "error": None, "dry_run": dry}
    service = None
    try:
        tiers.ensure_summaries(now_utc)
        months = expired_months(now_utc)
        if not months:
            return result

        def get_service():
            nonlocal service
            if service is None:
                if service_factory is not None:
                    service = service_factory()
                else:
                    import cloud_backup
                    service = cloud_backup.build_drive_service()
            return service

        for month in months:
            pruned = archive_month(get_service, month, dry)
            result["months"] += 1
            result["rows_pruned"] += pruned
    except Exception as exc:
        result["error"] = type(exc).__name__
        logger.error("Activity archive failed: %s (nothing was deleted for the failing month; will retry tomorrow)", type(exc).__name__)
    finally:
        try:
            if service is not None:
                close = getattr(service, "close", None)
                if close:
                    close()
            if service_factory is None:
                import cloud_backup
                cloud_backup._close_services()
        except Exception:
            pass
    if result["months"] and not result["error"]:
        logger.info("Activity archive run: months archived=%d, rows pruned=%d%s",
                    result["months"], result["rows_pruned"], " (dry run)" if dry else "")
    return result
