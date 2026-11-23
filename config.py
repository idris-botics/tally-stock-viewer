import os
from datetime import timedelta

try:
    # .env is written by first_time_setup.bat but nothing previously loaded
    # it into the process environment — os.environ.get() below only ever
    # saw real OS env vars. Load it here so .env actually takes effect.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    # Flask
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "tally-stock-viewer-dev-secret")
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Session and cookies
    SESSION_TIMEOUT_HOURS = int(os.environ.get("SESSION_TIMEOUT_HOURS", "8"))
    PERMANENT_SESSION_LIFETIME = timedelta(hours=SESSION_TIMEOUT_HOURS)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

    # Database
    DB_PATH = os.environ.get("DB_PATH", "data/mappings.db")
    DB_TIMEOUT = int(os.environ.get("DB_TIMEOUT", "30"))
    SQLITE_CACHE_KB = int(os.environ.get("SQLITE_CACHE_KB", "4096"))

    # Tally
    TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
    TALLY_TIMEOUT = int(os.environ.get("TALLY_TIMEOUT", "30"))
    TALLY_RETRY_ATTEMPTS = int(os.environ.get("TALLY_RETRY_ATTEMPTS", "3"))
    TALLY_EXPORT_INTERVAL = int(os.environ.get("TALLY_EXPORT_INTERVAL", "180"))

    # Files
    MAX_IMAGE_SIZE = int(os.environ.get("MAX_IMAGE_SIZE", str(10 * 1024 * 1024)))
    ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif"}

    # Cache
    MAX_IMAGE_RESPONSE_LIMIT = int(os.environ.get("MAX_IMAGE_RESPONSE_LIMIT", "500"))
    INITIAL_IMAGE_SCAN = os.environ.get("INITIAL_IMAGE_SCAN", "1").strip().lower() not in ("0", "false")

    # System panel (remote deployment/admin panel) — deliberately has no
    # default. If unset, the entire /admin/system* panel stays disabled.
    SYSTEM_ACCESS_TOKEN = os.environ.get("SYSTEM_ACCESS_TOKEN", "").strip()

    # Accounts panel secondary password gate — deliberately has no default.
    # If unset, Manage Accounts stays locked (same "not configured" pattern
    # as SYSTEM_ACCESS_TOKEN above) rather than silently allowing access.
    ACCOUNTS_ACCESS_PASSWORD = os.environ.get("ACCOUNTS_ACCESS_PASSWORD", "").strip()

    # Cloud backup (Google Drive) — OAuth installed-app flow, authenticating
    # as an actual Google account rather than a Service Account (a Service
    # Account has zero personal Drive storage quota of its own: it can
    # create folders but every real file upload fails with a "Service
    # Accounts do not have storage quota" 403 — see build_drive_service()
    # in cloud_backup.py). One-time setup: create an OAuth Client ID
    # (Desktop app) in the Google Cloud project, download its JSON to
    # GDRIVE_OAUTH_CLIENT_SECRETS_PATH, then run a sync once — a browser
    # opens for a one-time consent step and the resulting token (with
    # refresh token) is saved to GDRIVE_OAUTH_TOKEN_PATH. Every run after
    # that refreshes silently, no browser needed, so scheduled/unattended
    # runs work too. Deliberately has no default for any of these. If any
    # is missing, cloud_backup.py cleanly no-ops (logs once, never
    # schedules the job, never crashes) — same fallback discipline as
    # SYSTEM_ACCESS_TOKEN/ACCOUNTS_ACCESS_PASSWORD above.
    GDRIVE_OAUTH_CLIENT_SECRETS_PATH = os.environ.get("GDRIVE_OAUTH_CLIENT_SECRETS_PATH", "").strip()
    GDRIVE_OAUTH_TOKEN_PATH = os.environ.get("GDRIVE_OAUTH_TOKEN_PATH", "").strip()
    GDRIVE_BACKUP_FOLDER_ID = os.environ.get("GDRIVE_BACKUP_FOLDER_ID", "").strip()
    # Fixed daily backup slot, "HH:MM" in IST (fixed UTC+5:30 offset, no tz
    # database -- see cloud_backup.py's scheduling section). This is now
    # the normal schedule; CLOUD_BACKUP_INTERVAL below is only a fallback,
    # used if this is unset or not parseable as HH:MM.
    CLOUD_BACKUP_DAILY_TIME = os.environ.get("CLOUD_BACKUP_DAILY_TIME", "02:00").strip()
    # Cloud backup mass-deletion guard: a run that would move more than this many
    # files to Drive Trash holds ALL deletions back (see cloud_backup.run_sync).
    # Unset = max(20, 2% of backed-up files). Set it (e.g. 1000) for a run where
    # you really did remove that many local files, then unset it again.
    try:
        BACKUP_MAX_DELETIONS = int(os.environ["BACKUP_MAX_DELETIONS"]) if os.environ.get("BACKUP_MAX_DELETIONS", "").strip() else None
    except ValueError:
        BACKUP_MAX_DELETIONS = None
    CLOUD_BACKUP_INTERVAL = int(os.environ.get("CLOUD_BACKUP_INTERVAL", str(3 * 24 * 3600)))

    # Cloud deployment — a cloud-hosted instance can never reach Tally
    # directly, so it receives data PUSHED to it from the office PC instead
    # of fetching it itself. Defaults to off (False), so this changes
    # nothing for the existing office PC setup unless deliberately enabled.
    #   - On the cloud instance: set DISABLE_TALLY_SCHEDULING=1 so it never
    #     starts the item-stock/full-refresh scheduling that would otherwise
    #     retry against an unreachable Tally every cycle, and set
    #     INTAKE_SYNC_TOKEN to a long random secret the office PC will send
    #     to prove a push is legitimate (deliberately separate from
    #     SYSTEM_ACCESS_TOKEN -- this endpoint is called by a background
    #     job, not a paired browser, and has no session of its own).
    #   - On the office PC: set CLOUD_SYNC_URL to the cloud instance's
    #     intake endpoint and CLOUD_SYNC_TOKEN to that same secret. If
    #     either is missing, the push is a clean no-op and the existing
    #     local scheduled jobs behave exactly as they do today.
    DISABLE_TALLY_SCHEDULING = os.environ.get("DISABLE_TALLY_SCHEDULING", "0").strip().lower() in ("1", "true", "yes")
    INTAKE_SYNC_TOKEN = os.environ.get("INTAKE_SYNC_TOKEN", "").strip()
    CLOUD_SYNC_URL = os.environ.get("CLOUD_SYNC_URL", "").strip()
    CLOUD_SYNC_TOKEN = os.environ.get("CLOUD_SYNC_TOKEN", "").strip()
    # A feeder push that would cut the number of cars or stock rows by more than this percentage is refused
    # (HTTP 422) so a Tally hiccup cannot blank the catalogue. Not checked while fewer than 20 are served.
    INTAKE_MAX_DROP_PERCENT = int(os.environ.get("INTAKE_MAX_DROP_PERCENT", "50"))

    # Rescan trigger -- lets a machine pushing new images straight onto
    # Render's disk over SCP (scripts/push_new_images.py) kick off the same
    # rescan the Training Mode "Rescan Images" button runs, without a
    # session or admin login. Deliberately separate from SYSTEM_ACCESS_TOKEN
    # (that one pairs a browser; this one authenticates a background script
    # on another machine) and from INTAKE_SYNC_TOKEN (different caller,
    # different endpoint). Same "missing = 403, feature cleanly disabled"
    # pattern as every other optional secret above.
    RESCAN_TRIGGER_TOKEN = os.environ.get("RESCAN_TRIGGER_TOKEN", "").strip()

    # Logging
    LOG_DIR = os.environ.get("LOG_DIR", "logs")
    LOG_FILE = os.environ.get("LOG_FILE", "logs/app.log")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

    # Resource monitor (see log_resource_usage()/start_resource_monitor() in
    # app.py) -- added after a production OOM/fd-exhaustion investigation so
    # the NEXT occurrence has an actual RSS/open-file trend in app.log
    # leading up to it, instead of only the crash itself. MEMORY_LIMIT_MB
    # should match the real deployment's container memory limit (e.g.
    # Render's 512Mi plan -> 512); it's display-only, not enforced.
    RESOURCE_LOG_INTERVAL_SECONDS = int(os.environ.get("RESOURCE_LOG_INTERVAL_SECONDS", "300"))
    MEMORY_LIMIT_MB = int(os.environ.get("MEMORY_LIMIT_MB", "512"))
