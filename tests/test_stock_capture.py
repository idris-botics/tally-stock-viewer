"""Stock Management, Part A: silent capture of stock changes and of car interest (nothing is shown anywhere).

Direct calls to _capture_stock_changes() cover the rules; a few tests go through the real intake route and the
real /designs and /api/search_cars routes to prove the hooks are wired and can never change a response."""
import copy
import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests import support
from tests.support import PAYLOAD, INTAKE_HEADERS

TABLES = ("stock_state", "stock_events", "capture_meta", "car_interest_daily", "search_miss_daily")


def items(count, qty=5, start=0):
    return [{"item_name": f"ITEM {i:04d}", "qty": qty} for i in range(start, start + count)]


class CaptureBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        self.clear_tables()
        self.app_module._INTEREST_RECENT.clear()
        self.app_module._SEARCH_MISS_LAST.clear()
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")
        self.admin = support.client_for("admin_locked")

    def tearDown(self):
        support.restore_state(self.snapshot)

    def clear_tables(self):
        with self.db.get_connection() as conn:
            for table in TABLES:
                conn.execute(f"DELETE FROM {table}")

    def rows(self, sql, args=()):
        with self.db.get_connection() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def state(self):
        return {k: (n, q) for k, n, q in self.rows("SELECT item_key, item_name, qty FROM stock_state")}

    def events(self):
        return self.rows("SELECT item_key, qty_before, qty_after, day_ist FROM stock_events ORDER BY id")

    def meta(self, key):
        found = self.rows("SELECT value FROM capture_meta WHERE key = ?", (key,))
        return found[0][0] if found else None

    def capture(self, stock):
        self.app_module._capture_stock_changes(stock)

    def push(self, payload):
        return self.anon.post("/admin/intake/sync_data", json=payload, headers=INTAKE_HEADERS)

    def at(self, year, month, day, hour, minute):
        return mock.patch.object(self.app_module, "_stock_now_utc",
                                 return_value=datetime(year, month, day, hour, minute, tzinfo=timezone.utc))


