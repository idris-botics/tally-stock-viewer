import functools
import glob
import hashlib
import os
import shutil
import sqlite3
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import machine_role
from config import Config
from utils.normalize import normalize_lookup_key


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = Config.DB_PATH if os.path.isabs(Config.DB_PATH) else os.path.join(BASE_DIR, Config.DB_PATH)
IMAGE_ROOT = os.path.join(BASE_DIR, "data", "S.S IMAGE")
logger = logging.getLogger(__name__)

# The material-tier "Assign Category" tags (see design_categories table below)
# used to be this fixed tuple, validated against on both a DB CHECK
# constraint and every Python entry point. Categories are now admin-editable
# (see the `categories` table / _seed_categories_table() below) -- this tuple
# survives only as the one-time seed data so an existing install's 8
# categories come through the upgrade with identical names, order, and
# abbreviations (see DESIGN_CATEGORY_ABBREVIATIONS_SEED below).
DESIGN_CATEGORY_CHOICES = (
    "Pearl",
    "Pearl Designer",
    "Pearl Deluxe",
    "Saka",
    "Ruby",
    "Napa Deluxe",
    "Napa Designer",
)

# Hand-curated short forms, previously duplicated as CATEGORY_RIBBON_ABBREVIATIONS
# in templates/index.html -- now the seed for categories.abbreviation, the
# single live source both the on-thumbnail ribbon and the burned share-image
# badge read from going forward.
DESIGN_CATEGORY_ABBREVIATIONS_SEED = {
    "Pearl": "PEARL",
    "Pearl Designer": "P DSGN",
    "Pearl Deluxe": "P DLX",
    "Saka": "SAKA",
    "Ruby": "RUBY",
    "Napa Deluxe": "N DLX",
    "Napa Designer": "N DSGN",
}

CATEGORY_ABBREVIATION_MAX_LENGTH = 16
CATEGORY_NAME_MAX_LENGTH = 100


def generate_category_abbreviation(name):
    """Auto-generated fallback abbreviation for a category created without
    an explicit override (Part 2): up to the first 4 letters of each of the
    first 2 significant (non-empty) words, uppercased and space-joined, capped
    at a reasonable total length so it can never blow up the on-thumbnail
    ribbon or the burned share-image badge the way an admin typing a long
    category name otherwise could. Deliberately simple/unpolished -- the
    System panel's abbreviation override exists specifically to fix any
    result that comes out looking awkward."""
    words = [word for word in str(name or "").split() if word]
    parts = []
    for word in words[:2]:
        letters = "".join(ch for ch in word if ch.isalnum())
        if letters:
            parts.append(letters[:4].upper())
    abbreviation = " ".join(parts)
    return (abbreviation or "CAT")[:12]


def _canonicalize_filepath(filepath):
    value = str(filepath or "").strip()
    if not value:
        return value
    if os.path.isabs(value):
        normalized = os.path.normpath(os.path.abspath(value))
        if os.name == "nt":
            normalized = os.path.normcase(normalized)
        return normalized
    return value.replace("\\", "/")


def _validate_stock_item_name(stock_item_name):
    value = str(stock_item_name or "")
    if "\x00" in value:
        raise ValueError("stock item name contains null byte")
    if len(value) > 500:
        raise ValueError("stock item name exceeds 500 characters")
    return value


def _validate_confidence(confidence):
    value = float(confidence)
    if value < 0.0 or value > 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    return value


def _resolve_filepath_candidate(value):
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(IMAGE_ROOT, value))


def _validate_filepath(filepath):
    value = _canonicalize_filepath(filepath)
    if not value:
        raise ValueError("filepath is required")
    if "\x00" in value:
        raise ValueError("filepath contains null byte")

    candidate = _resolve_filepath_candidate(value)
    if not os.path.exists(candidate):
        raise ValueError(f"filepath does not exist: {candidate}")
    if not os.access(candidate, os.R_OK):
        raise ValueError(f"filepath is not readable: {candidate}")
    if os.path.getsize(candidate) > Config.MAX_IMAGE_SIZE:
        raise ValueError(f"filepath exceeds max image size: {candidate}")

    if os.path.isabs(value):
        image_root_norm = os.path.normcase(os.path.normpath(IMAGE_ROOT)) if os.name == "nt" else os.path.normpath(IMAGE_ROOT)
        candidate_norm = os.path.normcase(os.path.normpath(candidate)) if os.name == "nt" else os.path.normpath(candidate)
        if candidate_norm.startswith(image_root_norm + os.sep) or candidate_norm == image_root_norm:
            relative_path = os.path.relpath(candidate, IMAGE_ROOT).replace("\\", "/")
            return relative_path
        raise ValueError("absolute filepath outside IMAGE_ROOT is not allowed")

    normalized_value = os.path.normpath(value)
    if normalized_value == os.pardir or normalized_value.startswith(os.pardir + os.sep):
        raise ValueError("filepath contains unsafe relative traversal")

    return value.replace("\\", "/")


def _normalize_legacy_absolute_image_paths(conn):
    """Convert legacy absolute image paths under IMAGE_ROOT to portable relative paths.

    Some rows already have a canonical relative-path row for the same physical
    file (left behind by earlier scans). Renaming the legacy row in place would
    collide with that row's UNIQUE(filepath) constraint, so such duplicates are
    merged into the canonical row (preserving any mapping) and the legacy row
    is dropped instead of renamed.
    """
    rows = conn.execute("SELECT id, filepath FROM images").fetchall()
    image_root_norm = os.path.normcase(os.path.normpath(IMAGE_ROOT)) if os.name == "nt" else os.path.normpath(IMAGE_ROOT)

    canonical_ids_by_relpath = {}
    legacy_rows = []
    for row in rows:
        raw_value = str(row["filepath"] or "").strip()
        if not raw_value:
            continue
        if os.path.isabs(raw_value):
            legacy_rows.append(row)
        else:
            canonical_ids_by_relpath[raw_value.strip("/").lower()] = int(row["id"])

    renamed = 0
    merged = 0
    for row in legacy_rows:
        raw_value = str(row["filepath"]).strip()
        abs_value = os.path.normpath(os.path.abspath(raw_value))
        abs_norm = os.path.normcase(abs_value) if os.name == "nt" else abs_value
        if not (abs_norm == image_root_norm or abs_norm.startswith(image_root_norm + os.sep)):
            continue

        relative_path = os.path.relpath(abs_value, IMAGE_ROOT).replace("\\", "/")
        if not relative_path or relative_path == ".":
            continue

        legacy_id = int(row["id"])
        canonical_id = canonical_ids_by_relpath.get(relative_path.strip("/").lower())

        if canonical_id is None:
            conn.execute("UPDATE images SET filepath = ? WHERE id = ?", (relative_path, legacy_id))
            canonical_ids_by_relpath[relative_path.strip("/").lower()] = legacy_id
            renamed += 1
            continue

        legacy_mapping = conn.execute(
            "SELECT stock_item_name, car_model, confidence, confirmed_by, created_at FROM mappings WHERE image_id = ?",
            (legacy_id,),
        ).fetchone()
        canonical_mapping = conn.execute(
            "SELECT created_at FROM mappings WHERE image_id = ?", (canonical_id,)
        ).fetchone()
        # Both rows may carry independent human-confirmed mappings (the legacy
        # duplicate wasn't always a dead end); keep whichever was confirmed most
        # recently rather than always favoring the canonical row.
        legacy_is_newer = legacy_mapping is not None and (
            canonical_mapping is None or str(legacy_mapping["created_at"]) > str(canonical_mapping["created_at"])
        )
        if legacy_is_newer:
            conn.execute(
                """
                INSERT INTO mappings (image_id, stock_item_name, car_model, confidence, confirmed_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id, stock_item_name) DO UPDATE SET
                    stock_item_name=excluded.stock_item_name,
                    car_model=excluded.car_model,
                    confidence=excluded.confidence,
                    confirmed_by=excluded.confirmed_by,
                    created_at=excluded.created_at
                """,
                (
                    canonical_id,
                    legacy_mapping["stock_item_name"],
                    legacy_mapping["car_model"],
                    legacy_mapping["confidence"],
                    legacy_mapping["confirmed_by"],
                    legacy_mapping["created_at"],
                ),
            )
        conn.execute("DELETE FROM images WHERE id = ?", (legacy_id,))
        merged += 1

    return renamed + merged


