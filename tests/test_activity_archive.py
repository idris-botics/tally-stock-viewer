"""Archive of expired user_activity months to the separate Drive folder, against an in-memory FAKE Drive
and a scratch database (synthetic rows only; never the real data/ or Drive). Covers: only whole expired
months leave the table, archived + remaining = original, every failure mode deletes nothing and the next
run succeeds, idempotence, never overwriting, dry run, folder handling, and that the nightly backup's
verification and deletion guard behave identically with the archive folder present."""
import gc
import gzip
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from tests import support
from tests.activity_fixtures import ScratchDb, make_users, insert_rows, synthetic_rows, count_rows
from tests.fakedrive import FakeDrive, FOLDER_MIME, make_http_error

import activity_archive as arch
import activity_tiers as tiers
import cloud_backup as cb
import database as db
from config import Config

NOW = datetime(2027, 2, 10, 12, 0, 0)


class MutatingService:
    """Wraps the fake Drive service; `create` can fail or corrupt what was uploaded."""

    def __init__(self, drive, mutate=None, fail=None):
        self.drive, self.inner, self.mutate, self.fail = drive, drive.service(), mutate, fail

    def files(self):
        real = self.inner.files()
        outer = self

        class Files:
            list = staticmethod(real.list); get = staticmethod(real.get); update = staticmethod(real.update); delete = staticmethod(real.delete)

            @staticmethod
            def create(body=None, media_body=None, fields=None):
                if outer.fail and media_body is not None:
                    raise outer.fail
                if outer.mutate and media_body is not None:
                    data = outer.mutate(media_body.getbytes(0, media_body.size()))

                    class Media:
                        def getbytes(self, a, b): return data
                        def size(self): return len(data)
                    media_body = Media()
                return real.create(body=body, media_body=media_body, fields=fields)
        return Files()


class ArchiveCase(unittest.TestCase):
    RETENTION = "30"

    def setUp(self):
        logging.disable(logging.NOTSET)
        self.scratch = ScratchDb().start()
        os.environ["ACTIVITY_RETENTION_DAYS"] = self.RETENTION
        os.environ.pop("ACTIVITY_ARCHIVE_DRY_RUN", None)
        self.saved_pace = cb.MIN_CALL_INTERVAL_SECONDS
        cb.MIN_CALL_INTERVAL_SECONDS = 0
        self.admin, self.dealers = make_users(6)
        self.drive = FakeDrive()
        self.service = self.drive.service()
        rows = synthetic_rows(NOW - timedelta(days=120), 120, self.dealers, per_day=25, seed=4, admin_id=self.admin)
        insert_rows([r for r in rows if r[1] < NOW.isoformat(timespec="seconds")])
        self.original = count_rows()

    def tearDown(self):
        cb.MIN_CALL_INTERVAL_SECONDS = self.saved_pace
        os.environ.pop("ACTIVITY_ARCHIVE_DRY_RUN", None)
        self.assertEqual(self.drive.calls["PERMANENT_DELETE"], 0, "a permanent Drive delete happened")
        self.scratch.stop()
        logging.disable(logging.CRITICAL)

    # helpers
    def run_arch(self, service=None, dry_run=False):
        svc = service or self.service
        return arch.run_archive(service_factory=lambda: svc, now_utc=NOW, dry_run=dry_run)

    def folder_id(self):
        found = [n for n in self.drive.nodes.values() if n["name"] == arch.FOLDER_NAME and not n["trashed"]]
        return found[0]["id"] if found else None

    def archive_files(self):
        fid = self.folder_id()
        return sorted((n for n in self.drive.live_files() if fid in n["parents"]), key=lambda n: n["name"])

    def month_rows(self, month):
        s, e = tiers.month_bounds_utc(month)
        return count_rows("at_utc >= ? AND at_utc < ?", (s, e))

    def snapshot_rows(self, where="1=1", args=()):
        with db.get_connection() as conn:
            return [tuple(r) for r in conn.execute(f"SELECT id, user_id, at_utc, action, detail FROM user_activity WHERE {where} ORDER BY id", args)]