class StockCapture(CaptureBase):
    def test_first_push_fills_state_writes_no_events_and_sets_start(self):
        self.capture(items(3))
        self.assertEqual(len(self.state()), 3)
        self.assertEqual(self.events(), [])
        self.assertTrue(self.meta("stock_tracking_started_at_utc"))

    def test_quantity_decrease_writes_one_event(self):
        self.capture([{"item_name": "ALTO 7D", "qty": 10}, {"item_name": "SWIFT 7D", "qty": 4}])
        self.capture([{"item_name": "ALTO 7D", "qty": 7}, {"item_name": "SWIFT 7D", "qty": 4}])
        self.assertEqual([e[:3] for e in self.events()], [("ALTO 7D", 10, 7)])
        self.assertEqual(self.state()["ALTO 7D"][1], 7)

    def test_increase_is_a_restock_event(self):
        self.capture([{"item_name": "ALTO 7D", "qty": 2}])
        self.capture([{"item_name": "ALTO 7D", "qty": 12}])
        self.assertEqual([e[:3] for e in self.events()], [("ALTO 7D", 2, 12)])

    def test_item_that_disappears_is_sold_out(self):
        self.capture([{"item_name": "ALTO 7D", "qty": 3}, {"item_name": "SWIFT 7D", "qty": 4}])
        self.capture([{"item_name": "SWIFT 7D", "qty": 4}])
        self.assertEqual([e[:3] for e in self.events()], [("ALTO 7D", 3, 0)])
        self.assertNotIn("ALTO 7D", self.state())

    def test_item_that_appears_comes_from_zero(self):
        self.capture([{"item_name": "SWIFT 7D", "qty": 4}])
        self.capture([{"item_name": "SWIFT 7D", "qty": 4}, {"item_name": "ALTO 7D", "qty": 6}])
        self.assertEqual([e[:3] for e in self.events()], [("ALTO 7D", 0, 6)])

    def test_zero_and_negative_quantities_count_as_absent(self):
        self.capture([{"item_name": "A", "qty": 5}, {"item_name": "B", "qty": 0}, {"item_name": "C", "qty": -3}])
        self.assertEqual(sorted(self.state()), ["A"])
        self.capture([{"item_name": "A", "qty": 5}, {"item_name": "B", "qty": -1}, {"item_name": "C", "qty": 0}])
        self.assertEqual(self.events(), [])                       # still absent: nothing changed
        self.capture([{"item_name": "A", "qty": 0}])               # sold down to zero = gone
        self.assertEqual([e[:3] for e in self.events()], [("A", 5, 0)])

    def test_duplicate_names_follow_the_site_rule(self):
        """The site's stock readers key by the cleaned upper-case name, keep the LAST row with a quantity above 0,
        and ignore a later row that is 0 or negative. Capture must see exactly the same quantities."""
        self.capture([{"item_name": "Alto  7D", "qty": 5}, {"item_name": "ALTO 7D ", "qty": 3},
                      {"item_name": "SWIFT 7D", "qty": 4}, {"item_name": "swift 7d", "qty": 0}])
        self.assertEqual({k: v[1] for k, v in self.state().items()}, {"ALTO 7D": 3, "SWIFT 7D": 4})
        self.assertEqual(self.state()["ALTO 7D"][0], "ALTO 7D")   # the display name of the winning row, trimmed

    def test_unchanged_push_writes_nothing(self):
        self.capture(items(5))
        before = (self.rows("SELECT * FROM stock_state ORDER BY item_key"), self.rows("SELECT * FROM stock_events"))
        self.capture(items(5))
        self.assertEqual(before, (self.rows("SELECT * FROM stock_state ORDER BY item_key"), self.rows("SELECT * FROM stock_events")))

    def test_mass_vanish_is_suspicious_and_becomes_baseline_on_the_third_push(self):
        self.capture(items(200))
        before = self.rows("SELECT * FROM stock_state ORDER BY item_key")
        shrunk = items(120)                                        # 80 vanished = 40%, and 80 >= 50
        self.capture(shrunk)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.rows("SELECT * FROM stock_state ORDER BY item_key"), before)
        self.assertEqual(self.meta("suspicious_push_streak"), "1")
        self.capture(shrunk)
        self.assertEqual(self.meta("suspicious_push_streak"), "2")
        self.assertEqual(len(self.state()), 200)
        self.capture(shrunk)                                       # third consistent push: accepted, without events
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.state()), 120)
        self.assertEqual(self.meta("suspicious_push_streak"), "0")

    def test_a_normal_push_resets_the_streak(self):
        self.capture(items(200))
        self.capture(items(120))
        self.assertEqual(self.meta("suspicious_push_streak"), "1")
        self.capture(items(200))                                   # back to normal: nothing vanished
        self.assertEqual(self.meta("suspicious_push_streak"), "0")
        self.assertEqual(self.events(), [])

    def test_small_or_few_vanishings_are_normal(self):
        self.capture(items(200))
        self.capture(items(190))                                   # 10 vanished: under 50
        self.assertEqual(len(self.events()), 10)
        self.capture(items(1000))                                  # many appear, none vanish
        self.capture(items(1000, start=60))                        # 60 vanished of 1000 = 6%: under 15%
        self.assertEqual(self.meta("suspicious_push_streak"), "0")

    def test_capture_failure_never_changes_the_intake_answer_or_the_files(self):
        pushed = copy.deepcopy(PAYLOAD)
        pushed["car_master"] = ["ALTO", "SWIFT", "WAGONR"]
        pushed["main_hierarchy"] += [{"item_name": "WAGONR", "qty": 1}, {"item_name": "WAGONR 7D MAT RED", "qty": 1}]
        pushed["item_stock"] = pushed["item_stock"] + [{"item_name": "WAGONR 7D MAT RED", "qty": 1}]
        with mock.patch.object(self.app_module, "_capture_stock_changes", side_effect=RuntimeError("boom")):
            response = self.push(pushed)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        with open(os.path.join(support.data_dir(), "car_master.json"), encoding="utf-8") as handle:
            self.assertIn("WAGONR", json.load(handle))             # the push really was applied and kept

    def test_intake_pushes_are_captured(self):
        self.assertEqual(self.push(PAYLOAD).status_code, 200)      # first push: baseline only
        self.assertEqual(len(self.state()), 3)
        changed = copy.deepcopy(PAYLOAD)
        changed["item_stock"][0]["qty"] = 1
        self.assertEqual(self.push(changed).status_code, 200)
        self.assertEqual([e[:3] for e in self.events()], [("ALTO 7D MAT BLACK", 3, 1)])

    def test_refused_push_does_not_touch_the_history(self):
        self.assertEqual(self.push(PAYLOAD).status_code, 200)
        before = (self.rows("SELECT * FROM stock_state ORDER BY item_key"), self.rows("SELECT * FROM stock_events"))
        bad = copy.deepcopy(PAYLOAD); bad["item_stock"] = []
        self.assertEqual(self.push(bad).status_code, 422)
        self.assertEqual(before, (self.rows("SELECT * FROM stock_state ORDER BY item_key"), self.rows("SELECT * FROM stock_events")))

    def test_old_data_is_pruned_once_per_ist_day_not_every_push(self):
        self.capture(items(5))
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES "
                         "('2020-01-01', 'OLD', 'OLD', 3, 2, '2020-01-01T00:00:00')")
            conn.execute("INSERT INTO car_interest_daily VALUES ('2020-01-01', 'OLDCAR', 'dealer', 1, 4)")
            conn.execute("INSERT INTO search_miss_daily VALUES ('2020-01-01', 'oldterm', 'dealer', 2)")
        with self.at(2026, 1, 15, 6, 30):
            self.capture(items(5, qty=4))                          # first call of the day: prunes
            self.assertEqual(self.rows("SELECT COUNT(*) FROM stock_events WHERE item_key = 'OLD'"), [(0,)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM car_interest_daily"), [(0,)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM search_miss_daily"), [(0,)])
            self.assertEqual(self.meta("last_prune_day_ist"), "2026-01-15")
            with self.db.get_connection() as conn:                 # put old rows back: the same day must not prune again
                conn.execute("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES "
                             "('2020-01-01', 'OLD', 'OLD', 3, 2, '2020-01-01T00:00:00')")
            self.capture(items(5, qty=3))
            self.assertEqual(self.rows("SELECT COUNT(*) FROM stock_events WHERE item_key = 'OLD'"), [(1,)])
        with self.at(2026, 1, 16, 6, 30):
            self.capture(items(5, qty=2))                          # next day: prunes again
            self.assertEqual(self.rows("SELECT COUNT(*) FROM stock_events WHERE item_key = 'OLD'"), [(0,)])

    def test_recent_rows_are_kept_by_the_prune(self):
        self.capture(items(5))
        with self.at(2026, 1, 15, 6, 30):
            self.capture(items(5, qty=4))
            self.assertEqual(len(self.events()), 5)

    def test_ist_day_boundary_is_18_30_utc(self):
        self.capture([{"item_name": "A", "qty": 9}])
        with self.at(2026, 1, 15, 18, 29):
            self.capture([{"item_name": "A", "qty": 8}])
        with self.at(2026, 1, 15, 18, 31):
            self.capture([{"item_name": "A", "qty": 7}])
        self.assertEqual([(e[1], e[2], e[3]) for e in self.events()], [(9, 8, "2026-01-15"), (8, 7, "2026-01-16")])

    def test_today_ist_ignores_the_machine_time_zone(self):
        with self.at(2026, 3, 1, 18, 30):
            self.assertEqual(self.app_module._today_ist(), "2026-03-02")
        with self.at(2026, 3, 1, 18, 29):
            self.assertEqual(self.app_module._today_ist(), "2026-03-01")

    def test_capture_is_fast_for_production_size(self):
        base = items(2800)
        start = time.perf_counter(); self.capture(base); first = time.perf_counter() - start
        start = time.perf_counter(); self.capture(base); same = time.perf_counter() - start
        changed = [dict(r, qty=r["qty"] + 1) if i < 200 else r for i, r in enumerate(base)]
        start = time.perf_counter(); self.capture(changed); busy = time.perf_counter() - start
        print(f"\n  capture timing: first {first * 1000:.0f} ms, no change {same * 1000:.0f} ms, 200 changes {busy * 1000:.0f} ms")
        self.assertLess(max(first, same, busy), 1.0)               # generous: the target is well under 100 ms
        self.assertEqual(len(self.events()), 200)