def _connect():
    machine_role.refuse_db_access_if_feeder()
    conn = sqlite3.connect(DB_PATH, timeout=Config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA cache_size = {-max(512, int(Config.SQLITE_CACHE_KB))}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection():
    """Mirrors sqlite3.Connection's own context-manager behavior (commit on
    a clean exit, rollback on an exception) and ALSO closes the connection
    either way -- unlike `with conn:` (a bare sqlite3.Connection used
    directly as its own context manager), whose __exit__ only commits/rolls
    back and never closes, leaving the connection (and its OS-level file
    descriptors) to be reclaimed only whenever CPython's refcounting or
    cyclic GC happens to get around to it. Every call site in this file was
    switched from `with _connect() as conn:` to `with get_connection() as
    conn:` for exactly this reason -- see the leak investigation this
    fixed. A caller that already does its own conn.commit() before this
    generator resumes is unaffected: committing twice with nothing pending
    is a no-op."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database():
    machine_role.refuse_db_access_if_feeder()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_folder TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL UNIQUE,
                scan_date TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                stock_item_name TEXT,
                car_model TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                confirmed_by TEXT NOT NULL DEFAULT 'human',
                created_at TEXT NOT NULL,
                FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folder_car_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_name TEXT NOT NULL UNIQUE,
                car_model_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                access_code TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'customer')),
                is_active INTEGER NOT NULL DEFAULT 1,
                last_login TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                performed_by INTEGER,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (performed_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS design_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_item_key TEXT NOT NULL UNIQUE,
                stock_item_name TEXT NOT NULL,
                category TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                assigned_by INTEGER,
                FOREIGN KEY (assigned_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        # Admin-editable category list (Part 1 of the category-settings
        # upgrade) -- design_categories.category above stores the category
        # NAME as a plain string, not a foreign key to this table, matching
        # how it always worked; membership is now validated in Python against
        # this live table (see category_exists()) instead of a fixed CHECK
        # constraint. name's uniqueness is case-insensitive (see the LOWER()
        # unique index below) because add/rename block an exact case-
        # insensitive duplicate at the application layer -- the index is the
        # hard backstop against a race doing the same.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL,
                abbreviation TEXT NOT NULL
            )
            """
        )
        # Two unrelated customer-facing features, one single-row table each:
        #   popup_notice  - optional text and/or image shown once per version
        #   banner_notice - required text shown as a non-dismissible marquee
        # Each `version` only ever increases (even across a clear) so a
        # customer who closed version N never mistakes newer content for it.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS popup_notice (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS banner_notice (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO popup_notice (id) VALUES (1)")
        conn.execute("INSERT OR IGNORE INTO banner_notice (id) VALUES (1)")
        # Single-row settings table -- currently just the Manage Accounts
        # gate password. Previously that password only lived in .env
        # (Config.ACCOUNTS_ACCESS_PASSWORD), which meant changing it
        # required editing a server file and restarting/redeploying the
        # app -- exactly the kind of friction that leaves an admin locked
        # out with no in-app way to fix it. DB-backed now so there's a real
        # "change password" UI (see /admin/accounts/change_password); the
        # INSERT below seeds it from the current .env value ONLY the first
        # time this table is created, so an existing deployment's password
        # keeps working unchanged after upgrading to this -- from then on
        # the DB row is authoritative and .env is no longer read for this.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                accounts_password TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (id, accounts_password) VALUES (1, ?)",
            (str(Config.ACCOUNTS_ACCESS_PASSWORD or "").strip(),),
        )
        # One-time carry-over from the earlier merged two-slot table: its text
        # slot becomes the popup text (and the banner too if it was important),
        # its image slot the popup image. Then the old table is dropped.
        has_old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'notice_slots'"
        ).fetchone()
        if has_old:
            old = {r["slot"]: r for r in conn.execute(
                "SELECT slot, version, active, content, important FROM notice_slots"
            ).fetchall()}
            old_text, old_image = old.get("text"), old.get("image")
            text_on = bool(old_text and old_text["active"])
            image_on = bool(old_image and old_image["active"])
            if text_on or image_on:
                conn.execute(
                    "UPDATE popup_notice SET version = 1, active = 1, text = ?, image = ? WHERE id = 1 AND version = 0",
                    (old_text["content"] if text_on else "", old_image["content"] if image_on else ""),
                )
            if text_on and old_text["important"]:
                conn.execute(
                    "UPDATE banner_notice SET version = 1, active = 1, text = ? WHERE id = 1 AND version = 0",
                    (old_text["content"],),
                )
            conn.execute("DROP TABLE notice_slots")
        # One-tap customer reports that a car has designs missing an image or
        # category. One unresolved report per car at a time (partial unique
        # index), so repeat taps never pile up duplicate rows.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car TEXT NOT NULL,
                car_key TEXT NOT NULL,
                details TEXT NOT NULL,
                reported_by TEXT NOT NULL DEFAULT '',
                reported_at TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT
            )
            """
        )
        report_columns = [row["name"] for row in conn.execute("PRAGMA table_info(customer_reports)")]
        if "reported_by" not in report_columns:
            conn.execute("ALTER TABLE customer_reports ADD COLUMN reported_by TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_reports_open_car ON customer_reports(car_key) WHERE resolved = 0"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_ci ON categories(LOWER(name))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_sort_order ON categories(sort_order)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_car_folder ON images(car_folder)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_car_folder_id ON images(car_folder, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item ON mappings(stock_item_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item_lower ON mappings(LOWER(stock_item_name), id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_image_id ON mappings(image_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mappings_image_stock_unique ON mappings(image_id, stock_item_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_filepath ON images(filepath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_folder_car_mapping ON folder_car_mapping(folder_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
        _ensure_users_schema(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_account_logs_timestamp ON account_logs(timestamp)")
        seed_default_data(conn)
        _seed_categories_table(conn)
        try:
            normalized_count = _normalize_legacy_absolute_image_paths(conn)
            if normalized_count:
                logger.info("Normalized %s legacy absolute image paths to relative paths", normalized_count)
        except Exception:
            logger.exception("Failed to normalize legacy image paths")

    _migrate_remove_image_id_unique()
    _migrate_drop_pricing_tables()
    _migrate_remove_design_categories_check_constraint()


DB_BACKUP_RETENTION_COUNT = 5


def _prune_old_database_backups(keep=DB_BACKUP_RETENTION_COUNT):
    """Keep only the most recent `keep` mappings.db.backup_* files.

    Safety net for any one-time migration (present or future) that calls
    _backup_database_file() -- the timestamp suffix (YYYYMMDD_HHMMSS) sorts
    both lexicographically and chronologically, so a plain sort is enough
    to find the oldest files to remove.
    """
    existing = sorted(glob.glob(f"{DB_PATH}.backup_*"))
    surplus = existing[:-keep] if keep > 0 else existing
    for stale_path in surplus:
        try:
            os.remove(stale_path)
            logger.info("Pruned old database backup: %s", stale_path)
        except OSError:
            logger.exception("Failed to prune old database backup: %s", stale_path)


def _backup_database_file():
    """Copy the live DB file, matching the timestamp convention used by
    the System panel's /admin/system/download_backup (backup_TIMESTAMP).

    Prunes older backups down to DB_BACKUP_RETENTION_COUNT afterward, so
    this is safe to call repeatedly (e.g. from future migrations) without
    accumulating backup files on disk indefinitely.
    """
    if not os.path.exists(DB_PATH):
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DB_PATH}.backup_{timestamp}"
    shutil.copy2(DB_PATH, backup_path)
    _prune_old_database_backups()
    return backup_path


def _migrate_remove_image_id_unique():
    """One-time migration: rebuild mappings without UNIQUE(image_id).

    The UNIQUE constraint made add_mapping()'s ON CONFLICT(image_id) upsert
    silently overwrite the existing row whenever the same image was confirmed
    against a second stock item, instead of creating a second row -- one
    image could never be linked to more than one stock item at a time.
    SQLite can't drop a column constraint via ALTER TABLE, so this rebuilds
    the table in a transaction, guarded to run only while the old unique
    auto-index is still present. Recreates a compound UNIQUE(image_id,
    stock_item_name) index so add_mapping()'s upsert still has a real
    constraint to target -- this only dedupes the exact same pair and does
    not restrict how many stock items a single image can map to.
    """
    conn = _connect()
    try:
        still_has_unique_image_id = False
        for row in conn.execute("PRAGMA index_list('mappings')").fetchall():
            if not row["unique"] or row["origin"] != "u":
                continue
            cols = conn.execute(f"PRAGMA index_info('{row['name']}')").fetchall()
            if [c["name"] for c in cols] == ["image_id"]:
                still_has_unique_image_id = True
                break

        if not still_has_unique_image_id:
            return

        backup_path = _backup_database_file()
        logger.info("Migrating mappings table to remove UNIQUE(image_id); backup at %s", backup_path)

        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE mappings_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    stock_item_name TEXT,
                    car_model TEXT,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    confirmed_by TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                INSERT INTO mappings_new (id, image_id, stock_item_name, car_model, confidence, confirmed_by, created_at)
                SELECT id, image_id, stock_item_name, car_model, confidence, confirmed_by, created_at FROM mappings
                """
            )
            conn.execute("DROP TABLE mappings")
            conn.execute("ALTER TABLE mappings_new RENAME TO mappings")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item ON mappings(stock_item_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item_lower ON mappings(LOWER(stock_item_name), id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_image_id ON mappings(image_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mappings_image_stock_unique ON mappings(image_id, stock_item_name)")
            conn.commit()
            logger.info("Migration succeeded: mappings.image_id UNIQUE constraint removed.")
        except Exception:
            conn.rollback()
            logger.exception(
                "Migration failed removing UNIQUE(image_id); rolled back. Backup preserved at %s",
                backup_path,
            )
            raise
    finally:
        conn.close()


def _migrate_drop_pricing_tables():
    """One-time migration: drop the base_prices/customer_prices tables.

    Both tables were leftovers from a pricing feature that was removed from
    the app; no code reads or writes either table anymore (customer_prices'
    only remaining reference was a cleanup DELETE in delete_customer_user(),
    removed alongside this migration). Guarded to run only while either
    table is still present (checked via sqlite_master), so it's safe to
    call on every startup -- a no-op once both are gone.
    """
    conn = _connect()
    try:
        present = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('base_prices', 'customer_prices')"
            ).fetchall()
        }
        if not present:
            return

        backup_path = _backup_database_file()
        logger.info("Migrating away leftover pricing tables %s; backup at %s", sorted(present), backup_path)

        conn.execute("BEGIN")
        try:
            if "customer_prices" in present:
                conn.execute("DROP TABLE customer_prices")
            if "base_prices" in present:
                conn.execute("DROP TABLE base_prices")
            conn.commit()
            logger.info("Migration succeeded: leftover pricing tables dropped.")
        except Exception:
            conn.rollback()
            logger.exception(
                "Migration failed dropping pricing tables; rolled back. Backup preserved at %s",
                backup_path,
            )
            raise
    finally:
        conn.close()


def _ensure_users_schema(conn):
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    columns = {str(row[1]).lower() for row in rows}
    if "created_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        conn.execute("UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL OR TRIM(created_at) = ''")
    if "is_active" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "last_login" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
    if "last_active_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT")

    # Small rows only: no IPs, no user agents. ON DELETE CASCADE so deleting
    # a dealer account takes their activity with it (foreign_keys is ON for
    # every connection, see _connect()).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            at_utc TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_activity_user_at ON user_activity (user_id, at_utc)"
    )

    # Permanent, tiny summaries of user_activity (see activity_tiers.py). They
    # outlive the raw rows, which are archived to Drive and then removed once
    # their whole month is past the raw window. No FOREIGN KEY on purpose: a
    # deleted dealer's history stays in the totals.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_daily (
            day_ist TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            count INTEGER NOT NULL,
            visits INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day_ist, user_id, action)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_hourly (
            day_ist TEXT NOT NULL,
            hour_ist INTEGER NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (day_ist, hour_ist)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_top_monthly (
            month TEXT NOT NULL,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (month, kind, label)
        ) WITHOUT ROWID
        """
    )
    # One row per finished IST day ('d:2026-10-09') / month ('m:2026-10'). Needed
    # because "a day with no activity" has no rows in the tables above, and
    # because pruning must be able to prove a day's summary exists.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_summary_done (
            key TEXT PRIMARY KEY,
            computed_at TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            performed_by INTEGER,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (performed_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )


def seed_default_data(conn=None):
    own_connection = conn is None
    if own_connection:
        conn = _connect()

    try:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        user_count = int(row["count"] if row else 0)
        if user_count > 0:
            return

        conn.executemany(
            """
            INSERT INTO users (username, access_code, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin", "idris123", "admin", datetime.now().isoformat(timespec="seconds")),
                ("star", "111", "customer", datetime.now().isoformat(timespec="seconds")),
                ("jeewajee", "222", "customer", datetime.now().isoformat(timespec="seconds")),
            ],
        )
    finally:
        if own_connection and conn is not None:
            conn.close()


