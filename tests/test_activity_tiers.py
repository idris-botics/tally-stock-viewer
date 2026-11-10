"""Summaries (activity_daily / activity_hourly / activity_top_monthly): idempotent backfill, IST day
boundary, empty days, deleted dealers, resumable backfill, pruning refusing without a summary, and the
summary-based totals agreeing with the raw-based ones. Synthetic data in a scratch database only."""
import os
import random
import unittest
from datetime import datetime, timedelta

from tests import support
from tests.activity_fixtures import ScratchDb, make_users, insert_rows, synthetic_rows, count_rows, dump_summaries

import activity_archive as arch
import activity_tiers as tiers
import database as db

NOW = datetime(2027, 2, 10, 12, 0, 0)   # fixed "now" (naive UTC) for every test here


class TierCase(unittest.TestCase):
    def setUp(self):
        self.scratch = ScratchDb().start()
        self.admin, self.dealers = make_users(6)

    def tearDown(self):
        self.scratch.stop()

    def seed(self, days=40, per_day=30, seed=3):
        first = NOW - timedelta(days=days)
        rows = synthetic_rows(first, days, self.dealers, per_day=per_day, seed=seed, admin_id=self.admin)
        rows = [r for r in rows if r[1] < NOW.isoformat(timespec="seconds")]
        insert_rows(rows)
        return rows


