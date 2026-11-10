"""Usage Charts extras: weekday x hour heatmap, Sunday vs other days, new vs returning, dormant dealers,
previous-period comparison, admin separation -- all from SYNTHETIC data in a scratch database."""
import json
import unittest
from datetime import date, datetime, timedelta

from tests import support
from tests.activity_fixtures import ScratchDb, make_users, insert_rows, synthetic_rows

import activity_insights as ins
import activity_tiers as tiers
import database as db

NOW = datetime(2027, 2, 10, 12, 0, 0)       # Wednesday 17:30 IST


class InsightsCase(unittest.TestCase):
    def setUp(self):
        self.scratch = ScratchDb().start()
        self.admin, ds = make_users(6)
        self.A, self.B, self.C, self.D, self.E, self.F = ds
        rows = []
        for t in ("2027-01-04T10:00:00", "2027-01-04T10:01:00", "2027-01-04T10:02:00"):   # Mon 15:30 IST, one visit
            rows.append((self.A, t, "view_car", "X"))
        rows += [(self.A, "2027-01-10T05:00:00", "view_car", "X"), (self.A, "2027-01-10T05:01:00", "search", "q")]   # Sunday 10:30 IST
        rows += [(self.A, "2027-01-12T08:00:00", "view_car", "X")]
        rows += [(self.B, f"2027-01-16T0{h}:00:00", "view_car", "Y") for h in (3, 4, 5, 6)]    # Saturday, four separate visits
        rows += [(self.C, "2027-01-20T05:00:00", "view_car", "Z")]                            # 21 days before "today"
        rows += [(self.D, "2027-02-05T05:00:00", "view_car", "Z")]
        rows += [(self.F, "2027-02-10T06:00:00", "view_car", "Z")]                            # today: raw only
        rows += [(self.admin, f"2027-01-05T0{h}:00:00", "view_car", "ADMIN") for h in (1, 2, 3, 4, 5)]
        insert_rows(rows)
        tiers.ensure_summaries(NOW)

    def tearDown(self):
        self.scratch.stop()

    def build(self, first="2027-01-04", last="2027-02-10", user=None, compare=True):
        return ins.build(first, last, user, compare, NOW)

    def test_sunday_versus_other_days_counts_empty_days_as_zero(self):
        out = self.build()
        sun = out["weekdays"][6]
        self.assertEqual((sun["weekday"], sun["days"], sun["actions"]), ("Sunday", 5, 2))
        self.assertEqual((sun["avg_actions"], sun["avg_dealers"]), (0.4, 0.2))
        self.assertEqual(out["weekdays"][0]["actions"], 3)           # Monday
        self.assertEqual(out["weekdays"][5]["actions"], 4)           # Saturday
        self.assertEqual(sum(w["actions"] for w in out["weekdays"]), 3 + 2 + 1 + 4 + 1 + 1)   # dealers only: no admin, no today
        self.assertEqual(out["last_day"], "2027-02-09", "finished days only")

    def test_heatmap_is_dealers_only_and_in_ist(self):
        grid = self.build()["heatmap"]
        self.assertEqual(grid[0][15], 3)                              # Monday 15:30 IST
        self.assertEqual(grid[6][10], 2)                              # Sunday 10:30 IST
        self.assertEqual(sum(map(sum, grid)), 12, "admin actions are not in the heatmap")
        self.assertEqual(self.build()["admin_actions"], 5, "admin activity is counted apart")

    def test_new_versus_returning_per_week(self):
        series = {w["week"]: (w["new"], w["returning"]) for w in self.build()["new_returning"]}
        self.assertEqual(series["2027-01-04"], (1, 0))               # A first seen
        self.assertEqual(series["2027-01-11"], (1, 1))               # B new, A back
        self.assertEqual(series["2027-01-18"], (1, 0))               # C
        self.assertEqual(series["2027-01-25"], (0, 0))
        self.assertEqual(series["2027-02-01"], (1, 0))               # D

    def test_dormant_never_active_and_active_today(self):
        d = self.build()["dormant"]
        self.assertEqual([(r["user_id"], r["days_ago"]) for r in d["dormant"]], [(self.C, 21), (self.B, 25), (self.A, 29)])
        never = {r["user_id"] for r in d["never_active"]}
        self.assertIn(self.E, never)
        self.assertFalse(never & {self.A, self.B, self.C, self.D, self.F})
        self.assertEqual(d["never_total"], len(never))
        self.assertEqual(d["recent"], 2, "D (5 days ago) and F (active today, raw rows only) are not dormant")
        self.assertEqual(d["dormant"][0]["last_active"], "2027-01-20")

    def test_single_dealer_gets_last_active_not_the_all_dealer_panels(self):
        out = self.build(user=self.C)
        self.assertIsNone(out["heatmap"]); self.assertIsNone(out["new_returning"]); self.assertIsNone(out["dormant"])
        self.assertEqual(out["last_active"], {"date": "2027-01-20", "days_ago": 21})
        self.assertEqual(sum(w["actions"] for w in out["weekdays"]), 1)
        self.assertEqual(self.build(user=self.E)["last_active"], {"date": None, "days_ago": None})

    def test_previous_period(self):
        self.assertIsNone(self.build()["previous"], "the period before tracking started has nothing to compare")
        p = self.build(first="2027-01-18", last="2027-02-10")["previous"]
        self.assertEqual((p["first_day"], p["last_day"], p["days"]), ("2026-12-25", "2027-01-17", 24))
        self.assertEqual((p["total_actions"], p["active_users"], p["visits"]), (10, 2, 7))
        self.assertEqual(p["days_with_data"], 14, "only 14 of the 24 days were tracked")
        self.assertIsNone(self.build(compare=False)["previous"])

    def test_ranges_start_where_tracking_started(self):
        out = self.build(first="2026-10-01")                       # tracking only began on 4 Jan
        self.assertEqual(out["first_day"], "2027-01-04")
        self.assertEqual(sum(w["days"] for w in out["weekdays"]), 37, "no fake zero days before tracking began")
        self.assertEqual(out["new_returning"][0]["week"], "2027-01-04")
        self.assertIsNone(out["previous"], "nothing was tracked in the period before")

    def test_deleted_dealer_still_counts_in_history(self):
        db.delete_customer_user(self.B)
        out = self.build()
        self.assertEqual(out["weekdays"][5]["actions"], 4)
        self.assertEqual(sum(map(sum, out["heatmap"])), 12)

    def test_payload_is_small_and_json(self):
        text = json.dumps(self.build())
        self.assertLess(len(text), 20000)
        self.assertNotIn("ADMIN", text, "no detail text (car names / searches) in the insights")


class InsightsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)
        self.admin = support.client_for("admin_unlocked")

    def test_every_range_carries_the_insights_block(self):
        for query in ("days=7", "days=90", "range=6m", "range=1y", "range=all"):
            body = self.admin.get("/admin/user_activity_stats?" + query).get_json()
            self.assertIn("insights", body, query)
            self.assertEqual(len(body["insights"]["weekdays"] or [0] * 7), 7)
        uid = support._state["users"]["dealer"]["id"]
        one = self.admin.get(f"/admin/user_activity_stats?range=1y&user_id={uid}").get_json()["insights"]
        self.assertIsNone(one["heatmap"])
        self.assertIn("last_active", one)

    def test_chart_days_start_at_the_first_tracked_day(self):
        uid = support._state["users"]["dealer"]["id"]
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM user_activity")
            conn.execute("DELETE FROM activity_daily"); conn.execute("DELETE FROM activity_summary_done")
        insert_rows([(uid, (datetime.utcnow() - timedelta(days=20)).isoformat(timespec="seconds"), "view_car", "X")])
        self.app_module._usage_cache_clear()
        body = self.admin.get("/admin/user_activity_stats?days=90").get_json()
        self.assertEqual(body["days"], 90)
        today = self.app_module.activity_tiers.ist_today()
        self.assertEqual(len(body["daily"]), 21, "weeks of zeros from before tracking began are not drawn")
        self.assertEqual(body["daily"][-1]["date"], today.isoformat())
        flat = self.admin.get("/admin/user_activity_stats?range=1y").get_json()
        self.assertEqual(flat["granularity"], "day", "a year range on a young system is short, so one bar per day")

    def test_an_insights_failure_hides_the_panels_but_not_the_charts(self):
        real = ins.build
        ins.build = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.app_module._usage_cache_clear()
            r = self.admin.get("/admin/user_activity_stats?days=14")
        finally:
            ins.build = real
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["insights"], {})
        self.assertIn("daily", body)


if __name__ == "__main__":
    unittest.main()