def _seed_categories_table(conn):
    """One-time seed of the `categories` table (Part 1.2) from the previous
    fixed DESIGN_CATEGORY_CHOICES tuple and its hand-curated abbreviations,
    preserving the exact existing names/order/abbreviations so an upgrade is
    a transparent no-visible-change event for the current 7 categories.
    Guarded like seed_default_data() -- runs only while the table is empty,
    so it's a no-op on every startup after the first."""
    row = conn.execute("SELECT COUNT(*) AS count FROM categories").fetchone()
    if int(row["count"] if row else 0) > 0:
        return
    conn.executemany(
        "INSERT INTO categories (name, sort_order, abbreviation) VALUES (?, ?, ?)",
        [
            (name, sort_order, DESIGN_CATEGORY_ABBREVIATIONS_SEED.get(name) or generate_category_abbreviation(name))
            for sort_order, name in enumerate(DESIGN_CATEGORY_CHOICES)
        ],
    )


def _migrate_remove_design_categories_check_constraint():
    """One-time migration: rebuild design_categories without its old
    CHECK(category IN (...)) constraint tied to the now-retired fixed
    DESIGN_CATEGORY_CHOICES tuple (Part 1.3).

    Category membership is validated in Python against the live `categories`
    table now (see category_exists()), so the CHECK would otherwise block
    admin-added categories from ever being assigned. Same guarded,
    backup-then-rebuild-in-a-transaction pattern as
    _migrate_remove_image_id_unique() / _migrate_drop_pricing_tables():
    checked via the table's own recorded CREATE TABLE sql in sqlite_master,
    backed up first, rolled back and the backup preserved on any failure.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='design_categories'"
        ).fetchone()
        table_sql = row["sql"] if row else None
        if not table_sql or "CHECK" not in table_sql.upper():
            return

        backup_path = _backup_database_file()
        logger.info(
            "Migrating design_categories to remove fixed CHECK(category IN (...)) constraint; backup at %s",
            backup_path,
        )

        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE design_categories_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_item_key TEXT NOT NULL UNIQUE,
                    stock_item_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    assigned_by INTEGER,
                    FOREIGN KEY (assigned_by) REFERENCES users(id) ON DELETE SET NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO design_categories_new (id, stock_item_key, stock_item_name, category, assigned_at, assigned_by)
                SELECT id, stock_item_key, stock_item_name, category, assigned_at, assigned_by FROM design_categories
                """
            )
            conn.execute("DROP TABLE design_categories")
            conn.execute("ALTER TABLE design_categories_new RENAME TO design_categories")
            conn.commit()
            logger.info("Migration succeeded: design_categories CHECK constraint removed.")
        except Exception:
            conn.rollback()
            logger.exception(
                "Migration failed removing design_categories CHECK constraint; rolled back. Backup preserved at %s",
                backup_path,
            )
            raise
    finally:
        conn.close()


def _row_to_dict(row):
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Content signatures -- what the "Stock updated" notice keys off.
#
# A signature is a hash of exactly the stored rows a dealer (or, for the
# accounts signature, an admin) can SEE, so it changes only when what they
# see changes -- never for last_active / last_login / user_activity writes,
# image scan_date refreshes, or an admin action that rewrote identical
# values. It is derived from stored content (not a counter), so it is the
# same after a restart and needs no table or setting of its own.
#
# The mutators below are decorated to flag the signature stale; the next
# get_content_signatures() call (one poll, after a change) recomputes it.
# Steady-state polls do no DB work at all.
# ---------------------------------------------------------------------------
_signature_lock = threading.Lock()
_dealer_dirty = True
_accounts_dirty = True
_dealer_signature = ""
_accounts_signature = ""