class Summaries(TierCase):
    def test_ist_day_boundary_18_29_vs_18_31_utc(self):
        d = self.dealers[0]
        insert_rows([(d, "2027-01-05T18:29:00", "view_car", "A"), (d, "2027-01-05T18:31:00", "view_car", "B")])
        tiers.ensure_summaries(NOW)
        with db.get_connection() as conn:
            rows = {r[0]: r[1] for r in conn.execute("SELECT day_ist, SUM(count) FROM activity_daily WHERE action != '' GROUP BY day_ist")}
            hours = {(r[0], r[1]): r[2] for r in conn.execute("SELECT * FROM activity_hourly")}
        self.assertEqual(rows, {"2027-01-05": 1, "2027-01-06": 1})        # 18:29 UTC = 23:59 IST, 18:31 UTC = 00:01 next IST day
        self.assertEqual(hours, {("2027-01-05", 23): 1, ("2027-01-06", 0): 1})

    def test_backfill_is_idempotent_and_never_touches_today(self):
        self.seed()
        first = tiers.ensure_summaries(NOW)
        self.assertGreater(first["days"], 30)
        snapshot = dump_summaries()
        again = tiers.ensure_summaries(NOW)
        self.assertEqual(again["days"], 0)
        self.assertEqual(dump_summaries(), snapshot, "a second backfill must change nothing")
        today = tiers.ist_today(NOW).isoformat()
        self.assertNotIn("d:" + today, [k for k in snapshot["activity_summary_done"]], "today is not over yet")
        self.assertFalse(tiers.compute_day(today, NOW))

    def test_quick_mode_is_a_no_op_when_yesterday_is_done_and_catches_up_when_not(self):
        self.seed(days=12)
        self.assertEqual(tiers.ensure_summaries(NOW, quick=True)["months"], 0, "quick mode leaves the monthly tops alone")
        self.assertEqual(dump_summaries()["activity_top_monthly"], [])
        self.assertEqual(tiers.ensure_summaries(NOW, quick=True), {"days": 0, "months": 0})
        with db.get_connection() as conn:
            conn.execute("DELETE FROM activity_summary_done WHERE key = ?", ("d:" + (tiers.ist_today(NOW) - timedelta(days=1)).isoformat(),))
            conn.execute("DELETE FROM activity_daily WHERE day_ist = ?", ((tiers.ist_today(NOW) - timedelta(days=1)).isoformat(),))
        self.assertEqual(tiers.ensure_summaries(NOW, quick=True)["days"], 1)

    def test_a_day_with_no_activity_gets_a_marker_and_no_rows(self):
        d = self.dealers[0]
        insert_rows([(d, "2027-01-02T05:00:00", "view_car", "A"), (d, "2027-01-06T05:00:00", "view_car", "B")])
        tiers.ensure_summaries(NOW)
        s = dump_summaries()
        keys = set(s["activity_summary_done"])
        for quiet in ("2027-01-03", "2027-01-04", "2027-01-05"):
            self.assertIn("d:" + quiet, keys)
            self.assertFalse([r for r in s["activity_daily"] if r[0] == quiet])

    def test_existing_summary_is_never_recomputed_after_raw_rows_are_gone(self):
        self.seed(days=10)
        tiers.ensure_summaries(NOW)
        before = dump_summaries()
        with db.get_connection() as conn:
            conn.execute("DELETE FROM user_activity")
        tiers.ensure_summaries(NOW)
        self.assertEqual(dump_summaries(), before)

    def test_deleted_dealer_stays_in_the_summaries_and_is_labelled(self):
        gone = self.dealers[0]
        insert_rows([(gone, "2027-01-03T05:00:00", "view_car", "A"), (self.dealers[1], "2027-01-03T06:00:00", "view_car", "B")])
        tiers.ensure_summaries(NOW)
        db.delete_customer_user(gone)
        self.assertEqual(count_rows("user_id = ?", (gone,)), 0, "raw rows go with the account")
        agg = tiers.get_summary_stats("2027-01-01", "2027-01-31")
        self.assertEqual(sum(a for _d, a, _u, _v in agg["daily"]), 2)
        names = {r[1] for r in agg["per_user"] if r[2]}
        self.assertIn(f"(deleted dealer #{gone})", names)

    def test_interrupted_backfill_resumes_and_matches_an_uninterrupted_one(self):
        self.seed(days=20)
        real = tiers.compute_day
        calls = {"n": 0}

        def flaky(day, now_utc=None):
            calls["n"] += 1
            if calls["n"] == 6:
                raise RuntimeError("interrupted")
            return real(day, now_utc)

        tiers.compute_day = flaky
        try:
            with self.assertRaises(RuntimeError):
                tiers.ensure_summaries(NOW)
        finally:
            tiers.compute_day = real
        partial = dump_summaries()
        self.assertEqual(len([k for k in partial["activity_summary_done"] if k.startswith("d:")]), 5)
        tiers.ensure_summaries(NOW)
        resumed = dump_summaries()
        with db.get_connection() as conn:
            conn.execute("DELETE FROM activity_daily"); conn.execute("DELETE FROM activity_hourly")
            conn.execute("DELETE FROM activity_top_monthly"); conn.execute("DELETE FROM activity_summary_done")
        tiers.ensure_summaries(NOW)
        self.assertEqual(dump_summaries(), resumed)

    def test_pruning_refuses_without_a_summary(self):
        self.seed(days=100)
        os.environ["ACTIVITY_RETENTION_DAYS"] = "30"
        before = count_rows()
        with self.assertRaises(arch.SummaryMissing):
            arch.prune_month("2026-12", count_rows("at_utc >= ? AND at_utc < ?", tiers.month_bounds_utc("2026-12")))
        self.assertEqual(count_rows(), before)
        res = arch.run_archive(service_factory=lambda: (_ for _ in ()).throw(RuntimeError("no drive")), now_utc=NOW)
        self.assertEqual(res["rows_pruned"], 0)
        self.assertEqual(count_rows(), before, "no raw row may go before the Drive upload is verified")