class ArchiveBasics(ArchiveCase):
    def test_only_fully_expired_months_are_archived_and_the_numbers_add_up(self):
        cutoff = (NOW - timedelta(days=30)).isoformat(timespec="seconds")
        self.assertEqual(arch.expired_months(NOW), ["2026-10", "2026-11", "2026-12"])
        per_month = {m: self.month_rows(m) for m in ("2026-10", "2026-11", "2026-12", "2027-01", "2027-02")}
        self.assertTrue(all(per_month.values()), "the fixture must span 5 months")
        young_before = self.snapshot_rows("at_utc >= ?", (tiers.month_bounds_utc("2027-01")[0],))
        res = self.run_arch()
        self.assertEqual((res["months"], res["error"]), (3, None))
        archived = per_month["2026-10"] + per_month["2026-11"] + per_month["2026-12"]
        self.assertEqual(res["rows_pruned"], archived)
        self.assertEqual(count_rows(), self.original - archived)                                   # archived + remaining = original
        self.assertEqual(count_rows("at_utc < ?", (tiers.month_bounds_utc("2027-01")[0],)), 0)
        self.assertEqual(self.snapshot_rows("at_utc >= ?", (tiers.month_bounds_utc("2027-01")[0],)), young_before,
                         "rows younger than the expired months (January, even though part of it is older than the cutoff) are untouched")
        files = self.archive_files()
        self.assertEqual([f["name"] for f in files], ["2026-10.jsonl.gz", "2026-11.jsonl.gz", "2026-12.jsonl.gz"])
        total = 0
        for f in files:
            lines = gzip.decompress(f["content"]).decode("utf-8").splitlines()
            total += len(lines)
            first = json.loads(lines[0])
            self.assertEqual(sorted(first), ["action", "at_utc", "detail", "id", "user_id", "username"])
        self.assertEqual(total, archived)
        self.assertTrue(os.path.isdir(arch.archive_dir()))
        self.assertEqual(sorted(os.listdir(arch.archive_dir())), [f["name"] for f in files])
        self.assertEqual(self.drive.nodes[self.folder_id()]["parents"], [self.drive.root], "separate folder in the Drive root")

    def test_reader_tool_returns_archive_plus_live_rows_exactly_once(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("activity_archive_read", os.path.join(support.ROOT, "tools", "activity_archive_read.py"))
        reader = importlib.util.module_from_spec(spec); spec.loader.exec_module(reader)
        everything = self.snapshot_rows()
        self.run_arch(dry_run=True)                       # rows now exist in BOTH places
        rows = list(reader.iter_all_rows(arch.archive_dir(), self.scratch.path))
        self.assertEqual(len(rows), self.original)
        self.run_arch()                                   # now only the young rows are live
        rows = list(reader.iter_all_rows(arch.archive_dir(), self.scratch.path))
        self.assertEqual([(r["id"], r["user_id"], r["at_utc"], r["action"], r["detail"]) for r in sorted(rows, key=lambda r: r["id"])], everything)
        self.assertEqual({r["source"] for r in rows}, {"archive", "live"})
        self.assertTrue(all(r["username"] for r in rows))

    def test_nothing_expired_means_no_drive_access_at_all(self):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM user_activity WHERE at_utc < ?", ("2027-01-15T00:00:00",))
        calls = []
        res = arch.run_archive(service_factory=lambda: calls.append(1), now_utc=NOW)
        self.assertEqual((res["months"], res["rows_pruned"], calls), (0, 0, []))

    def test_default_window_is_90_days(self):
        os.environ.pop("ACTIVITY_RETENTION_DAYS", None)
        self.assertEqual(tiers.retention_days(), 90)
        self.assertEqual(arch.expired_months(NOW), ["2026-10"])      # only October is wholly older than 90 days

    def test_running_twice_archives_once(self):
        first = self.run_arch()
        uploads = self.drive.calls["create_media"]
        second = self.run_arch()
        self.assertEqual((first["months"], second["months"], second["rows_pruned"]), (3, 0, 0))
        self.assertEqual(self.drive.calls["create_media"], uploads)

    def test_existing_archive_files_are_never_overwritten(self):
        self.run_arch(dry_run=True)                                       # uploads + verifies, prunes nothing
        self.assertEqual(count_rows(), self.original)
        paths = [os.path.join(arch.archive_dir(), n) for n in sorted(os.listdir(arch.archive_dir()))]
        stamps = {p: (open(p, "rb").read(), os.stat(p).st_mtime_ns) for p in paths}
        drive_before = {n["id"]: bytes(n["content"]) for n in self.archive_files()}
        uploads = self.drive.calls["create_media"]
        res = self.run_arch()                                             # now for real: nothing re-uploaded
        self.assertEqual((res["months"], res["error"]), (3, None))
        self.assertEqual(self.drive.calls["create_media"], uploads)
        self.assertEqual({p: (open(p, "rb").read(), os.stat(p).st_mtime_ns) for p in paths}, stamps)
        self.assertEqual({n["id"]: bytes(n["content"]) for n in self.archive_files()}, drive_before)

    def test_a_different_file_with_the_archive_name_on_drive_is_kept_and_nothing_is_deleted(self):
        first = self.run_arch(dry_run=True)
        node = next(n for n in self.archive_files() if n["name"] == "2026-11.jsonl.gz")
        node["content"] = b"someone else's file"
        with self.assertLogs("activity_archive", "ERROR") as logs:
            res = self.run_arch()
        self.assertEqual(res["error"], "DriveFileConflict")
        self.assertEqual(node["content"], b"someone else's file", "never overwritten")
        self.assertGreater(self.month_rows("2026-11"), 0)
        self.assertEqual(len([r for r in logs.records if r.levelno >= logging.ERROR]), 1)

    def test_dry_run_env_uploads_and_verifies_but_never_prunes(self):
        os.environ["ACTIVITY_ARCHIVE_DRY_RUN"] = "1"
        res = arch.run_archive(service_factory=lambda: self.service, now_utc=NOW)
        self.assertEqual((res["months"], res["rows_pruned"], res["dry_run"]), (3, 0, True))
        self.assertEqual(count_rows(), self.original)
        self.assertEqual(len(self.archive_files()), 3)

    def test_summaries_are_written_before_any_row_leaves(self):
        self.run_arch()
        with db.get_connection() as conn:
            marked = {r[0] for r in conn.execute("SELECT key FROM activity_summary_done")}
            daily = conn.execute("SELECT COUNT(*) FROM activity_daily WHERE day_ist LIKE '2026-10%'").fetchone()[0]
        self.assertIn("m:2026-10", marked)
        self.assertIn("d:2026-10-20", marked)
        self.assertGreater(daily, 0)


class ArchiveFailureModes(ArchiveCase):
    def assert_nothing_lost_then_recovers(self, first_run, expect_error=True):
        with self.assertLogs("activity_archive", "ERROR") as logs:
            res = first_run()
        if expect_error:
            self.assertIsNotNone(res["error"])
            errors = [r for r in logs.records if r.levelno >= logging.ERROR]
            self.assertEqual(len(errors), 1, "exactly one clear ERROR")
            self.assertIn(res["error"], errors[0].getMessage())
        self.assertEqual(count_rows(), self.original, "a failed run deletes nothing")
        ok = self.run_arch()                                              # the next day's run
        self.assertEqual((ok["error"], ok["months"]), (None, 3))
        self.assertEqual(count_rows() + ok["rows_pruned"], self.original)
        self.assertEqual(count_rows("at_utc < ?", (tiers.month_bounds_utc("2027-01")[0],)), 0)
        self.assertEqual([f["name"] for f in self.archive_files()], ["2026-10.jsonl.gz", "2026-11.jsonl.gz", "2026-12.jsonl.gz"])
        return ok

    def test_upload_failure(self):
        svc = MutatingService(self.drive, fail=make_http_error(403))
        self.assert_nothing_lost_then_recovers(lambda: self.run_arch(svc))
        self.assertEqual([f for f in self.archive_files() if f["name"].endswith(".uploading")], [])

    def test_wrong_md5_on_drive(self):
        flip = lambda b: b[:-1] + bytes([b[-1] ^ 0xFF])
        res = self.assert_nothing_lost_then_recovers(lambda: self.run_arch(MutatingService(self.drive, mutate=flip)))
        self.assertGreaterEqual(self.drive.calls["trash"], 1, "the unverified leftover goes to Drive's Trash, not a permanent delete")

    def test_partial_upload(self):
        cut = lambda b: b[: len(b) // 2]
        self.assert_nothing_lost_then_recovers(lambda: self.run_arch(MutatingService(self.drive, mutate=cut)))
        self.assertGreaterEqual(self.drive.calls["trash"], 1)

    def test_wrong_row_count_in_the_exported_file(self):
        real = arch.scan_archive_file
        state = {"n": 0}

        def lying(path):
            c, sha, size, md5 = real(path)
            state["n"] += 1
            return (c + 1, sha, size, md5) if state["n"] == 1 else (c, sha, size, md5)

        arch.scan_archive_file = lying
        try:
            self.assert_nothing_lost_then_recovers(lambda: self.run_arch())
        finally:
            arch.scan_archive_file = real
        self.assertFalse([n for n in os.listdir(arch.archive_dir()) if n.endswith(".tmp")])

    def test_interruption_after_upload_before_delete(self):
        class Crash(BaseException):
            pass

        real = arch.prune_month

        def boom(month, expected):
            raise Crash()

        arch.prune_month = boom
        try:
            with self.assertRaises(Crash):
                self.run_arch()
        finally:
            arch.prune_month = real
        self.assertEqual(count_rows(), self.original)
        self.assertEqual(self.drive.calls["create_media"], 1, "only the first month got as far as Drive")
        ok = self.run_arch()
        self.assertEqual((ok["error"], ok["months"]), (None, 3))
        self.assertEqual(self.drive.calls["create_media"], 3, "the already-verified month is not uploaded again; the other two are")
        self.assertEqual(count_rows() + ok["rows_pruned"], self.original)

    def test_interruption_in_the_middle_of_the_delete_keeps_the_rows(self):
        real = arch.prune_month

        def half(month, expected):
            s, e = tiers.month_bounds_utc(month)
            with db.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM user_activity WHERE at_utc >= ? AND at_utc < ?", (s, e))
                raise RuntimeError("killed before commit")               # rolls back

        arch.prune_month = half
        try:
            with self.assertLogs("activity_archive", "ERROR"):
                res = self.run_arch()
        finally:
            arch.prune_month = real
        self.assertEqual(res["error"], "RuntimeError")
        self.assertEqual(count_rows(), self.original)
        ok = self.run_arch()
        self.assertEqual(count_rows() + ok["rows_pruned"], self.original)

    def test_rows_added_after_verification_abort_the_delete(self):
        real = arch.prune_month

        def sneaky(month, expected):
            insert_rows([(self.dealers[0], f"{month}-15T10:00:00", "view_car", "LATE ROW")])
            return real(month, expected)

        arch.prune_month = sneaky
        try:
            with self.assertLogs("activity_archive", "ERROR"):
                res = self.run_arch()
        finally:
            arch.prune_month = real
        self.assertEqual(res["error"], "PruneCountMismatch")
        self.assertEqual(count_rows(), self.original + 1)

    def test_local_archive_that_no_longer_covers_the_table_is_set_aside_and_redone(self):
        self.run_arch(dry_run=True)
        # An old local file missing rows, and nothing on Drive under that name.
        local = os.path.join(arch.archive_dir(), "2026-12.jsonl.gz")
        keep = gzip.open(local, "rb").read().splitlines(True)
        with open(local, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(b"".join(keep[:-5]))
        drive_node = next(n for n in self.archive_files() if n["name"] == "2026-12.jsonl.gz")
        drive_node["trashed"] = True
        res = self.run_arch()
        self.assertEqual(res["error"], None)
        self.assertTrue([n for n in os.listdir(arch.archive_dir()) if ".unusable-" in n], "the bad file is kept, not deleted")
        self.assertEqual(count_rows() + res["rows_pruned"], self.original)

    def test_live_rows_missing_from_a_verified_archive_block_the_delete(self):
        self.run_arch(dry_run=True)
        insert_rows([(self.dealers[1], "2026-10-20T10:00:00", "view_car", "NOT ARCHIVED")])   # appears after the file was made
        with self.assertLogs("activity_archive", "ERROR"):
            res = self.run_arch()
        self.assertEqual(res["error"], "ArchiveRowsMismatch")
        self.assertEqual(count_rows(), self.original + 1)

    def test_dealer_deleted_after_export_does_not_block_the_prune(self):
        self.run_arch(dry_run=True)
        db.delete_customer_user(self.dealers[0])                       # cascade removes their rows: table is now a SUBSET of the file
        res = self.run_arch()
        self.assertEqual((res["error"], res["months"]), (None, 3))
        self.assertEqual(count_rows("at_utc < ?", (tiers.month_bounds_utc("2027-01")[0],)), 0)


class ArchiveFolder(ArchiveCase):
    def test_finds_an_existing_folder_by_exact_name_in_the_root_and_remembers_it(self):
        mine = self.service.files().create(body={"name": arch.FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": ["root"]}).execute()["id"]
        self.service.files().create(body={"name": arch.FOLDER_NAME + " old", "mimeType": FOLDER_MIME, "parents": ["root"]}).execute()
        self.assertEqual(arch.get_archive_folder_id(self.service), mine)
        self.assertEqual(json.load(open(arch.folder_id_path()))["folder_id"], mine)
        lists = self.drive.calls["list"]
        self.assertEqual(arch.get_archive_folder_id(self.service), mine)
        self.assertEqual(self.drive.calls["list"], lists, "the remembered id is used without searching again")
        self.assertEqual(len([n for n in self.drive.nodes.values() if n["name"] == arch.FOLDER_NAME]), 1, "no duplicate created")

    def test_several_same_named_folders_use_the_oldest_and_warn(self):
        a = self.service.files().create(body={"name": arch.FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": ["root"]}).execute()["id"]
        self.service.files().create(body={"name": arch.FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": ["root"]}).execute()
        with self.assertLogs("activity_archive", "WARNING") as logs:
            self.assertEqual(arch.get_archive_folder_id(self.service), a)
        self.assertTrue(any("oldest" in r.getMessage() for r in logs.records))

    def test_creates_it_when_missing_and_recreates_if_the_remembered_one_was_trashed(self):
        first = arch.get_archive_folder_id(self.service)
        self.assertEqual(self.drive.nodes[first]["parents"], [self.drive.root])
        self.drive.nodes[first]["trashed"] = True
        second = arch.get_archive_folder_id(self.service)
        self.assertNotEqual(first, second)

    def test_folder_in_a_subfolder_is_not_mistaken_for_the_root_one(self):
        other = self.service.files().create(body={"name": "Backup", "mimeType": FOLDER_MIME, "parents": ["root"]}).execute()["id"]
        self.service.files().create(body={"name": arch.FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": [other]}).execute()
        fid = arch.get_archive_folder_id(self.service)
        self.assertEqual(self.drive.nodes[fid]["parents"], [self.drive.root])


class ArchiveResources(ArchiveCase):
    def test_fds_and_rss_stay_flat_across_10_archive_runs(self):
        import psutil
        proc = psutil.Process()
        fds = lambda: proc.num_fds() if hasattr(proc, "num_fds") else proc.num_handles()
        readings = []
        rnd = random.Random(1)
        closed = []

        class Tracked:
            def __init__(self, inner): self.inner = inner
            def files(self): return self.inner.files()
            def close(self): closed.append(1)

        for i in range(10):
            month = f"2025-{i + 1:02d}"
            rows = [(rnd.choice(self.dealers), f"{month}-{rnd.randint(2, 27):02d}T{rnd.randint(0, 23):02d}:00:00", "view_car", f"C{rnd.randint(0, 9)}")
                    for _ in range(400)]
            insert_rows(rows)
            res = self.run_arch(Tracked(self.service))
            self.assertEqual(res["error"], None)
            gc.collect()
            readings.append((fds(), proc.memory_info().rss / 1048576))
        self.assertEqual(len(closed), 10, "the Drive client is closed after every run")
        self.assertLessEqual(readings[-1][0] - readings[1][0], 2, f"open fds/handles grew: {readings}")
        self.assertLess(readings[-1][1] - readings[1][1], 20, f"RSS grew: {readings}")


class BackupUnaffected(unittest.TestCase):
    """The nightly backup mirrors ONE Drive folder; the archive folder is a sibling in the Drive root, so
    verification and the deletion guard must behave exactly as without it."""

    N_IMAGES = 60

    def build(self, with_archive):
        tmp = tempfile.mkdtemp(prefix="bkarch_")
        self.addCleanup(shutil.rmtree, tmp, True)
        data = os.path.join(tmp, "data"); os.makedirs(os.path.join(data, "S.S IMAGE"))
        saved = (cb.DATA_DIR, cb.IMAGE_ROOT, cb.MANIFEST_PATH, cb.STATUS_PATH, cb.SNAPSHOT_DIR, cb.is_configured, cb.build_drive_service,
                 cb._get_thread_local_drive_service, cb.MIN_CALL_INTERVAL_SECONDS, Config.DB_PATH, Config.GDRIVE_BACKUP_FOLDER_ID, Config.BACKUP_MAX_DELETIONS)

        def restore():
            (cb.DATA_DIR, cb.IMAGE_ROOT, cb.MANIFEST_PATH, cb.STATUS_PATH, cb.SNAPSHOT_DIR, cb.is_configured, cb.build_drive_service,
             cb._get_thread_local_drive_service, cb.MIN_CALL_INTERVAL_SECONDS, Config.DB_PATH, Config.GDRIVE_BACKUP_FOLDER_ID, Config.BACKUP_MAX_DELETIONS) = saved
        self.addCleanup(restore)
        cb.DATA_DIR = data; cb.IMAGE_ROOT = os.path.join(data, "S.S IMAGE"); cb.MANIFEST_PATH = os.path.join(data, ".m.json")
        cb.STATUS_PATH = os.path.join(data, ".s.json"); cb.SNAPSHOT_DIR = os.path.join(data, ".snap")
        Config.DB_PATH = os.path.join(data, "mappings.db"); Config.BACKUP_MAX_DELETIONS = None
        cb.is_configured = lambda: True; cb.MIN_CALL_INTERVAL_SECONDS = 0
        drive = FakeDrive(); svc = drive.service()
        backup_root = svc.files().create(body={"name": "SuperSeatings Backup", "mimeType": FOLDER_MIME, "parents": ["root"]}).execute()["id"]
        Config.GDRIVE_BACKUP_FOLDER_ID = backup_root
        cb.build_drive_service = lambda: svc; cb._get_thread_local_drive_service = lambda: svc
        rnd = random.Random(1)
        for i in range(self.N_IMAGES):
            d = os.path.join(cb.IMAGE_ROOT, f"CAR {i % 4}'D"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"img_{i:03d}.jpg"), "wb").write(rnd.randbytes(1500 + i))
        for name in cb.INCLUDED_JSON_FILES:
            open(os.path.join(data, name), "w").write('{"x": 1}')
        c = sqlite3.connect(Config.DB_PATH); c.execute("create table t(id integer primary key, v text)")
        c.executemany("insert into t(v) values (?)", [(f"row{i}",) for i in range(50)]); c.commit(); c.close()
        if with_archive:           # a real archive folder with real archive files, made by the real code
            scratch = ScratchDb()
            db_saved = (db.DB_PATH, Config.DB_PATH)
            db.DB_PATH = os.path.join(tmp, "act", "mappings.db"); os.makedirs(os.path.dirname(db.DB_PATH))
            Config.DB_PATH = db.DB_PATH
            os.environ["ACTIVITY_RETENTION_DAYS"] = "30"
            try:
                db.init_database()
                _admin, dealers = make_users(3)
                insert_rows([r for r in synthetic_rows(NOW - timedelta(days=100), 100, dealers, per_day=10) if r[1] < NOW.isoformat(timespec="seconds")])
                res = arch.run_archive(service_factory=lambda: svc, now_utc=NOW, dry_run=False)
                assert res["months"] >= 2 and not res["error"], res
            finally:
                os.environ.pop("ACTIVITY_RETENTION_DAYS", None)
                db.DB_PATH, Config.DB_PATH = db_saved
                Config.DB_PATH = os.path.join(data, "mappings.db")
        return drive

    def sequence(self, with_archive):
        logging.disable(logging.CRITICAL)
        drive = self.build(with_archive)
        out = []
        keep = lambda r: (r["status"], r["added"], r["updated"], r["deleted"], r["deletions_held_back"], (r.get("verification") or {}).get("ok"))
        out.append(keep(cb.run_sync("test")["last_run"]))
        imgs = sorted(os.path.join(r, f) for r, _d, fs in os.walk(cb.IMAGE_ROOT) for f in fs)
        for p in imgs[:3]:
            os.remove(p)
        out.append(keep(cb.run_sync("test")["last_run"]))                       # 3 deletions: allowed, go to Trash
        for p in imgs[3:3 + 21]:
            os.remove(p)
        out.append(keep(cb.run_sync("test")["last_run"]))                       # 21 deletions: held back
        shutil.rmtree(cb.IMAGE_ROOT); os.makedirs(cb.IMAGE_ROOT)
        out.append(keep(cb.run_sync("test")["last_run"]))                       # empty folder: held back
        self.assertEqual(drive.calls["PERMANENT_DELETE"], 0)
        backup_trash = len(drive.trashed_files())
        return out, backup_trash, drive

    def test_verification_and_deletion_guard_identical_with_the_archive_folder_present(self):
        plain, plain_trash, _ = self.sequence(False)
        with_arch, arch_trash, drive = self.sequence(True)
        self.assertEqual(with_arch, plain)
        self.assertEqual(arch_trash, plain_trash)
        self.assertEqual(plain[0][0], "success")
        self.assertTrue(plain[0][5])
        self.assertEqual(plain[2][0], "partial")                   # the guard really fired in both runs
        self.assertGreaterEqual(len([n for n in drive.live_files() if n["name"].endswith(".jsonl.gz")]), 2, "archive files still on Drive")

    def test_a_backup_that_succeeds_triggers_the_archive_and_a_failed_one_does_not(self):
        calls = []
        real = arch.run_archive
        arch.run_archive = lambda *a, **k: calls.append(1)
        try:
            cb._archive_activity_after_backup({"last_run": {"status": "partial"}})
            cb._archive_activity_after_backup({"last_run": {"status": "failed"}})
            cb._archive_activity_after_backup({"configured": False})
            self.assertEqual(calls, [])
            cb._archive_activity_after_backup({"last_run": {"status": "success"}})
            self.assertEqual(calls, [1])
        finally:
            arch.run_archive = real

    def test_scheduled_run_holds_the_backup_lock_while_archiving_and_skips_when_a_sync_is_running(self):
        seen = []
        saved = (cb.is_authorized, cb.run_sync, cb._archive_activity_after_backup)
        cb.is_authorized = lambda: True
        cb.run_sync = lambda triggered_by="x": {"last_run": {"status": "success"}}
        cb._archive_activity_after_backup = lambda outcome: seen.append(cb.SYNC_LOCK.locked())
        try:
            cb._attempt_scheduled_run()
            self.assertEqual(seen, [True], "archive runs inside SYNC_LOCK, so never concurrently with a backup")
            self.assertTrue(cb.SYNC_LOCK.acquire(blocking=False))
            try:
                cb._attempt_scheduled_run()        # a sync is already running: both the backup and the archive are skipped
            finally:
                cb.SYNC_LOCK.release()
            self.assertEqual(seen, [True])
        finally:
            cb.is_authorized, cb.run_sync, cb._archive_activity_after_backup = saved


if __name__ == "__main__":
    unittest.main()
