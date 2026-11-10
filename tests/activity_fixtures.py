"""Synthetic data + a scratch database for the activity-tier tests. Never real rows, never the real data/."""
import os
import random
import shutil
import tempfile
from datetime import datetime, timedelta

from tests import support  # noqa: F401  (refuses to run outside check_all's scratch copy; sets the fake env)

import database as db
from config import Config

ACTIONS = ["view_car", "view_car", "view_car", "search", "search", "share_image", "share_longpress", "copy_image", "login", "report"]
CARS = [f"CAR {i:02d} FOOT MAT" for i in range(12)]
QUERIES = ["creta", "alto", "swift", "body", "7d mat", "baleno"]


class ScratchDb:
    """A throw-away database file. Use as a context manager or call start()/stop()."""

    def start(self):
        self.tmp = tempfile.mkdtemp(prefix="act_")
        self.path = os.path.join(self.tmp, "data", "mappings.db")
        os.makedirs(os.path.dirname(self.path))
        self.saved = (db.DB_PATH, Config.DB_PATH, os.environ.get("ACTIVITY_RETENTION_DAYS"))
        db.DB_PATH = self.path
        Config.DB_PATH = self.path
        db.init_database()
        return self

    def stop(self):
        db.DB_PATH, Config.DB_PATH, retention = self.saved
        if retention is None:
            os.environ.pop("ACTIVITY_RETENTION_DAYS", None)
        else:
            os.environ["ACTIVITY_RETENTION_DAYS"] = retention
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def make_users(n_dealers=6):
    with db.get_connection() as conn:
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
        if not admin:
            conn.execute("INSERT INTO users (username, access_code, role) VALUES ('adm', 'x', 'admin')")
            admin = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
        admin_id = admin[0]
    dealers = [db.create_customer_user(f"dealer{i}", f"code{i}")["id"] for i in range(n_dealers)]
    return admin_id, dealers


def insert_rows(rows):
    """rows: (user_id, at_utc_iso, action, detail)"""
    with db.get_connection() as conn:
        conn.executemany("INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (?, ?, ?, ?)", rows)


def detail_for(rnd, action):
    if action == "view_car":
        return rnd.choice(CARS)
    if action == "search":
        return rnd.choice(QUERIES)
    if action in ("share_longpress", "copy_image"):
        return f"{rnd.randint(1, 6)} img · {rnd.choice(CARS)}"
    return None


def synthetic_rows(first_utc, days, users, per_day=40, seed=7, admin_id=None, admin_share=0.3):
    """Deterministic rows spread over `days` days from first_utc, in bursts (so visits form) and
    deliberately including rows exactly 30 minutes apart and rows around IST midnight."""
    rnd = random.Random(seed)
    rows = []
    for d in range(days):
        day0 = first_utc + timedelta(days=d)
        for _ in range(rnd.randint(per_day // 2, per_day)):
            uid = admin_id if (admin_id and rnd.random() < admin_share) else rnd.choice(users)
            t = day0 + timedelta(seconds=rnd.randint(0, 86399))
            burst = rnd.randint(1, 4)
            for _b in range(burst):
                action = rnd.choice(ACTIONS)
                rows.append((uid, t.isoformat(timespec="seconds"), action, detail_for(rnd, action)))
                t += timedelta(minutes=rnd.choice([0, 1, 5, 29, 30, 31]), seconds=rnd.choice([0, 0, 59]))
        # around IST midnight (18:30 UTC)
        for hh, mm in ((18, 29), (18, 31)):
            uid = rnd.choice(users)
            t = day0.replace(hour=hh, minute=mm, second=0)
            rows.append((uid, t.isoformat(timespec="seconds"), "view_car", rnd.choice(CARS)))
    return rows


def count_rows(where="1=1", args=()):
    with db.get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM user_activity WHERE {where}", args).fetchone()[0]


def dump_summaries():
    with db.get_connection() as conn:
        out = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1,2,3")]
               for t in ("activity_daily", "activity_hourly", "activity_top_monthly")}
        out["activity_summary_done"] = [r[0] for r in conn.execute("SELECT key FROM activity_summary_done ORDER BY 1")]
    return out