class SummariesAgreeWithRaw(TierCase):
    def test_totals_match_on_overlapping_days(self):
        self.seed(days=45, per_day=35, seed=11)
        tiers.ensure_summaries(NOW)
        today = tiers.ist_today(NOW)
        first = today - timedelta(days=40)
        last = today - timedelta(days=1)
        start_utc, _ = tiers.day_bounds_utc(first)
        _, end_utc = tiers.day_bounds_utc(last)
        raw = db.get_activity_stats(start_utc, None, end_utc=end_utc)
        summ = tiers.get_summary_stats(first.isoformat(), last.isoformat(), None)
        self.assertEqual(sorted(raw["daily"]), sorted((d, a, u) for d, a, u, _v in summ["daily"]))
        self.assertEqual(dict(raw["visits"]), {d: v for d, _a, _u, v in summ["daily"] if v})
        self.assertEqual(sorted(raw["hours"]), sorted(summ["hours"]))
        norm = lambda rows: sorted((i, a, n) for i, _name, a, n in rows if a)
        self.assertEqual(norm(raw["per_user"]), norm(summ["per_user"]))
        # Top lists are kept per calendar month, so they agree on whole months.
        jan_raw = db.get_activity_stats(tiers.day_bounds_utc("2027-01-01")[0], None, end_utc=tiers.day_bounds_utc("2027-01-31")[1])
        jan_sum = tiers.get_summary_stats("2027-01-01", "2027-01-31", None)
        self.assertEqual(jan_raw["cars"], jan_sum["cars"])
        self.assertEqual(jan_raw["searches"], jan_sum["searches"])

    def test_single_dealer_matches_too(self):
        self.seed(days=30, seed=5)
        tiers.ensure_summaries(NOW)
        today = tiers.ist_today(NOW)
        first, last = today - timedelta(days=25), today - timedelta(days=1)
        start_utc, _ = tiers.day_bounds_utc(first)
        _, end_utc = tiers.day_bounds_utc(last)
        uid = self.dealers[2]
        raw = db.get_activity_stats(start_utc, uid, end_utc=end_utc)
        summ = tiers.get_summary_stats(first.isoformat(), last.isoformat(), uid)
        self.assertEqual(sorted(raw["daily"]), sorted((d, a, u) for d, a, u, _v in summ["daily"]))
        self.assertEqual(dict(raw["visits"]), {d: v for d, _a, _u, v in summ["daily"] if v})
        self.assertIsNone(summ["hours"], "hourly is not stored per dealer")

    def test_sql_visit_rule_equals_the_shared_python_splitter(self):
        import app as app_module
        rnd = random.Random(2)
        uid = self.dealers[0]
        t = datetime(2027, 1, 1, 3, 0, 0)
        rows = []
        for _ in range(400):
            t += timedelta(minutes=rnd.choice([0, 1, 10, 29, 30, 31, 90, 600]), seconds=rnd.choice([0, 0, 1]))
            rows.append((uid, t.isoformat(timespec="seconds"), "view_car", "X"))
        insert_rows(rows)
        stamps = [{"at_utc": r[1]} for r in reversed(rows)]
        groups = app_module.split_into_visit_groups(stamps, lambda r: r["at_utc"])
        want = {}
        for g in groups:
            day = (datetime.fromisoformat(g[0]["at_utc"]) + tiers.IST_OFFSET).date().isoformat()
            want[day] = want.get(day, 0) + 1
        start = "2027-01-01T00:00:00"
        got = dict(db.get_activity_stats(start, uid)["visits"])
        self.assertEqual(got, want)