def _marks_content_dirty(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _dealer_dirty
        try:
            return fn(*args, **kwargs)
        finally:
            _dealer_dirty = True
    return wrapper


def _marks_accounts_dirty(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _accounts_dirty
        try:
            return fn(*args, **kwargs)
        finally:
            _accounts_dirty = True
    return wrapper


def _hash_rows(conn, queries):
    digest = hashlib.sha1()
    for sql in queries:
        digest.update(sql.encode("ascii"))
        for row in conn.execute(sql):
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


def get_content_signatures():
    """(dealer_signature, accounts_signature). Recomputed only if a flagged
    mutator ran since the last call. The dirty flag is cleared BEFORE the
    read, so a write landing mid-read re-flags it and is picked up next time."""
    global _dealer_dirty, _accounts_dirty, _dealer_signature, _accounts_signature
    with _signature_lock:
        if _dealer_dirty or _accounts_dirty:
            do_dealer, do_accounts = _dealer_dirty, _accounts_dirty
            _dealer_dirty = _accounts_dirty = False
            try:
                with get_connection() as conn:
                    if do_dealer:
                        # scan_date is deliberately left out: a rescan
                        # refreshes it on every image without changing
                        # anything visible.
                        _dealer_signature = _hash_rows(conn, (
                            "SELECT name, abbreviation, sort_order FROM categories ORDER BY id",
                            "SELECT stock_item_key, category FROM design_categories ORDER BY stock_item_key",
                            "SELECT image_id, stock_item_name FROM mappings ORDER BY image_id, stock_item_name",
                            "SELECT id, car_folder, filename, filepath FROM images ORDER BY id",
                        ))
                    if do_accounts:
                        # last_login / last_active / access_code excluded.
                        _accounts_signature = _hash_rows(conn, (
                            "SELECT id, username, role, is_active FROM users ORDER BY id",
                        ))
            except Exception:
                _dealer_dirty = _dealer_dirty or do_dealer
                _accounts_dirty = _accounts_dirty or do_accounts
                raise
        return _dealer_signature, _accounts_signature


@_marks_content_dirty
def add_image(car_folder, filename, filepath):
    filepath = _validate_filepath(filepath)
    scan_date = datetime.now().isoformat(timespec="seconds")
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO images (car_folder, filename, filepath, scan_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    car_folder=excluded.car_folder,
                    filename=excluded.filename,
                    scan_date=excluded.scan_date
                """,
                (car_folder, filename, filepath, scan_date),
            )
            return cursor.lastrowid
    except Exception:
        logger.exception("add_image failed for filepath=%s", filepath)
        raise


@_marks_content_dirty
def add_images_batch(records):
    """Batch upsert image records.

    records: iterable of (car_folder, filename, filepath)
    """
    scan_date = datetime.now().isoformat(timespec="seconds")
    values = []
    for car_folder, filename, filepath in records:
        try:
            safe_path = _validate_filepath(filepath)
        except Exception:
            logger.warning("Skipping invalid filepath during batch insert: %s", filepath)
            continue
        values.append((car_folder, filename, safe_path, scan_date))

    if not values:
        return 0

    try:
        with get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO images (car_folder, filename, filepath, scan_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    car_folder=excluded.car_folder,
                    filename=excluded.filename,
                    scan_date=excluded.scan_date
                """,
                values,
            )
        return len(values)
    except Exception:
        logger.exception("add_images_batch failed")
        raise


@_marks_content_dirty
def add_mapping(image_id, stock_item_name, car_model, confidence, confirmed_by="human"):
    stock_item_name = _validate_stock_item_name(stock_item_name)
    confidence = _validate_confidence(confidence)
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO mappings (image_id, stock_item_name, car_model, confidence, confirmed_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id, stock_item_name) DO UPDATE SET
                    stock_item_name=excluded.stock_item_name,
                    car_model=excluded.car_model,
                    confidence=excluded.confidence,
                    confirmed_by=excluded.confirmed_by,
                    created_at=excluded.created_at
                """,
                (image_id, stock_item_name, car_model, confidence, confirmed_by, created_at),
            )
    except Exception:
        logger.exception("add_mapping failed for image_id=%s", image_id)
        raise


@_marks_content_dirty
def remove_mapping_by_image_id(image_id):
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM mappings WHERE image_id = ?",
                (image_id,),
            )
        return cursor.rowcount > 0
    except Exception:
        logger.exception("remove_mapping_by_image_id failed for image_id=%s", image_id)
        raise


def add_folder_mapping(folder_name, car_model):
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO folder_car_mapping (folder_name, car_model_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(folder_name) DO UPDATE SET
                    car_model_name=excluded.car_model_name,
                    created_at=excluded.created_at
                """,
                (folder_name, car_model, created_at),
            )
    except Exception:
        logger.exception("add_folder_mapping failed for folder=%s", folder_name)
        raise


def get_folder_car_model(folder_name):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT folder_name, car_model_name, created_at FROM folder_car_mapping WHERE folder_name = ?",
            (folder_name,),
        ).fetchone()
    return _row_to_dict(row)


def get_image_by_id(image_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, car_folder, filename, filepath, scan_date FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_mapping_by_image_id(image_id):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, image_id, stock_item_name, car_model, confidence, confirmed_by, created_at
            FROM mappings
            WHERE image_id = ?
            """,
            (image_id,),
        ).fetchone()
    return _row_to_dict(row)


@_marks_content_dirty
def remove_mappings_for_stock_item(stock_item_name, exclude_image_id=None):
    """Delete any mapping(s) pointing at stock_item_name, optionally excluding one image_id.

    Used so a stock item is only ever mapped to a single image at a time.
    """
    try:
        with get_connection() as conn:
            if exclude_image_id is not None:
                cursor = conn.execute(
                    "DELETE FROM mappings WHERE LOWER(stock_item_name) = LOWER(?) AND image_id != ?",
                    (stock_item_name, exclude_image_id),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM mappings WHERE LOWER(stock_item_name) = LOWER(?)",
                    (stock_item_name,),
                )
        return cursor.rowcount
    except Exception:
        logger.exception("remove_mappings_for_stock_item failed for stock_item_name=%s", stock_item_name)
        raise


def get_mapping_for_stock_item(stock_item_name):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                m.id,
                m.image_id,
                m.stock_item_name,
                m.car_model,
                m.confidence,
                m.confirmed_by,
                m.created_at,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date
            FROM mappings m
            JOIN images i ON i.id = m.image_id
            WHERE LOWER(m.stock_item_name) = LOWER(?)
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 1
            """,
            (stock_item_name,),
        ).fetchone()
    return _row_to_dict(row)


def get_mappings_for_stock_items(stock_item_names):
    cleaned_keys = set()
    for stock_item_name in stock_item_names or []:
        try:
            value = _validate_stock_item_name(stock_item_name)
        except ValueError:
            continue
        key = normalize_lookup_key(value)
        if key:
            cleaned_keys.add(key)

    if not cleaned_keys:
        return {}

    # Matching is done in Python (not SQL LOWER()) because stock item names
    # commonly carry irregular internal whitespace from Tally exports, and
    # normalize_lookup_key() collapses those runs -- something plain SQL
    # LOWER() cannot replicate. Using the same shared key on both the input
    # names and the stored rows is what keeps this in sync with the app-side
    # lookup builder (see _normalize_lookup_key in app.py).
    query = """
        SELECT
            m.id,
            m.image_id,
            m.stock_item_name,
            m.car_model,
            m.confidence,
            m.confirmed_by,
            m.created_at,
            i.car_folder,
            i.filename,
            i.filepath,
            i.scan_date
        FROM mappings m
        JOIN images i ON i.id = m.image_id
    """
    with get_connection() as conn:
        rows = conn.execute(query).fetchall()

    result = {}
    for row in rows:
        record = _row_to_dict(row)
        key = normalize_lookup_key(record.get("stock_item_name"))
        if key not in cleaned_keys:
            continue
        existing = result.get(key)
        candidate_rank = (record.get("created_at") or "", record.get("id") or 0)
        existing_rank = (existing.get("created_at") or "", existing.get("id") or 0) if existing else None
        if existing is None or candidate_rank > existing_rank:
            result[key] = record
    return result