class CarInterest(CaptureBase):
    def interest(self):
        return self.rows("SELECT day_ist, car_key, role, user_id, opens FROM car_interest_daily")

    def misses(self):
        return self.rows("SELECT day_ist, term, role, count FROM search_miss_daily ORDER BY term")

    def test_opening_a_car_counts_once_per_user_day_and_role(self):
        with self.at(2026, 1, 15, 6, 30):
            self.assertEqual(self.dealer.get("/designs", query_string={"car": "ALTO"}).status_code, 200)
            self.dealer.get("/designs", query_string={"car": "ALTO"})          # a reload: inside 10 minutes
        dealer_id = self.rows("SELECT id FROM users WHERE username = 'star'")[0][0]
        self.assertEqual(self.interest(), [("2026-01-15", "ALTO", "dealer", dealer_id, 1)])
        self.assertTrue(self.meta("interest_tracking_started_at_utc"))

    def test_opening_again_after_ten_minutes_counts_again(self):
        clock = {"now": 1000.0}
        with mock.patch.object(self.app_module, "_interest_clock", side_effect=lambda: clock["now"]):
            self.dealer.get("/designs", query_string={"car": "ALTO"})
            clock["now"] += 599
            self.dealer.get("/designs", query_string={"car": "ALTO"})
            self.assertEqual(self.interest()[0][4], 1)
            clock["now"] += 2
            self.dealer.get("/designs", query_string={"car": "ALTO"})
        self.assertEqual(self.interest()[0][4], 2)

    def test_two_cars_are_counted_separately(self):
        self.dealer.get("/designs", query_string={"car": "ALTO"})
        self.dealer.get("/designs", query_string={"car": "SWIFT"})
        self.assertEqual(sorted(r[1] for r in self.interest()), ["ALTO", "SWIFT"])

    def test_admin_is_recorded_with_role_admin_and_usage_charts_are_unchanged(self):
        self.admin.get("/designs", query_string={"car": "ALTO"})
        self.assertEqual([r[2] for r in self.interest()], ["admin"])

    def test_unknown_car_names_are_not_stored(self):
        self.dealer.get("/designs", query_string={"car": "NOT A REAL CAR"})
        self.assertEqual(self.interest(), [])

    def test_anonymous_and_failed_requests_record_nothing(self):
        self.anon.get("/designs", query_string={"car": "ALTO"})
        self.assertEqual(self.interest(), [])

    def test_recording_error_never_breaks_the_car_page(self):
        expected = self.dealer.get("/designs", query_string={"car": "SWIFT"}).get_data()
        self.app_module._INTEREST_RECENT.clear()
        with mock.patch.object(self.app_module, "_record_car_open", side_effect=RuntimeError("boom")):
            response = self.dealer.get("/designs", query_string={"car": "SWIFT"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), expected)

    def test_search_with_no_result_is_a_miss(self):
        with self.at(2026, 1, 15, 6, 30):
            self.dealer.get("/api/search_cars", query_string={"q": "  Zzzz   Wwww "})
        self.assertEqual(self.misses(), [("2026-01-15", "zzzz wwww", "dealer", 1)])
        self.assertTrue(self.meta("interest_tracking_started_at_utc"))

    def test_search_with_results_and_short_terms_are_not_recorded(self):
        self.dealer.get("/api/search_cars", query_string={"q": "alto"})
        self.dealer.get("/api/search_cars", query_string={"q": "zz"})
        self.dealer.get("/api/search_cars", query_string={"q": ""})
        self.dealer.get("/api/search_cars", query_string={"q": "zzzz", "page": 2})
        self.assertEqual(self.misses(), [])

    def test_same_search_twice_counts_once(self):
        self.dealer.get("/api/search_cars", query_string={"q": "qqqq"})
        self.dealer.get("/api/search_cars", query_string={"q": "qqqq"})
        self.assertEqual([m[3] for m in self.misses()], [1])

    def test_typing_forward_keeps_only_the_final_term(self):
        for term in ("qqq", "qqqq", "qqqqx"):
            self.dealer.get("/api/search_cars", query_string={"q": term})
        self.assertEqual([(m[1], m[3]) for m in self.misses()], [("qqqqx", 1)])

    def test_backspacing_does_not_add_a_shorter_term(self):
        self.dealer.get("/api/search_cars", query_string={"q": "qqqqx"})
        self.dealer.get("/api/search_cars", query_string={"q": "qqqq"})
        self.assertEqual([(m[1], m[3]) for m in self.misses()], [("qqqqx", 1)])

    def test_admin_search_misses_are_a_separate_role(self):
        self.dealer.get("/api/search_cars", query_string={"q": "nonecar"})
        self.admin.get("/api/search_cars", query_string={"q": "nonecar"})
        self.assertEqual(sorted((m[1], m[2], m[3]) for m in self.misses()), [("nonecar", "admin", 1), ("nonecar", "dealer", 1)])

    def test_search_recording_error_never_breaks_search(self):
        expected = self.dealer.get("/api/search_cars", query_string={"q": "alto"}).get_data()
        with mock.patch.object(self.app_module, "_record_search_miss", side_effect=RuntimeError("boom")):
            response = self.dealer.get("/api/search_cars", query_string={"q": "nonecar"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.dealer.get("/api/search_cars", query_string={"q": "alto"}).get_data(), expected)

    def test_throttle_memory_is_bounded(self):
        limit = self.app_module.INTEREST_MAX_TRACKED
        for i in range(limit):
            self.app_module._INTEREST_RECENT[(i, "X")] = 0.0       # the map is full
        self.app_module._record_car_open(999999, "dealer", "ALTO")  # a new key arrives: the map is cleared first
        self.assertLessEqual(len(self.app_module._INTEREST_RECENT), limit)
        self.assertEqual(len(self.app_module._INTEREST_RECENT), 1)


if __name__ == "__main__":
    unittest.main()
