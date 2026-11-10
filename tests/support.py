"""Shared helpers for the check_all test suite.

Everything here runs INSIDE a scratch copy of the code (tests/check_all.py builds it), so the real
data/ folder, mappings.db, logs and .env are never read or written. Running a test file directly in
the real repo is refused on purpose."""
import hashlib
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.environ.get("CHECK_ALL_SCRATCH") != "1":
    sys.exit("Run the tests with:  python tests/check_all.py   (it works in a scratch copy, so data/ and .env are never touched).")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

GOLDEN_DIR = os.path.join(ROOT, "tests", "golden")
UPDATE = os.environ.get("CHECK_ALL_UPDATE") == "1"

# Fixed, fake configuration for every in-process test (set BEFORE config/app are imported).
TEST_ENV = {
    "DISABLE_TALLY_SCHEDULING": "1", "DB_PATH": "data/test.db", "INITIAL_IMAGE_SCAN": "0", "LOG_FILE": "logs/test.log",
    "INTAKE_SYNC_TOKEN": "tok123", "SYSTEM_ACCESS_TOKEN": "sys", "ACCOUNTS_ACCESS_PASSWORD": "pw",
    "FLASK_SECRET_KEY": "test-secret", "RESCAN_TRIGGER_TOKEN": "rescan", "TALLY_RETRY_ATTEMPTS": "1",
}
for key in list(os.environ):
    if key.startswith(("GDRIVE", "CLOUD_SYNC")) or key in ("MACHINE_ROLE", "BACKUP_MAX_DELETIONS", "IMAGE_SCAN_ROOT", "FEEDER_MODE"):
        del os.environ[key]
os.environ.update(TEST_ENV)

PAYLOAD = {
    "car_master": ["ALTO", "SWIFT"],
    "main_hierarchy": [{"item_name": "ALTO", "qty": 5}, {"item_name": "ALTO 7D MAT BLACK", "qty": 3}, {"item_name": "ALTO 9D MAT RED", "qty": 2},
                       {"item_name": "", "qty": ""}, {"item_name": "SWIFT", "qty": 4}, {"item_name": "SWIFT 7D MAT GREY", "qty": 4}],
    "item_stock": [{"item_name": "ALTO 7D MAT BLACK", "qty": 3}, {"item_name": "ALTO 9D MAT RED", "qty": 2}, {"item_name": "SWIFT 7D MAT GREY", "qty": 4}],
}
INTAKE_HEADERS = {"X-Intake-Token": "tok123"}

_state = {"app": None, "db": None, "users": None}


def bootstrap():
    """Import the app once, push the fixture data through the real intake route and seed a few rows."""
    if _state["app"] is not None:
        return _state["app"], _state["db"]
    import logging
    import app as app_module
    import database as db
    logging.disable(logging.CRITICAL)   # blocked-on-purpose routes log tracebacks; keep the output readable
    c = app_module.app.test_client()
    assert c.post("/admin/intake/sync_data", json=PAYLOAD, headers=INTAKE_HEADERS).status_code == 200
    from PIL import Image
    root = os.path.join(ROOT, "data", "S.S IMAGE", "ALTO")
    os.makedirs(root, exist_ok=True)
    for i, color in enumerate(((200, 30, 30), (30, 200, 30), (30, 30, 200))):
        Image.new("RGB", (40 + i, 30), color).save(os.path.join(root, f"img{i}.jpg"), "JPEG")
    ids = [db.add_image("ALTO", f"img{i}.jpg", os.path.join(root, f"img{i}.jpg")) for i in range(3)]
    db.add_mapping(ids[0], "ALTO 7D MAT BLACK", "ALTO", 1.0, "human")
    db.upsert_design_category("ALTO 7D MAT BLACK", db.get_all_categories()[0]["name"], 1)
    db.add_customer_report("ALTO", "alto", "fixture report", "star")
    db.publish_popup("fixture popup")
    db.publish_banner("fixture banner")
    _state.update(app=app_module, db=db, users={
        "admin": db.authenticate_user("admin", "idris123"), "dealer": db.authenticate_user("star", "111")})
    return app_module, db


def data_dir():
    return os.path.join(ROOT, "data")


def _db_file():
    return os.path.join(data_dir(), "test.db")


def _copy_db(src_path, dst_path):
    """Consistent database copy through SQLite's own backup API (works with WAL, no file-lock fights on Windows)."""
    import sqlite3
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def snapshot_state():
    dest = os.path.join(ROOT, ".state_snapshot")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    for folder, _dirs, names in os.walk(data_dir()):
        rel = os.path.relpath(folder, data_dir())
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for name in names:
            if name.startswith("test.db"):
                continue
            shutil.copy2(os.path.join(folder, name), os.path.join(dest, rel, name))
    _copy_db(_db_file(), os.path.join(dest, "test.db"))
    return dest


def restore_state(snapshot):
    import contextlib
    import io
    app_module, db = _state["app"], _state["db"]
    keep = set()
    for folder, _dirs, names in os.walk(snapshot):
        rel = os.path.relpath(folder, snapshot)
        os.makedirs(os.path.join(data_dir(), rel), exist_ok=True)
        for name in names:
            if name == "test.db":
                continue
            shutil.copy2(os.path.join(folder, name), os.path.join(data_dir(), rel, name))
            keep.add(os.path.normpath(os.path.join(rel, name)))
    for folder, _dirs, names in os.walk(data_dir()):          # remove files a request created
        rel = os.path.relpath(folder, data_dir())
        for name in names:
            if name.startswith("test.db"):
                continue
            if os.path.normpath(os.path.join(rel, name)) not in keep:
                os.remove(os.path.join(folder, name))
    _copy_db(os.path.join(snapshot, "test.db"), _db_file())
    db._dealer_dirty = db._accounts_dirty = True
    app_module._feeder_dirty = True
    app_module._feeder_file_state.clear()
    if hasattr(app_module, "_usage_cache_clear"):
        app_module._usage_cache_clear()
    with contextlib.redirect_stdout(io.StringIO()):
        app_module.load_data(refresh_first=False)