def get_all_categories():
    """All categories in display order (Part 6's reorder controls change
    sort_order; this is the one place that order is read back)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, sort_order, abbreviation FROM categories ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_popup():
    """The active popup as {version, text, image} (image is a stored filename,
    '' when none), or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT version, text, image FROM popup_notice WHERE id = 1 AND active = 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def publish_popup(text, image=""):
    """Replaces the popup (text and/or image) and bumps its version."""
    if not (text or image):
        raise ValueError("a popup needs text or an image")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE popup_notice
            SET version = version + 1, active = 1, text = ?, image = ?, updated_at = datetime('now')
            WHERE id = 1
            """,
            (text, image),
        )
        row = conn.execute("SELECT version FROM popup_notice WHERE id = 1").fetchone()
    return row["version"]


def set_popup_image(image):
    """Swaps the stored image filename in place (no version bump) -- used to
    replace a temp upload name with its versioned one."""
    with get_connection() as conn:
        conn.execute("UPDATE popup_notice SET image = ? WHERE id = 1", (image,))


def clear_popup():
    with get_connection() as conn:
        conn.execute("UPDATE popup_notice SET active = 0, text = '', image = '' WHERE id = 1")


def get_banner():
    """The active banner as {version, text}, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT version, text FROM banner_notice WHERE id = 1 AND active = 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def publish_banner(text):
    if not text:
        raise ValueError("a banner needs text")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE banner_notice
            SET version = version + 1, active = 1, text = ?, updated_at = datetime('now')
            WHERE id = 1
            """,
            (text,),
        )
        row = conn.execute("SELECT version FROM banner_notice WHERE id = 1").fetchone()
    return row["version"]


def clear_banner():
    with get_connection() as conn:
        conn.execute("UPDATE banner_notice SET active = 0, text = '' WHERE id = 1")


def add_customer_report(car, car_key, details, reported_by=""):
    """Records a report unless the car already has an unresolved one.
    Returns True when a new row was created, False for a repeat."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO customer_reports (car, car_key, details, reported_by, reported_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (car, car_key, details, reported_by, datetime.now().isoformat(timespec="seconds")),
        )
        return cursor.rowcount > 0


def list_open_customer_reports():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, car, details, reported_by, reported_at FROM customer_reports WHERE resolved = 0 ORDER BY id ASC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def resolve_customer_report(report_id):
    """Marks one open report resolved. Returns False if it wasn't open."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE customer_reports SET resolved = 1, resolved_at = ? WHERE id = ? AND resolved = 0",
            (datetime.now().isoformat(timespec="seconds"), int(report_id)),
        )
        return cursor.rowcount > 0


def category_exists(name):
    value = str(name or "").strip()
    if not value:
        return False
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM categories WHERE name = ? LIMIT 1", (value,)).fetchone()
    return row is not None


def get_category_by_name(name):
    value = str(name or "").strip()
    if not value:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, name, sort_order, abbreviation FROM categories WHERE name = ? LIMIT 1", (value,)
        ).fetchone()
    return _row_to_dict(row)


def find_category_by_name_ci(name, exclude_name=None):
    """Case-insensitive category lookup, optionally excluding one exact name.

    Used by the rename route to tell a same-category case tweak (e.g. "Saka"
    -> "SAKA") apart from a real merge conflict with a DIFFERENT existing
    category (Part 3.2)."""
    value = str(name or "").strip()
    if not value:
        return None
    with get_connection() as conn:
        if exclude_name is not None:
            row = conn.execute(
                "SELECT id, name, sort_order, abbreviation FROM categories WHERE LOWER(name) = LOWER(?) AND name != ? LIMIT 1",
                (value, exclude_name),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, sort_order, abbreviation FROM categories WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (value,),
            ).fetchone()
    return _row_to_dict(row)


def count_items_for_category(name):
    """How many design_categories rows are currently tagged with `name` --
    the affected-item count shown in the rename-merge and delete warnings
    (Part 3.2/3.3)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM design_categories WHERE category = ?", (name,)
        ).fetchone()
    return int(row["count"] if row else 0)


@_marks_content_dirty
def add_category(name):
    """Adds a new category (Part 3.1): auto-generated abbreviation, always
    appended to the end of the display order. Raises ValueError on a blank
    name, an oversized name, or an exact case-insensitive duplicate."""
    value = str(name or "").strip()
    if not value:
        raise ValueError("category name is required")
    if len(value) > CATEGORY_NAME_MAX_LENGTH:
        raise ValueError(f"category name exceeds {CATEGORY_NAME_MAX_LENGTH} characters")
    if find_category_by_name_ci(value):
        raise ValueError(f"a category named '{value}' already exists")

    abbreviation = generate_category_abbreviation(value)
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS max_order FROM categories").fetchone()
            next_order = int(row["max_order"] if row else -1) + 1
            cursor = conn.execute(
                "INSERT INTO categories (name, sort_order, abbreviation) VALUES (?, ?, ?)",
                (value, next_order, abbreviation),
            )
            category_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"a category named '{value}' already exists") from exc
    return {"id": category_id, "name": value, "sort_order": next_order, "abbreviation": abbreviation}


@_marks_content_dirty
def rename_category(old_name, new_name):
    """Renames a category and cascades the new name onto every
    design_categories row that referenced the old one, atomically (Part
    3.2's non-conflict path). Caller is responsible for having already ruled
    out a merge conflict with a DIFFERENT existing category via
    find_category_by_name_ci() -- this function does not itself check for
    one, so calling it when a real conflict exists would raise a plain
    UNIQUE-constraint IntegrityError instead of the friendlier merge flow.
    Returns the number of design_categories rows updated."""
    old_value = str(old_name or "").strip()
    new_value = str(new_name or "").strip()
    if not new_value:
        raise ValueError("new category name is required")
    if len(new_value) > CATEGORY_NAME_MAX_LENGTH:
        raise ValueError(f"category name exceeds {CATEGORY_NAME_MAX_LENGTH} characters")

    conn = _connect()
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute("SELECT id FROM categories WHERE name = ?", (old_value,)).fetchone()
            if not row:
                raise ValueError(f"category '{old_value}' not found")
            conn.execute("UPDATE categories SET name = ? WHERE name = ?", (new_value, old_value))
            cursor = conn.execute(
                "UPDATE design_categories SET category = ? WHERE category = ?", (new_value, old_value)
            )
            affected = cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return affected


