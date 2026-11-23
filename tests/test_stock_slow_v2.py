"""Stock Management v2, part C: Slow stock from the sales history (with the stock-change logic as the fallback), and the
old interest recording is gone (its tables stay, unused)."""
import unittest
from unittest import mock

from tests import support
from tests.test_stock_needs_v2 import V2Base, BASE, day, FORCE
from tests.support import INTAKE_HEADERS


class SlowFromSales(V2Base):
    def slow(self, days=60, client=None, **params):
        response = (client or self.admin).get(BASE + "/data", query_string=dict(tab="slow", days=days, **params))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def history(self, days_back=100):
        """Sales history that reaches back `days_back` days (one old sale of an item that is not in stock)."""
        self.sale("HISTORY ANCHOR", 1, days_back)

    def test_items_in_stock_whose_last_sale_is_older_than_the_threshold(self):
        a = lambda n: self.item("CARA", n)
        self.history(100)
        for n, last in ((1, 70), (2, 10), (4, 59), (5, 60)):
            self.in_stock(a(n), 2)
            self.sale(a(n), 1, last)
        data = self.slow(60)
        self.assertEqual(data["source"], "sales")
        self.assertEqual(sorted(self.names(data)), sorted([a(1), a(5)]))                        # 70 and exactly 60 days; not 10 or 59
        rows = {r["item_name"]: r for r in data["rows"]}
        self.assertEqual((rows[a(1)]["qty"], rows[a(1)]["last_sold"], rows[a(1)]["days_sitting"]), (2, day(70), 70))
        self.assertEqual(rows[a(5)]["days_sitting"], 60)

    def test_longest_sitting_first_then_the_larger_quantity(self):
        a = lambda n: self.item("CARA", n)
        self.history(200)
        self.in_stock(a(1), 1); self.sale(a(1), 1, 70)
        self.in_stock(a(2), 3); self.sale(a(2), 1, 120)
        self.in_stock(a(3), 5); self.sale(a(3), 1, 70)
        self.assertEqual(self.names(self.slow(60)), [a(2), a(3), a(1)])

    def test_never_sold_in_the_history_counts_and_shows_no_sale_date(self):
        a1 = self.item("CARA", 1)
        self.history(100)
        self.in_stock(a1, 1)
        row = self.slow(60)["rows"][0]
        self.assertEqual((row["item_name"], row["last_sold"], row["days_sitting"]), (a1, None, 100))

    def test_a_return_is_not_a_sale(self):
        a1 = self.item("CARA", 1)
        self.history(100)
        self.in_stock(a1, 1)
        self.sale(a1, 1, 80); self.sale(a1, 1, 5, kind="return")
        row = self.slow(60)["rows"][0]
        self.assertEqual((row["last_sold"], row["days_sitting"]), (day(80), 80))

    def test_items_not_in_stock_are_never_listed(self):
        self.history(100)
        self.sale(self.item("CARA", 1), 1, 90)
        self.assertEqual(self.slow(60)["rows"], [])

    def test_an_item_that_came_into_stock_recently_is_not_slow(self):
        a1 = self.item("CARA", 1)
        self.history(100)
        self.in_stock(a1, 3)
        self.event(a1, 0, 3, 5)
        self.assertEqual(self.slow(60)["rows"], [])

    def test_the_90_day_selector(self):
        a = lambda n: self.item("CARA", n)
        self.history(200)
        self.in_stock(a(1), 1); self.sale(a(1), 1, 70)
        self.in_stock(a(2), 1); self.sale(a(2), 1, 100)
        self.assertEqual(sorted(self.names(self.slow(60))), sorted([a(1), a(2)]))
        self.assertEqual(self.names(self.slow(90)), [a(2)])

    def test_search_filters_the_list(self):
        self.history(200)
        self.in_stock(self.item("CARA", 1), 1); self.in_stock(self.item("CARB", 1), 1)
        self.assertEqual(self.names(self.slow(60, q="carb")), [self.item("CARB", 1)])

    def test_a_short_sales_history_falls_back_to_the_stock_changes_and_keeps_its_message(self):
        self.sale(self.item("CARB", 1), 1, 30)                  # sales only go back 30 days: not enough for 60
        self.sql("INSERT INTO capture_meta VALUES ('stock_tracking_started_at_utc', '2026-02-28T06:30:00')")
        self.in_stock(self.item("CARA", 1), 5)
        self.app_module._stock_mgmt_cache_clear()
        data = self.slow(60)
        self.assertTrue(data["insufficient_history"])
        self.assertEqual(data["source"], "stock_changes")
        self.assertEqual(data["message"], "Tracking only since 2026-02-28 (20 days). This list will appear after 2026-04-29.")

    def test_a_short_sales_history_with_long_stock_tracking_uses_the_old_logic(self):
        a = lambda n: self.item("CARA", n)
        self.sale(self.item("CARB", 1), 1, 30)
        self.sql("INSERT INTO capture_meta VALUES ('stock_tracking_started_at_utc', '2025-12-10T06:30:00')")      # 100 days
        self.in_stock(a(1), 5); self.in_stock(a(2), 5)
        self.event(a(2), 6, 5, 10)                              # decreased 10 days ago: not slow
        data = self.slow(60)
        self.assertEqual(data["source"], "stock_changes")
        self.assertEqual(self.names(data), [a(1)])
        self.assertEqual(data["rows"][0]["days_sitting"], 100)

    def test_with_no_sales_at_all_the_old_logic_and_message_are_used(self):
        self.in_stock(self.item("CARA", 1), 5)
        data = self.slow(60)
        self.assertEqual(data["source"], "stock_changes")
        self.assertTrue(data["insufficient_history"])
        self.assertIn("Stock tracking has not started yet", data["message"])

    def test_only_60_and_90_days_and_admin_only(self):
        self.assertEqual(self.admin.get(BASE + "/data", query_string={"tab": "slow", "days": 30}).status_code, 400)
        self.assertEqual(self.dealer.get(BASE + "/data", query_string={"tab": "slow", "days": 60}).status_code, 403)
        self.assertIn(self.anon.get(BASE + "/data", query_string={"tab": "slow", "days": 60}).status_code, (401, 403))

    def test_the_page_says_sold_not_decreased_when_the_source_is_sales(self):
        text = self.admin.get(BASE).get_data(as_text=True)
        self.assertIn("no sale in the sales history", text)