class Endpoint(unittest.TestCase):
    """The route: clamps, the new range keys, the dealer filter -- on the shared fixture database."""

    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)
        self.app_module._usage_cache_clear()
        self.admin = support.client_for("admin_unlocked")

    def test_days_clamped_to_the_raw_window_and_default_is_30(self):
        self.assertEqual(self.admin.get("/admin/user_activity_stats?days=999").get_json()["days"], 90)
        self.assertEqual(self.admin.get("/admin/user_activity_stats?days=0").get_json()["days"], 1)
        self.assertEqual(self.admin.get("/admin/user_activity_stats?days=60").get_json()["days"], 60)
        self.assertEqual(self.admin.get("/admin/user_activity_stats").get_json()["days"], 30)
        self.assertEqual(self.admin.get("/admin/user_activity_stats?days=abc").get_json()["days"], 30)

    def test_repeated_requests_are_served_from_a_short_bounded_cache(self):
        calls = []
        real = self.app_module._usage_from_raw
        self.app_module._usage_from_raw = lambda days, uid: (calls.append(days), real(days, uid))[1]
        try:
            for _ in range(5):
                self.assertEqual(self.admin.get("/admin/user_activity_stats?days=45").status_code, 200)
            self.assertEqual(calls, [45], "the heavy query ran once for five identical requests")
            for d in range(1, 91):                       # many different views never grow the cache past its bound
                self.admin.get(f"/admin/user_activity_stats?days={d}")
            self.assertLessEqual(len(self.app_module._usage_cache), self.app_module.USAGE_CACHE_MAX_ENTRIES)
            self.app_module._usage_cache_clear()
            self.admin.get("/admin/user_activity_stats?days=45")
            self.assertEqual(calls.count(45), 2)
        finally:
            self.app_module._usage_from_raw = real

    def test_long_ranges_use_summaries_and_say_what_is_reduced(self):
        for key in ("6m", "1y", "all"):
            r = self.admin.get(f"/admin/user_activity_stats?range={key}")
            self.assertEqual(r.status_code, 200, key)
            body = r.get_json()
            self.assertEqual((body["source"], body["range"]), ("summary", key))
            self.assertIn("visit_detail", body["reduced"])
            self.assertTrue(body["note"])
        self.assertEqual(self.admin.get("/admin/user_activity_stats?range=2y").status_code, 400)

    def test_dealer_filter_on_a_long_range(self):
        uid = support._state["users"]["dealer"]["id"]
        body = self.admin.get(f"/admin/user_activity_stats?range=1y&user_id={uid}").get_json()
        self.assertEqual(body["source"], "summary")
        self.assertIsNone(body["hourly"])
        self.assertIn("busiest_hours", body["reduced"])
        self.assertEqual(self.admin.get("/admin/user_activity_stats?range=1y&user_id=99999").status_code, 400)

    def test_raw_and_summary_routes_agree_on_a_finished_range(self):
        uid = support._state["users"]["dealer"]["id"]
        now = datetime.utcnow()
        rows = synthetic_rows(now - timedelta(days=140), 138, [uid], per_day=8, seed=9)
        insert_rows([r for r in rows if r[1] < (now - timedelta(days=2)).isoformat(timespec="seconds")])
        tiers.ensure_summaries()
        raw = self.admin.get("/admin/user_activity_stats?days=90").get_json()
        summ = self.admin.get("/admin/user_activity_stats?range=6m").get_json()
        self.assertEqual(summ["granularity"], "week")
        # 6 months is shown weekly: add the raw days into Monday buckets and compare finished weeks
        weeks = {}
        for d in raw["daily"]:
            day = datetime.strptime(d["date"], "%Y-%m-%d").date()
            monday = (day - timedelta(days=day.weekday())).isoformat()
            w = weeks.setdefault(monday, [0, 0])
            w[0] += d["actions"]; w[1] += d["visits"]
        checked = 0
        for d in summ["daily"][:-1]:             # the last bucket is the running week
            if d["date"] in weeks and d["date"] >= raw["daily"][0]["date"] and d["actions"]:
                self.assertEqual((d["actions"], d["visits"]), tuple(weeks[d["date"]]), d["date"])
                checked += 1
        self.assertGreaterEqual(checked, 3)
        self.app_module.ACTIVITY_WEEKLY_ABOVE_DAYS = 100000          # ask for one bar per day for the day-by-day comparison
        self.app_module._usage_cache_clear()
        try:
            one_year_days = self.admin.get("/admin/user_activity_stats?range=all").get_json()
        finally:
            self.app_module.ACTIVITY_WEEKLY_ABOVE_DAYS = 120
        self.assertEqual(one_year_days["granularity"], "day")
        daily = {d["date"]: d for d in one_year_days["daily"]}
        for d in raw["daily"][:-1]:
            if d["date"] in daily:
                self.assertEqual((d["actions"], d["visits"], d["active_users"]),
                                 (daily[d["date"]]["actions"], daily[d["date"]]["visits"], daily[d["date"]]["active_users"]), d["date"])


if __name__ == "__main__":
    unittest.main()