@_marks_content_dirty
def merge_categories(source_name, target_name):
    """Moves every design_categories row from source_name to target_name,
    then deletes the now-empty source category row, atomically (Part 3.2's
    confirmed-merge path). Returns the number of design_categories rows
    moved."""
    source_value = str(source_name or "").strip()
    target_value = str(target_name or "").strip()

    conn = _connect()
    try:
        conn.execute("BEGIN")
        try:
            source_row = conn.execute("SELECT id FROM categories WHERE name = ?", (source_value,)).fetchone()
            target_row = conn.execute("SELECT id FROM categories WHERE name = ?", (target_value,)).fetchone()
            if not source_row:
                raise ValueError(f"category '{source_value}' not found")
            if not target_row:
                raise ValueError(f"category '{target_value}' not found")
            cursor = conn.execute(
                "UPDATE design_categories SET category = ? WHERE category = ?", (target_value, source_value)
            )
            affected = cursor.rowcount
            conn.execute("DELETE FROM categories WHERE name = ?", (source_value,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return affected


@_marks_content_dirty
def delete_category(name):
    """Deletes the category row and REMOVES (not blanks) every
    design_categories row that had it, atomically (Part 3.3) -- reverting
    those stock items to the same "uncategorized" state as an item that was
    never tagged. Returns the list of stock_item_names that were affected,
    so the caller can clean up their now-orphaned cached badge images."""
    value = str(name or "").strip()
    conn = _connect()
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute("SELECT id FROM categories WHERE name = ?", (value,)).fetchone()
            if not row:
                raise ValueError(f"category '{value}' not found")
            affected_rows = conn.execute(
                "SELECT stock_item_name FROM design_categories WHERE category = ?", (value,)
            ).fetchall()
            affected_names = [r["stock_item_name"] for r in affected_rows]
            conn.execute("DELETE FROM design_categories WHERE category = ?", (value,))
            conn.execute("DELETE FROM categories WHERE name = ?", (value,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return affected_names


@_marks_content_dirty
def update_category_abbreviation(name, abbreviation):
    """System panel abbreviation override (Part 6.3)."""
    value = str(name or "").strip()
    abbr_value = str(abbreviation or "").strip()
    if not abbr_value:
        raise ValueError("abbreviation is required")
    if len(abbr_value) > CATEGORY_ABBREVIATION_MAX_LENGTH:
        raise ValueError(f"abbreviation exceeds {CATEGORY_ABBREVIATION_MAX_LENGTH} characters")
    with get_connection() as conn:
        cursor = conn.execute("UPDATE categories SET abbreviation = ? WHERE name = ?", (abbr_value, value))
        if cursor.rowcount == 0:
            raise ValueError(f"category '{value}' not found")
    return abbr_value


@_marks_content_dirty
def move_category(name, direction):
    """Swaps sort_order with the immediate neighbor above/below (Part 6.2's
    up/down arrow buttons) -- a simple adjacent swap, no drag-and-drop
    reindexing needed. Returns False (no-op) if `name` is already at that
    edge of the list."""
    value = str(name or "").strip()
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, sort_order FROM categories ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        ordered = [dict(r) for r in rows]
        index = next((i for i, r in enumerate(ordered) if r["name"] == value), None)
        if index is None:
            raise ValueError(f"category '{value}' not found")

        neighbor_index = index - 1 if direction == "up" else index + 1
        if neighbor_index < 0 or neighbor_index >= len(ordered):
            return False

        current = ordered[index]
        neighbor = ordered[neighbor_index]
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (neighbor["sort_order"], current["id"]))
            conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (current["sort_order"], neighbor["id"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return True
    finally:
        conn.close()


@_marks_content_dirty
def upsert_design_category(stock_item_name, category, assigned_by):
    """Assign/overwrite a stock item's material-tier category (last-write-wins,
    see design_categories table). Keyed on normalize_lookup_key() so the same
    whitespace-tolerant matching used everywhere else in this codebase
    (get_mappings_for_stock_items(), _build_design_payload()) also governs
    category lookups."""
    stock_item_name = _validate_stock_item_name(stock_item_name)
    if not category_exists(category):
        raise ValueError(f"invalid category: {category!r}")
    key = normalize_lookup_key(stock_item_name)
    if not key:
        raise ValueError("stock item name is required")

    assigned_at = datetime.now().isoformat(timespec="seconds")
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO design_categories (stock_item_key, stock_item_name, category, assigned_at, assigned_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(stock_item_key) DO UPDATE SET
                    stock_item_name=excluded.stock_item_name,
                    category=excluded.category,
                    assigned_at=excluded.assigned_at,
                    assigned_by=excluded.assigned_by
                """,
                (key, stock_item_name, category, assigned_at, assigned_by),
            )
    except Exception:
        logger.exception("upsert_design_category failed for stock_item_name=%s", stock_item_name)
        raise


def get_category_for_stock_item(stock_item_name):
    key = normalize_lookup_key(stock_item_name)
    if not key:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT category FROM design_categories WHERE stock_item_key = ?",
            (key,),
        ).fetchone()
    return row["category"] if row else None


@_marks_content_dirty
def remove_design_category(stock_item_name):
    """Clears a single stock item's category assignment -- the explicit
    "(No category)" clear action in Training Mode's confirm-match category
    picker. Distinct from delete_category() above, which removes every
    item's assignment for an entire category name at once; this only ever
    touches the one item. Returns True if a row was actually removed."""
    key = normalize_lookup_key(stock_item_name)
    if not key:
        return False
    try:
        with get_connection() as conn:
            cursor = conn.execute("DELETE FROM design_categories WHERE stock_item_key = ?", (key,))
        return cursor.rowcount > 0
    except Exception:
        logger.exception("remove_design_category failed for stock_item_name=%s", stock_item_name)
        raise


def get_categories_for_stock_items(stock_item_names):
    """Batched form of get_category_for_stock_item() -- same shape/spirit as
    get_mappings_for_stock_items() above. Returns {normalized_key: category}."""
    cleaned_keys = set()
    for stock_item_name in stock_item_names or []:
        key = normalize_lookup_key(stock_item_name)
        if key:
            cleaned_keys.add(key)

    if not cleaned_keys:
        return {}

    placeholders = ",".join("?" for _ in cleaned_keys)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT stock_item_key, category FROM design_categories WHERE stock_item_key IN ({placeholders})",
            tuple(cleaned_keys),
        ).fetchall()
    return {row["stock_item_key"]: row["category"] for row in rows}


def get_confirmed_mappings():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.image_id,
                m.stock_item_name,
                m.car_model,
                m.confidence,
                m.confirmed_by,
                m.created_at,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date
            FROM mappings m
            JOIN images i ON i.id = m.image_id
            WHERE m.confidence >= 1.0
            ORDER BY m.created_at DESC, m.id DESC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_unmapped_images(limit=None):
    query = """
        WITH ranked_unmapped AS (
            SELECT
                i.id,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(i.filepath)
                    ORDER BY i.id DESC
                ) AS rn
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            WHERE m.image_id IS NULL
        )
        SELECT id, car_folder, filename, filepath, scan_date
        FROM ranked_unmapped
        WHERE rn = 1
        ORDER BY LOWER(filename) ASC, id ASC
    """
    params = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_unmapped_images_by_folder(folder_name, limit=None):
    """Return unmapped images restricted to a specific folder name.

    This performs the filtering at the SQL level so callers don't need to
    pull the full unmapped set into memory.
    """
    query = """
        WITH ranked_unmapped AS (
            SELECT
                i.id,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(i.filepath)
                    ORDER BY i.id DESC
                ) AS rn
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            WHERE m.image_id IS NULL
              AND i.car_folder = ?
        )
        SELECT id, car_folder, filename, filepath, scan_date
        FROM ranked_unmapped
        WHERE rn = 1
        ORDER BY LOWER(filename) ASC, id ASC
    """
    params = [folder_name]
    if limit is not None:
        query = query.strip() + " LIMIT ?"
        params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_images_by_folder(folder_name, limit=None):
    """Return all images for a folder with mapping status details."""
    query = """
        WITH ranked_images AS (
            SELECT
                i.id,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date,
                CASE WHEN m.image_id IS NULL THEN 0 ELSE 1 END AS mapped,
                m.stock_item_name,
                m.confidence,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(i.filepath)
                    ORDER BY
                        CASE WHEN m.image_id IS NULL THEN 1 ELSE 0 END ASC,
                        i.id DESC
                ) AS rn
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            WHERE i.car_folder = ?
        )
        SELECT
            id,
            car_folder,
            filename,
            filepath,
            scan_date,
            mapped,
            stock_item_name,
            confidence
        FROM ranked_images
        WHERE rn = 1
        ORDER BY LOWER(filename) ASC, id ASC
    """
    params = [folder_name]
    if limit is not None:
        query = query.strip() + " LIMIT ?"
        params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_next_unmapped_image(after_image_id=None):
    if after_image_id is None:
        rows = get_unmapped_images(limit=1)
        return rows[0] if rows else None

    with get_connection() as conn:
        row = conn.execute(
            """
            WITH ranked_unmapped AS (
                SELECT
                    i.id,
                    i.car_folder,
                    i.filename,
                    i.filepath,
                    i.scan_date,
                    ROW_NUMBER() OVER (
                        PARTITION BY LOWER(i.filepath)
                        ORDER BY i.id DESC
                    ) AS rn
                FROM images i
                LEFT JOIN mappings m ON m.image_id = i.id
                WHERE m.image_id IS NULL
                  AND i.id > ?
            )
            SELECT id, car_folder, filename, filepath, scan_date
            FROM ranked_unmapped
            WHERE rn = 1
            ORDER BY id ASC
            LIMIT 1
            """,
            (after_image_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_image_count():
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM images").fetchone()
    return int(row["count"] if row else 0)


def get_mapped_image_count():
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM mappings
            WHERE TRIM(COALESCE(stock_item_name, '')) <> ''
              AND stock_item_name <> '__UNMATCHABLE__'
            """
        ).fetchone()
    return int(row["count"] if row else 0)


def get_processed_image_count():
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM mappings").fetchone()
    return int(row["count"] if row else 0)


def get_mapping_stats():
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM images) AS total_images,
                (
                    SELECT COUNT(*)
                    FROM mappings
                    WHERE TRIM(COALESCE(stock_item_name, '')) <> ''
                      AND stock_item_name <> '__UNMATCHABLE__'
                ) AS mapped_images,
                (SELECT COUNT(*) FROM mappings) AS processed_images
            """
        ).fetchone()
    total_images = int(row["total_images"] if row else 0)
    mapped_images = int(row["mapped_images"] if row else 0)
    processed_images = int(row["processed_images"] if row else 0)
    percent = round((processed_images / total_images) * 100, 2) if total_images else 0.0
    return {
        "total_images": total_images,
        "mapped_images": mapped_images,
        "unmapped_images": max(total_images - processed_images, 0),
        "percent_complete": percent,
    }


def get_all_images():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, car_folder, filename, filepath, scan_date FROM images ORDER BY id ASC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_all_images_with_link_status():
    """Return every image row plus whether it currently has a confirmed stock-item mapping.

    Uses an EXISTS subquery rather than a LEFT JOIN because a single image can
    have more than one mapping row (the UNIQUE(image_id) constraint was
    removed -- see _migrate_remove_image_id_unique), so a join would return
    one row per mapping and inflate counts for anything that iterates this.
    "Confirmed" mirrors get_mapped_image_count()'s definition: a non-empty
    stock_item_name that isn't the '__UNMATCHABLE__' sentinel.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id, i.car_folder, i.filename, i.filepath, i.scan_date,
                EXISTS (
                    SELECT 1 FROM mappings m
                    WHERE m.image_id = i.id
                      AND TRIM(COALESCE(m.stock_item_name, '')) <> ''
                      AND m.stock_item_name <> '__UNMATCHABLE__'
                ) AS mapped
            FROM images i
            ORDER BY i.id ASC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_stock_item_names_for_images(image_ids):
    """Return {image_id: [stock_item_name, ...]} of confirmed mappings for the given ids.

    Scoped to a specific id set (rather than joining onto every image) so
    callers who only need this for a small subset -- e.g. the missing-images
    list -- don't pay for a catalog-wide GROUP_CONCAT. An image can have more
    than one mapping row (bulk match links one image to many stock items),
    so this returns a list per image rather than a single name. "Confirmed"
    mirrors get_mapped_image_count()'s definition.
    """
    ids = sorted({int(image_id) for image_id in (image_ids or [])})
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT image_id, stock_item_name
            FROM mappings
            WHERE image_id IN ({placeholders})
              AND TRIM(COALESCE(stock_item_name, '')) <> ''
              AND stock_item_name <> '__UNMATCHABLE__'
            ORDER BY image_id ASC, created_at DESC, id DESC
            """,
            ids,
        ).fetchall()

    result = defaultdict(list)
    for row in rows:
        result[int(row["image_id"])].append(row["stock_item_name"])
    return dict(result)


def get_image_folders():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT car_folder
            FROM images
            WHERE car_folder IS NOT NULL AND TRIM(car_folder) <> ''
            ORDER BY LOWER(car_folder), car_folder
            """
        ).fetchall()
    return [row["car_folder"] for row in rows]