class OldInterestRecordingIsGone(V2Base):
    def counts(self):
        with self.db.get_connection() as conn:
            return (conn.execute("SELECT COUNT(*) FROM car_interest_daily").fetchone()[0], conn.execute("SELECT COUNT(*) FROM search_miss_daily").fetchone()[0])

    def test_the_functions_and_the_hook_no_longer_exist(self):
        for name in ("_record_stock_interest", "_record_car_open", "_record_search_miss", "_INTEREST_RECENT", "_SEARCH_MISS_LAST",
                     "_known_car_keys", "_interest_clock", "_normalize_interest_term", "INTEREST_MAX_TRACKED", "_stock_tab_asked"):
            self.assertFalse(hasattr(self.app_module, name), name)
        hooks = [f.__name__ for f in self.app_module.app.after_request_funcs.get(None, [])]
        self.assertNotIn("_record_stock_interest", hooks)

    def test_opening_a_car_and_searching_still_work_and_write_nothing_to_the_old_tables(self):
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM car_interest_daily")
            conn.execute("DELETE FROM search_miss_daily")
        for client in (self.dealer, self.admin):
            self.assertEqual(client.get("/designs", query_string={"car": "CARA"}).status_code, 200)
            self.assertEqual(client.get("/api/search_cars", query_string={"q": "nosuchcarname"}).status_code, 200)
            self.assertEqual(client.get("/cars").status_code, 200)
        self.assertEqual(self.counts(), (0, 0))

    def test_the_tables_and_their_rows_are_left_alone(self):
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM car_interest_daily")
            conn.execute("DELETE FROM search_miss_daily")
            conn.execute("INSERT INTO car_interest_daily VALUES ('2020-01-01', 'OLDCAR', 'dealer', 1, 4)")
            conn.execute("INSERT INTO search_miss_daily VALUES ('2020-01-01', 'oldterm', 'dealer', 2)")
        self.app_module._capture_stock_changes([{"item_name": "A", "qty": 3}])
        self.app_module._capture_stock_changes([{"item_name": "A", "qty": 1}])      # a changed push: the once-a-day prune runs
        self.assertEqual(self.counts(), (1, 1))                                      # not pruned, not touched

    def test_the_asked_for_data_is_gone_but_the_other_tabs_remain(self):
        self.assertEqual(self.admin.get(BASE + "/data", query_string={"tab": "asked"}).status_code, 400)
        for tab in ("order", "ordered", "slow"):
            self.assertEqual(self.admin.get(BASE + "/data", query_string={"tab": tab}).status_code, 200, tab)

    def test_capture_never_opens_a_file(self):
        import builtins
        self.app_module._capture_stock_changes([{"item_name": "A", "qty": 3}, {"item_name": "B", "qty": 2}])

        def refuse(*args, **kwargs):
            raise AssertionError(f"file opened: {args[:1]}")
        with mock.patch.object(builtins, "open", side_effect=refuse):
            self.app_module._capture_stock_changes([{"item_name": "A", "qty": 1}, {"item_name": "C", "qty": 2}])

    def test_interest_ordering_still_comes_from_the_usage_activity_log(self):
        for name in ("CARA", "CARB"):
            for n in range(1, 10):
                self.in_stock(self.item(name, n), 1) if n <= 2 else self.sale(self.item(name, n), 1, 100)
        self.views("CARA", 6)
        self.views("CARB", 1)
        rows = self.admin.get(BASE + "/data", query_string={"tab": "needs", "section": "thin"}).get_json()["rows"]
        self.assertEqual([(r["car_name"], r["opens"]) for r in rows], [("CARA", 6), ("CARB", 1)])


if __name__ == "__main__":
    unittest.main()