ROLES = ("anonymous", "dealer", "admin_locked", "admin_unlocked")


def client_for(role):
    app_module, db = bootstrap()
    client = app_module.app.test_client()
    if role == "anonymous":
        return client
    user = _state["users"]["dealer" if role == "dealer" else "admin"]
    with client.session_transaction() as sess:
        sess["user_id"] = user["id"]
        sess["username"] = user["username"]
        sess["role"] = user["role"]
        sess["remember"] = False
        if role == "admin_unlocked":
            sess["accounts_unlocked"] = True
            sess["accounts_unlocked_version"] = db.get_accounts_password_version()
    if role == "admin_unlocked":
        client.get("/admin/system/authorize-device", query_string={"token": "sys"})
    return client


class blocked_side_effects:
    """Context manager: nothing a route does can leave the process (no subprocess, network, thread, exit)."""
    def __enter__(self):
        import subprocess, threading, webbrowser, requests, cloud_backup
        self._saved = []
        def blocked(*a, **k):
            raise RuntimeError("blocked in tests")
        for obj, name in ((subprocess, "Popen"), (subprocess, "run"), (subprocess, "check_output"), (subprocess, "call"),
                          (os, "_exit"), (os, "execv"), (requests.sessions.Session, "request"), (webbrowser, "open"),
                          (cloud_backup, "run_sync"), (cloud_backup, "build_drive_service"), (cloud_backup, "authorize")):
            if hasattr(obj, name):
                self._saved.append((obj, name, getattr(obj, name)))
                setattr(obj, name, blocked)
        self._saved.append((threading.Thread, "start", threading.Thread.start))
        threading.Thread.start = lambda self_, *a, **k: None
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        return False


# ---------------------------------------------------------------------------
# Normalising responses so a snapshot only changes when behaviour changes
# ---------------------------------------------------------------------------
_MASKS = [
    (re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?"), "<ts>"),
    (re.compile(r"\d{2}/\d{2}/\d{4},? \d{2}:\d{2}(?::\d{2})?(?: ?[AP]M)?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{12}\b"), "<ver>"),
    (re.compile(r"[0-9a-f]{20,}"), "<hex>"),
    (re.compile(r"\b\d{10,}\b"), "<epoch>"),
    (re.compile(r'"date": "\d{4}-\d\d-\d\d"'), '"date": "<day>"'),   # the activity chart lists the last N calendar days
]
SHAPE_ONLY_PREFIXES = ("/admin/system/",)   # live numbers / log lines: snapshot only the structure


def mask_text(text):
    root = ROOT.replace("\\", "/")
    for variant in {root, root.replace("/", "\\"), root.replace("/", "\\\\")}:
        text = text.replace(variant, "<ROOT>")
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


def _shape(value):
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(v) for v in value[:3]] + (["..."] if len(value) > 3 else [])
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return "<num>"
    return "<str>"


def normalize_response(response, path):
    body = response.get_data()
    mimetype = response.mimetype
    if mimetype == "application/json":
        try:
            data = json.loads(body)
            text = json.dumps(_shape(data) if path.startswith(SHAPE_ONLY_PREFIXES) else data, sort_keys=True, ensure_ascii=False)
        except ValueError:
            text = body.decode("utf-8", "replace")
        text = mask_text(text)
    elif mimetype.startswith(("text/", "application/javascript")) or mimetype in ("application/xml",):
        if path.startswith(SHAPE_ONLY_PREFIXES) and mimetype == "text/plain":
            text = "<text>"      # e.g. the log viewer: its content depends on whatever ran before
        else:
            text = mask_text(body.decode("utf-8", "replace"))
            text = re.sub(r"\s+", " ", text)
    elif mimetype.startswith("image/"):
        text = f"<image {len(body)} bytes>"
    else:
        text = "<binary>"   # e.g. the backup zip: its size varies run to run
    location = response.headers.get("Location", "")
    location = re.sub(r"^https?://[^/]+", "", location)
    return {
        "status": response.status_code,
        "type": mimetype,
        "cache": response.headers.get("Cache-Control", ""),
        "location": mask_text(location),
        "body": hashlib.sha1(text.encode("utf-8")).hexdigest()[:10],
        "len": len(text),
    }


# ---------------------------------------------------------------------------
# Golden files
# ---------------------------------------------------------------------------
def golden_path(name):
    return os.path.join(GOLDEN_DIR, name)


def compare_golden(testcase, name, actual):
    """Compare (or, with CHECK_ALL_UPDATE=1, rewrite) tests/golden/<name>."""
    path = golden_path(name)
    text = json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if UPDATE:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return
    if not os.path.exists(path):
        testcase.fail(f"golden file {name} is missing; create it with: python tests/check_all.py --update")
    expected = json.load(open(path, encoding="utf-8"))
    if expected != actual:
        diffs = []
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                if expected.get(key) != actual.get(key):
                    diffs.append(f"  {key}\n      expected: {expected.get(key)}\n      actual:   {actual.get(key)}")
        testcase.fail(f"{name} changed ({len(diffs)} difference(s)). If the change is intended run: python tests/check_all.py --update\n"
                      + "\n".join(diffs[:25]) + ("\n  ..." if len(diffs) > 25 else ""))