def _hash_file(file_path, chunk_size=65536):
    hasher = hashlib.sha256()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_image_root_path(stored_path):
    value = str(stored_path or "").strip().replace("\\", "/")
    if not value:
        return None
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(IMAGE_ROOT, value))


def find_duplicate_image_rows():
    """Find image rows that are very likely visible duplicates.

    Candidate grouping is (car_folder, filename with extension stripped and
    lowercased) — e.g. "BLK-DKTAN" and "BLK-DKTAN.jpg" land in the same
    candidate group. Candidates are only reported as duplicates once
    confirmed by content hash, so two different photos that happen to share
    a generic base name (e.g. "1.jfif" reused across unrelated subfolders)
    are never wrongly flagged.

    Read-only: does not delete or modify anything.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id, i.car_folder, i.filename, i.filepath, i.scan_date,
                CASE WHEN m.image_id IS NULL THEN 0 ELSE 1 END AS mapped,
                m.stock_item_name
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            """
        ).fetchall()

    candidates = defaultdict(list)
    for row in rows:
        stem = os.path.splitext(str(row["filename"] or ""))[0].strip().lower()
        folder_key = str(row["car_folder"] or "").strip().lower()
        if not stem:
            continue
        candidates[(folder_key, stem)].append(_row_to_dict(row))

    duplicate_groups = []
    for (_folder_key, stem), group_rows in candidates.items():
        if len(group_rows) < 2:
            continue

        by_hash = defaultdict(list)
        for row in group_rows:
            file_path = _resolve_image_root_path(row.get("filepath"))
            if not file_path or not os.path.exists(file_path):
                continue
            try:
                file_hash = _hash_file(file_path)
            except OSError:
                continue
            by_hash[file_hash].append(row)

        for file_hash, hashed_rows in by_hash.items():
            if len(hashed_rows) < 2:
                continue
            duplicate_groups.append({
                "car_folder": hashed_rows[0].get("car_folder"),
                "filename_stem": stem,
                "content_hash": file_hash,
                "count": len(hashed_rows),
                "rows": sorted(hashed_rows, key=lambda r: r["id"]),
            })

    duplicate_groups.sort(key=lambda group: group["count"], reverse=True)
    return duplicate_groups


@_marks_content_dirty
def remove_missing_image_rows(image_ids):
    """Delete the given image rows (and their mappings, via ON DELETE CASCADE).

    Callers are responsible for having already re-verified each id is still
    actually missing on disk right before calling this -- this function just
    performs the delete. Returns (images_removed, mappings_removed).
    """
    ids = sorted({int(image_id) for image_id in (image_ids or [])})
    if not ids:
        return 0, 0

    placeholders = ",".join("?" for _ in ids)
    try:
        with get_connection() as conn:
            mapping_row = conn.execute(
                f"SELECT COUNT(*) AS count FROM mappings WHERE image_id IN ({placeholders})",
                ids,
            ).fetchone()
            mappings_removed = int(mapping_row["count"] if mapping_row else 0)

            cursor = conn.execute(
                f"DELETE FROM images WHERE id IN ({placeholders})",
                ids,
            )
            images_removed = cursor.rowcount
        return images_removed, mappings_removed
    except Exception:
        logger.exception("remove_missing_image_rows failed for ids=%s", ids)
        raise


def authenticate_user(username, access_code):
    username_value = str(username or "").strip()
    access_code_value = str(access_code or "").strip()
    if not username_value or not access_code_value:
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, username, role, is_active
            FROM users
            WHERE LOWER(username) = LOWER(?) AND access_code = ?
            LIMIT 1
            """,
            (username_value, access_code_value),
        ).fetchone()
    return _row_to_dict(row)


def update_last_login(user_id):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), int(user_id)),
        )


def _utc_now_naive_iso():
    # Naive-UTC ISO ("2026-10-09T12:00:00"): the convention static/shared.js's
    # formatIST() expects, and independent of the server's local clock.
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def touch_last_active(user_id):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_active_at = ? WHERE id = ?",
            (_utc_now_naive_iso(), int(user_id)),
        )


def add_user_activity(user_id, action, detail=None):
    """Returns the new row id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (?, ?, ?, ?)",
            (int(user_id), _utc_now_naive_iso(), str(action)[:40], (str(detail)[:120] if detail else None)),
        )
        return int(cursor.lastrowid)


def replace_user_activity(row_id, user_id, detail):
    """Refreshes a just-written row (search type-ahead collapsing). Scoped by
    user_id too so it can never touch another user's row."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE user_activity SET at_utc = ?, detail = ? WHERE id = ? AND user_id = ?",
            (_utc_now_naive_iso(), (str(detail)[:120] if detail else None), int(row_id), int(user_id)),
        )
        return cursor.rowcount > 0


def get_recent_user_activity(user_id, limit=50):
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id, username, role, last_active_at FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if not user:
            return None
        rows = conn.execute(
            "SELECT at_utc, action, detail FROM user_activity WHERE user_id = ? "
            "ORDER BY at_utc DESC, id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    return {"user": _row_to_dict(user), "rows": [_row_to_dict(r) for r in rows]}


ACTIVITY_VISIT_GAP_MINUTES = 30   # THE visit rule: a gap this long between a user's actions starts a new visit
IST_SQL = "+330 minutes"


def visit_starts_sql(where_sql, group_by_user=False):
    """SQL (aggregated, parameterised) counting visit STARTS per IST day (and
    per user when group_by_user). Parameters, in order: lookback_utc,
    <where_sql args>, count_from_utc, gap_seconds. Rows from lookback_utc
    (= count_from_utc minus the gap) are read only so the first action in the
    range knows whether it continues an earlier visit; only starts at or after
    count_from_utc are counted. Same rule as split_into_visit_groups() in
    app.py (gap >= N minutes starts a new visit); tests keep them equal."""
    sel_user = "user_id, " if group_by_user else ""
    grp = "user_id, d" if group_by_user else "d"
    return (
        "WITH s AS (SELECT user_id, at_utc, LAG(at_utc) OVER (PARTITION BY user_id ORDER BY at_utc, id) AS prev "
        f"FROM user_activity WHERE at_utc >= ? AND {where_sql}) "
        f"SELECT {sel_user}date(at_utc, '{IST_SQL}') AS d, COUNT(*) AS n FROM s "
        "WHERE at_utc >= ? AND (prev IS NULL OR "
        "CAST(strftime('%s', at_utc) AS INTEGER) - CAST(strftime('%s', prev) AS INTEGER) >= ?) "
        f"GROUP BY {grp}"
    )


def get_activity_stats(start_utc, user_id=None, end_utc=None):
    """Read-only aggregates over user_activity rows with start_utc <= at_utc
    (< end_utc when given), naive-UTC ISO. user_id=None means all dealers
    (role='customer'); else that one user. Bucketing is done in SQL in IST
    (+330 minutes), so a day boundary is IST midnight. Everything is
    aggregated in SQL (visits too, with a window function) so memory stays
    flat however many rows are in range; detail text only ever leaves here as
    the top-10 cars/searches."""
    ist = IST_SQL
    if user_id is None:
        scope_sql, scope_args = "u.role = 'customer'", ()
        act_scope, act_args = "user_id IN (SELECT id FROM users WHERE role = 'customer')", ()
    else:
        scope_sql, scope_args = "u.id = ?", (int(user_id),)
        act_scope, act_args = "user_id = ?", (int(user_id),)
    base = f"at_utc >= ? AND {act_scope}"
    base_args = (start_utc,) + act_args
    if end_utc is not None:
        base += " AND at_utc < ?"
        base_args += (end_utc,)
    lookback = (datetime.fromisoformat(start_utc) - timedelta(minutes=ACTIVITY_VISIT_GAP_MINUTES)).isoformat(timespec="seconds")
    visit_where = act_scope + (" AND at_utc < ?" if end_utc is not None else "")
    visit_args = (lookback,) + act_args + ((end_utc,) if end_utc is not None else ()) + (start_utc, ACTIVITY_VISIT_GAP_MINUTES * 60)
    with get_connection() as conn:
        daily = conn.execute(
            f"SELECT date(at_utc, '{ist}') AS d, COUNT(*) AS actions, COUNT(DISTINCT user_id) AS users "
            f"FROM user_activity WHERE {base} GROUP BY d", base_args,
        ).fetchall()
        hours = conn.execute(
            f"SELECT CAST(strftime('%H', at_utc, '{ist}') AS INTEGER) AS h, COUNT(*) AS n "
            f"FROM user_activity WHERE {base} GROUP BY h", base_args,
        ).fetchall()
        per_user = conn.execute(
            "SELECT u.id AS user_id, u.username, a.action, COUNT(a.id) AS n "
            "FROM users u LEFT JOIN user_activity a ON a.user_id = u.id AND a.at_utc >= ? "
            + ("AND a.at_utc < ? " if end_utc is not None else "")
            + f"WHERE {scope_sql} GROUP BY u.id, a.action",
            (start_utc,) + ((end_utc,) if end_utc is not None else ()) + scope_args,
        ).fetchall()
        cars = conn.execute(
            f"SELECT detail, COUNT(*) AS n FROM user_activity WHERE {base} AND action = 'view_car' "
            "AND detail IS NOT NULL AND detail != '' GROUP BY detail ORDER BY n DESC, detail LIMIT 10", base_args,
        ).fetchall()
        searches = conn.execute(
            f"SELECT LOWER(TRIM(detail)) AS q, COUNT(*) AS n FROM user_activity WHERE {base} AND action = 'search' "
            "AND detail IS NOT NULL AND TRIM(detail) != '' GROUP BY q ORDER BY n DESC, q LIMIT 10", base_args,
        ).fetchall()
        visits = conn.execute(visit_starts_sql(visit_where), visit_args).fetchall()
    return {
        "daily": [tuple(r) for r in daily],
        "hours": [tuple(r) for r in hours],
        "per_user": [tuple(r) for r in per_user],
        "cars": [tuple(r) for r in cars],
        "searches": [tuple(r) for r in searches],
        "visits": [tuple(r) for r in visits],
    }


def user_exists(user_id):
    with get_connection() as conn:
        return conn.execute("SELECT 1 FROM users WHERE id = ?", (int(user_id),)).fetchone() is not None


def get_user_by_id(user_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    return _row_to_dict(row)


@_marks_accounts_dirty
def create_customer_user(username, access_code):
    username_value = str(username or "").strip()
    access_code_value = str(access_code or "").strip()
    if not username_value:
        raise ValueError("username is required")
    if not access_code_value:
        raise ValueError("access code is required")
    if len(username_value) > 100:
        raise ValueError("username is too long")
    if len(access_code_value) > 100:
        raise ValueError("access code is too long")

    try:
        with get_connection() as conn:
            created_at = datetime.now().isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                INSERT INTO users (username, access_code, role, created_at)
                VALUES (?, ?, 'customer', ?)
                """,
                (username_value, access_code_value, created_at),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("username already exists") from exc

    return get_user_by_id(user_id)


def log_account_action(user_id, action, performed_by):
    action_value = str(action or "").strip().lower()
    if not action_value:
        return
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO account_logs (user_id, action, performed_by, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(user_id) if user_id is not None else None,
                action_value,
                int(performed_by) if performed_by is not None else None,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def get_all_customers_with_details():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, username, access_code, role, is_active, created_at, last_login, last_active_at
            FROM users
            WHERE role = 'customer'
            ORDER BY datetime(COALESCE(created_at, '1970-01-01T00:00:00')) DESC, id DESC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


@_marks_accounts_dirty
def toggle_customer_active_status(user_id):
    user_id_value = int(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE id = ?",
            (user_id_value,),
        ).fetchone()
        user = _row_to_dict(row)
        if not user:
            raise ValueError("user not found")
        if user.get("role") != "customer":
            raise ValueError("only customer accounts can be paused or resumed")

        new_status = 0 if user.get("is_active") else 1
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id_value))
        user["is_active"] = new_status
    return user


@_marks_accounts_dirty
def set_all_customer_active_status(is_active):
    status_value = 1 if is_active else 0
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE users SET is_active = ? WHERE role = 'customer'",
            (status_value,),
        )
    return cursor.rowcount


@_marks_accounts_dirty
def delete_customer_user(user_id):
    user_id_value = int(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE id = ?",
            (user_id_value,),
        ).fetchone()
        user = _row_to_dict(row)
        if not user:
            raise ValueError("user not found")
        if user.get("role") != "customer":
            raise ValueError("only customer accounts can be deleted")

        conn.execute("DELETE FROM users WHERE id = ?", (user_id_value,))
    return user


def update_access_code(user_id, new_access_code):
    """Set a new access_code for any user, admin or dealer. Used both for
    an admin resetting a dealer's forgotten code (Manage Accounts) and for
    self-service "change my password" (any logged-in role). No role
    restriction here -- callers that must not touch certain accounts
    (e.g. a dealer-only reset action) check role themselves before calling
    this, same as toggle_customer_active_status/delete_customer_user do."""
    user_id_value = int(user_id)
    access_code_value = str(new_access_code or "").strip()
    if not access_code_value:
        raise ValueError("access code is required")
    if len(access_code_value) > 100:
        raise ValueError("access code is too long")

    with get_connection() as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id_value,)).fetchone()
        user = _row_to_dict(row)
        if not user:
            raise ValueError("user not found")
        conn.execute("UPDATE users SET access_code = ? WHERE id = ?", (access_code_value, user_id_value))
    return user


def get_accounts_password():
    with get_connection() as conn:
        row = conn.execute("SELECT accounts_password FROM app_settings WHERE id = 1").fetchone()
    return (row["accounts_password"] if row else "") or ""


def get_accounts_password_version():
    """A value that changes exactly when the accounts password does (its
    updated_at timestamp, NULL/"" until the first change) -- stored in a
    session at unlock time and re-checked on every accounts_access_required
    request (see app.py) so changing the password re-locks every session
    that had already unlocked it, not just new ones going forward."""
    with get_connection() as conn:
        row = conn.execute("SELECT updated_at FROM app_settings WHERE id = 1").fetchone()
    return (row["updated_at"] if row else "") or ""


def set_accounts_password(new_password):
    password_value = str(new_password or "").strip()
    if not password_value:
        raise ValueError("password is required")
    if len(password_value) > 200:
        raise ValueError("password is too long")
    with get_connection() as conn:
        conn.execute(
            "UPDATE app_settings SET accounts_password = ?, updated_at = ? WHERE id = 1",
            (password_value, datetime.now().isoformat(timespec="seconds")),
        )


