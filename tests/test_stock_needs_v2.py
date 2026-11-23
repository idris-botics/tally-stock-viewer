"""Stock Management v2, Needs restocking: sold-out designs, last pieces and thin cars, built from the sales lines the
shop pushes (and, where there are no sales, from the stock changes the site has seen). No 'days left' and no speed."""
import json
import math
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import support
from tests.support import INTAKE_HEADERS

BASE = "/admin/stock_mgmt"
NOW = datetime(2026, 3, 20, 6, 30, tzinfo=timezone.utc)          # 12:00 IST on 2026-03-20
FORCE = dict(INTAKE_HEADERS, **{"X-Intake-Force": "1"})


def day(offset):
    return (datetime(2026, 3, 20) - timedelta(days=offset)).strftime("%Y-%m-%d")


def catalogue_payload(cars, items_per_car):
    hierarchy, stock = [], []
    for car in cars:
        hierarchy.append({"item_name": car, "qty": items_per_car})
        for n in range(1, items_per_car + 1):
            hierarchy.append({"item_name": f"{car} ITEM {n:03d}", "qty": 1})
            stock.append({"item_name": f"{car} ITEM {n:03d}", "qty": 1})
        hierarchy.append({"item_name": "", "qty": ""})
    return {"car_master": list(cars), "main_hierarchy": hierarchy, "item_stock": stock}


class V2Base(unittest.TestCase):
    cars = ["CARA", "CARB", "CARC"]
    items_per_car = 9

    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")
        self.admin = support.client_for("admin_locked")
        self.counter = 0
        self.app_module._stock_mgmt_cache_clear()
        r = self.anon.post("/admin/intake/sync_data", json=catalogue_payload(self.cars, self.items_per_car), headers=FORCE)
        self.assertEqual(r.status_code, 200)
        with self.db.get_connection() as conn:
            for table in ("stock_state", "stock_events", "capture_meta", "sales_lines", "restock_draft", "restock_orders", "stock_mgmt_settings"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM user_activity WHERE action = 'view_car'")
        patcher = mock.patch.object(self.app_module, "_stock_now_utc", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        support.restore_state(self.snapshot)

    # ---- helpers
    def sql(self, statement, args=()):
        with self.db.get_connection() as conn:
            conn.execute(statement, args)

    def item(self, car, n):
        return f"{car} ITEM {n:03d}"

    def sale(self, name, qty, days_ago, kind="sale"):
        self.counter += 1
        self.sql("INSERT INTO sales_lines VALUES (?, ?, ?, ?, ?, ?)", (f"{self.counter:08x}", day(days_ago), kind, name.upper(), name, qty))
        self.app_module._stock_mgmt_cache_clear()

    def in_stock(self, name, qty=1):
        self.sql("INSERT OR REPLACE INTO stock_state VALUES (?, ?, ?, 'x')", (name.upper(), name, qty))
        self.app_module._stock_mgmt_cache_clear()

    def event(self, name, before, after, days_ago):
        self.sql("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES (?, ?, ?, ?, ?, 'x')",
                 (day(days_ago), name.upper(), name, before, after))
        self.app_module._stock_mgmt_cache_clear()

    def views(self, car, count, days_ago=1, role="dealer"):
        user = self.db_one("SELECT id FROM users WHERE username = ?", ("star" if role == "dealer" else "admin",))
        at = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.db.get_connection() as conn:
            conn.executemany("INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (?, ?, 'view_car', ?)", [(user, at, car)] * count)
        self.app_module._stock_mgmt_cache_clear()

    def db_one(self, statement, args=()):
        with self.db.get_connection() as conn:
            return conn.execute(statement, args).fetchone()[0]

    def setting(self, **values):
        response = self.admin.post(BASE + "/settings", json=values)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def get(self, section="sold", client=None, **params):
        response = (client or self.admin).get(BASE + "/data", query_string=dict(tab="needs", section=section, **params))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def names(self, data):
        return [r["item_name"] for r in data["rows"]]


class SoldOut(V2Base):
    def test_membership(self):
        a = lambda n: self.item("CARA", n)
        self.sale(a(1), 3, 10)                       # sold, not in stock: listed
        self.sale(a(2), 5, 10); self.in_stock(a(2))  # sold but in stock: not listed
        self.in_stock(a(3), 2)                       # never sold, in stock: not listed
        # a(4) never sold and not in stock: not listed
        self.sale(a(5), 2, 200)                      # sold only 200 days ago: outside the default 90 days
        data = self.get("sold")
        self.assertEqual(self.names(data), [a(1)])
        self.assertEqual(data["total"], 1)
        self.setting(window_days=365)
        self.assertEqual(sorted(self.names(self.get("sold"))), [a(1), a(5)])

    def test_the_row_carries_what_the_page_shows(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 3, 10); self.sale(a1, 2, 4)
        self.event(a1, 1, 0, 3)
        row = self.get("sold")["rows"][0]
        self.assertEqual((row["item_name"], row["car"], row["sold"], row["window_days"], row["last_sold"], row["sold_out_since"]),
                         (a1, "CARA", 5, 90, day(4), day(3)))
        self.assertEqual((row["open_order"], row["on_order_list"]), (None, False))

    def test_sold_out_since_is_a_dash_when_stock_changes_do_not_know(self):
        self.sale(self.item("CARA", 1), 3, 10)
        self.assertIsNone(self.get("sold")["rows"][0]["sold_out_since"])

    def test_sorting_most_sold_first_then_most_recent_sale(self):
        a = lambda n: self.item("CARA", n)
        self.sale(a(1), 5, 3)
        self.sale(a(2), 5, 10)
        self.sale(a(3), 7, 50)
        self.sale(a(4), 1, 1)
        self.assertEqual(self.names(self.get("sold")), [a(3), a(1), a(2), a(4)])

    def test_the_car_with_the_best_selling_design_comes_first_and_a_car_is_never_split(self):
        self.sale(self.item("CARA", 1), 4, 5)
        self.sale(self.item("CARB", 1), 9, 5)
        self.sale(self.item("CARA", 2), 3, 5)
        self.sale(self.item("CARB", 2), 1, 5)
        self.assertEqual([(r["car"], r["item_name"]) for r in self.get("sold")["rows"]],
                         [("CARB", self.item("CARB", 1)), ("CARB", self.item("CARB", 2)), ("CARA", self.item("CARA", 1)), ("CARA", self.item("CARA", 2))])

    def test_min_sold_and_window_change_the_list(self):
        a = lambda n: self.item("CARA", n)
        self.sale(a(1), 1, 10); self.sale(a(2), 3, 10); self.sale(a(3), 2, 40); self.sale(a(4), 4, 100)
        self.assertEqual(sorted(self.names(self.get("sold"))), [a(1), a(2), a(3)])
        self.setting(min_sold=2)
        self.assertEqual(sorted(self.names(self.get("sold"))), [a(2), a(3)])
        self.setting(window_days=30)
        self.assertEqual(self.names(self.get("sold")), [a(2)])
        self.setting(window_days=365, min_sold=1)
        self.assertEqual(sorted(self.names(self.get("sold"))), [a(1), a(2), a(3), a(4)])

    def test_returns_reduce_net_sales_and_never_go_below_zero(self):
        a = lambda n: self.item("CARA", n)
        self.sale(a(1), 5, 10); self.sale(a(1), 4, 8, kind="return")          # net 1
        self.sale(a(2), 2, 10); self.sale(a(2), 5, 8, kind="return")          # net below zero: counts as 0
        self.sale(a(3), 3, 10)
        data = self.get("sold")
        self.assertEqual({r["item_name"]: r["sold"] for r in data["rows"]}, {a(1): 1, a(3): 3})
        self.setting(min_sold=2)
        self.assertEqual(self.names(self.get("sold")), [a(3)])

    def test_a_return_outside_the_window_does_not_reduce_this_windows_sales(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 3, 10); self.sale(a1, 3, 200, kind="return")
        self.assertEqual(self.get("sold")["rows"][0]["sold"], 3)

    def test_without_sales_data_the_list_comes_from_stock_changes(self):
        a = lambda n: self.item("CARA", n)
        self.event(a(1), 2, 0, 10)                 # dropped to zero inside the window: listed (no sales data at all)
        self.event(a(2), 3, 1, 10)                 # only went down: not sold out
        self.event(a(3), 2, 0, 200)                # dropped to zero long ago: outside the window
        self.event(a(4), 0, 3, 10)                 # came into stock: not sold out
        data = self.get("sold")
        self.assertEqual(self.names(data), [a(1)])
        self.assertEqual((data["rows"][0]["sold"], data["rows"][0]["sold_out_since"]), (0, day(10)))
        self.assertIn("No sales history yet", data["banner"]["text"])

    def test_with_sales_data_covering_the_window_stock_changes_alone_do_not_list_an_item(self):
        a = lambda n: self.item("CARA", n)
        self.sale(self.item("CARB", 1), 1, 120)    # sales data reaches back past the 90-day window
        self.event(a(1), 2, 0, 10)
        self.assertEqual(self.get("sold")["rows"], [])

    def test_partial_sales_history_still_uses_stock_changes_for_the_rest(self):
        a = lambda n: self.item("CARA", n)
        self.sale(self.item("CARB", 1), 1, 20)     # sales data only goes back 20 days: it does not cover 90
        self.event(a(1), 2, 0, 60)
        self.assertEqual(sorted(self.names(self.get("sold"))), sorted([a(1), self.item("CARB", 1)]))

    def test_an_open_order_shows_its_badge_and_the_row_stays(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 2, 10)
        self.sql("INSERT INTO restock_orders (item_key, item_name, qty_ordered, ordered_at_utc) VALUES (?, ?, 2, '2026-03-17T05:00:00')", (a1.upper(), a1))
        row = self.get("sold")["rows"][0]
        self.assertEqual(row["open_order"]["ordered_on"], "2026-03-17")

    def test_search_and_the_car_filter(self):
        self.sale(self.item("CARA", 1), 2, 10); self.sale(self.item("CARB", 1), 2, 10)
        self.assertEqual(self.names(self.get("sold", q="carb")), [self.item("CARB", 1)])
        self.assertEqual(self.names(self.get("sold", q="item 001")), [self.item("CARA", 1), self.item("CARB", 1)])
        self.assertEqual(self.get("sold", q="nothing like this")["rows"], [])

    def test_totals_for_the_three_sections_come_with_every_answer(self):
        self.sale(self.item("CARA", 1), 2, 10)
        self.in_stock(self.item("CARA", 2), 1); self.sale(self.item("CARA", 2), 3, 20)
        totals = self.get("sold")["totals"]
        self.assertEqual((totals["sold"], totals["last"]), (1, 1))
        self.assertIn("thin", totals)

    def test_at_most_100_rows_per_answer_with_pages(self):
        self.setting(min_sold=1)
        pushed = self.anon.post("/admin/intake/sync_data", json=catalogue_payload(["BIGCAR"], 250), headers=FORCE)
        self.assertEqual(pushed.status_code, 200)
        self.sql("DELETE FROM stock_state")
        for n in range(1, 251):
            self.sale(self.item("BIGCAR", n), 1, 5)
        first = self.get("sold")
        self.assertEqual((len(first["rows"]), first["total"], first["more"]), (100, 250, True))
        self.assertEqual(len(self.get("sold", page=3)["rows"]), 50)
        self.assertFalse(self.get("sold", page=3)["more"])


class SuggestedQuantity(V2Base):
    def suggested(self, name):
        return next(r for r in self.get("sold")["rows"] if r["item_name"] == name)["suggested_qty"]

    def test_the_usual_quantity_from_the_last_restock_is_used(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 2, 10)
        self.event(a1, 0, 2, 100)
        self.assertEqual(self.suggested(a1), 2)

    def test_the_most_recent_restock_wins(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 2, 10)
        self.event(a1, 0, 1, 200); self.event(a1, 0, 2, 100)
        self.assertEqual(self.suggested(a1), 2)

    def test_a_big_usual_quantity_is_capped_by_keep_max(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 2, 10)
        self.event(a1, 0, 7, 100)
        self.assertEqual(self.suggested(a1), 3)
        self.setting(keep_max=5)
        self.assertEqual(self.suggested(a1), 5)
        self.setting(keep_max=1)
        self.assertEqual(self.suggested(a1), 1)

    def test_a_restock_older_than_a_year_is_not_used(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 7, 10)
        self.event(a1, 0, 2, 400)
        self.assertEqual(self.suggested(a1), 3)                 # unknown: ceil(7 / 3) = 3, capped at keep_max 3

    def test_unknown_usual_uses_a_third_of_the_last_90_days_of_sales(self):
        a, b, c = (self.item("CARA", n) for n in (1, 2, 3))
        self.sale(a, 7, 10); self.sale(b, 1, 10); self.sale(c, 4, 10); self.sale(c, 2, 5, kind="return")
        self.setting(keep_max=10)
        self.assertEqual((self.suggested(a), self.suggested(b), self.suggested(c)), (math.ceil(7 / 3), 1, 1))

    def test_it_is_never_below_one_and_never_above_keep_max(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 100, 10)
        self.assertEqual(self.suggested(a1), 3)
        self.event(a1, 0, 0, 100)                               # an odd zero event must not make it 0
        self.assertGreaterEqual(self.suggested(a1), 1)

    def test_the_order_list_button_adds_the_suggested_quantity(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 6, 10)
        row = self.get("sold")["rows"][0]
        self.assertEqual(self.admin.post(BASE + "/draft", json={"action": "add", "item_name": row["item_name"], "qty": row["suggested_qty"]}).status_code, 200)
        self.assertEqual(self.db_one("SELECT qty FROM restock_draft WHERE item_key = ?", (a1.upper(),)), row["suggested_qty"])
        self.assertTrue(self.get("sold")["rows"][0]["on_order_list"])

    def test_an_item_known_only_from_sales_can_be_added_to_the_order_list(self):
        self.sale("ONLY IN SALES", 2, 10)
        self.assertEqual(self.admin.post(BASE + "/draft", json={"action": "add", "item_name": "ONLY IN SALES", "qty": 1}).status_code, 200)


class LastPiece(V2Base):
    def test_qty_one_with_enough_recent_sales_is_listed(self):
        a = lambda n: self.item("CARA", n)
        self.in_stock(a(1), 1); self.sale(a(1), 2, 100)                 # exactly the default minimum of 2
        self.in_stock(a(2), 2); self.sale(a(2), 5, 100)                 # qty 2: not a last piece
        self.in_stock(a(3), 1); self.sale(a(3), 1, 100)                 # too few sales
        self.in_stock(a(4), 1); self.sale(a(4), 9, 300)                 # sales older than 180 days
        data = self.get("last")
        self.assertEqual(self.names(data), [a(1)])
        row = data["rows"][0]
        self.assertEqual((row["sold"], row["window_days"], row["last_sold"]), (2, 180, day(100)))
        self.assertGreaterEqual(row["suggested_qty"], 1)

    def test_returns_count_and_the_minimum_can_change(self):
        a = lambda n: self.item("CARA", n)
        self.in_stock(a(1), 1); self.sale(a(1), 5, 50); self.sale(a(1), 4, 40, kind="return")      # net 1
        self.in_stock(a(2), 1); self.sale(a(2), 3, 50)
        self.assertEqual(self.names(self.get("last")), [a(2)])
        self.setting(last_piece_min_sold=1)
        self.assertEqual(sorted(self.names(self.get("last"))), [a(1), a(2)])

    def test_sorted_by_sales_and_grouped_by_car(self):
        self.in_stock(self.item("CARA", 1), 1); self.sale(self.item("CARA", 1), 3, 10)
        self.in_stock(self.item("CARB", 1), 1); self.sale(self.item("CARB", 1), 8, 10)
        self.assertEqual([r["car"] for r in self.get("last")["rows"]], ["CARB", "CARA"])

    def test_the_section_can_be_hidden_with_a_switch(self):
        self.in_stock(self.item("CARA", 1), 1); self.sale(self.item("CARA", 1), 3, 10)
        self.assertEqual(self.get("last")["settings"]["show_last_piece"], 1)
        self.setting(show_last_piece=0)
        data = self.get("last")
        self.assertEqual((data["rows"], data["totals"]["last"], data["settings"]["show_last_piece"]), ([], 0, 0))


class ThinCars(V2Base):
    items_per_car = 9

    def make_active(self, car, in_stock, sold_out):
        """`in_stock` items in stock and `sold_out` items that sold in the last year but are gone."""
        for n in range(1, in_stock + 1):
            self.in_stock(self.item(car, n), 1)
        for n in range(in_stock + 1, in_stock + sold_out + 1):
            self.sale(self.item(car, n), 1, 100)

    def test_thin_by_ratio_and_minimum_designs(self):
        self.make_active("CARA", 4, 5)             # 4 of 9 in stock: 44% <= 50%: thin
        self.make_active("CARB", 5, 4)             # 5 of 9: 55%: not thin
        self.make_active("CARC", 0, 3)             # only 3 active designs: under thin_min_designs 4
        data = self.get("thin")
        self.assertEqual([(r["car_name"], r["in_stock"], r["active"]) for r in data["rows"]], [("CARA", 4, 9)])
        self.assertEqual(data["totals"]["thin"], 1)

    def test_exactly_at_the_ratio_counts_as_thin_and_settings_move_the_line(self):
        self.make_active("CARA", 2, 2)             # 2 of 4 = exactly 50%
        self.assertEqual([r["car_name"] for r in self.get("thin")["rows"]], ["CARA"])
        self.setting(thin_ratio_percent=40)
        self.assertEqual(self.get("thin")["rows"], [])
        self.setting(thin_ratio_percent=50, thin_min_designs=5)
        self.assertEqual(self.get("thin")["rows"], [])

    def test_designs_that_never_sold_and_are_not_in_stock_are_not_active(self):
        self.in_stock(self.item("CARA", 1), 1)
        for n in range(2, 6):
            self.sale(self.item("CARA", n), 1, 100)
        # items 6..9 never sold and are not in stock: they are not counted, so 1 of 5
        row = self.get("thin")["rows"][0]
        self.assertEqual((row["in_stock"], row["active"]), (1, 5))

    def test_sales_older_than_a_year_and_net_zero_do_not_make_a_design_active(self):
        a = lambda n: self.item("CARA", n)
        self.in_stock(a(1), 1)
        self.sale(a(2), 1, 400)                                        # too old
        self.sale(a(3), 2, 100); self.sale(a(3), 2, 90, kind="return") # net zero
        self.sale(a(4), 1, 100); self.sale(a(5), 1, 100); self.sale(a(6), 1, 100)
        row = self.get("thin")["rows"][0]
        self.assertEqual((row["in_stock"], row["active"]), (1, 4))

    def test_sorted_by_dealer_interest_first_then_the_lowest_ratio(self):
        self.make_active("CARA", 3, 6)             # 33%
        self.make_active("CARB", 1, 8)             # 11%
        self.make_active("CARC", 2, 7)             # 22%
        self.assertEqual([r["car_name"] for r in self.get("thin")["rows"]], ["CARB", "CARC", "CARA"])       # no interest: lowest ratio first
        self.views("CARA", 5)
        self.views("CARC", 2)
        self.assertEqual([r["car_name"] for r in self.get("thin")["rows"]], ["CARA", "CARC", "CARB"])
        self.assertEqual([r["opens"] for r in self.get("thin")["rows"]], [5, 2, 0])

    def test_admin_opens_follow_the_existing_include_admin_setting(self):
        self.make_active("CARA", 3, 6); self.make_active("CARB", 1, 8)
        self.views("CARA", 9, role="admin")
        self.assertEqual(self.get("thin")["rows"][0]["car_name"], "CARA")
        self.setting(include_admin=0)
        self.assertEqual(self.get("thin")["rows"][0]["car_name"], "CARB")

    def test_the_expansion_lists_the_sold_out_designs_of_that_car(self):
        self.make_active("CARA", 4, 5)
        data = self.get("thin", block="car_items", car="CARA")
        self.assertEqual(sorted(self.names(data)), sorted(self.item("CARA", n) for n in range(5, 10)))
        self.assertIn("suggested_qty", data["rows"][0])
        self.assertEqual(self.get("thin", block="car_items", car="NO SUCH CAR")["rows"], [])

    def test_search_filters_the_cars(self):
        self.make_active("CARA", 1, 8); self.make_active("CARB", 1, 8)
        self.assertEqual([r["car_name"] for r in self.get("thin", q="carb")["rows"]], ["CARB"])


class Settings(V2Base):
    def test_defaults(self):
        self.assertEqual(self.get("sold")["settings"], {"keep_max": 3, "min_sold": 1, "window_days": 90, "last_piece_min_sold": 2,
                                                       "thin_ratio_percent": 50, "thin_min_designs": 4, "show_last_piece": 1,
                                                       "include_admin": 1})

    def test_valid_values_are_saved_and_returned(self):
        self.setting(keep_max=10, min_sold=50, window_days=365, last_piece_min_sold=1, thin_ratio_percent=90, thin_min_designs=50, show_last_piece=0)
        settings = self.get("sold")["settings"]
        self.assertEqual((settings["keep_max"], settings["min_sold"], settings["window_days"], settings["last_piece_min_sold"],
                          settings["thin_ratio_percent"], settings["thin_min_designs"], settings["show_last_piece"]), (10, 50, 365, 1, 90, 50, 0))

    def test_invalid_values_are_refused_with_short_fixed_messages_and_save_nothing(self):
        bad = ({"keep_max": 0}, {"keep_max": 11}, {"min_sold": 0}, {"min_sold": 51}, {"window_days": 60}, {"window_days": "90"}, {"window_days": True},
               {"window_days": 0}, {"last_piece_min_sold": 0}, {"last_piece_min_sold": 51}, {"thin_ratio_percent": 9}, {"thin_ratio_percent": 91},
               {"thin_min_designs": 1}, {"thin_min_designs": 51}, {"keep_max": 1.5}, {"keep_max": "3"}, {"show_last_piece": 2}, {"unknown": 1},
               {"low_days": 14}, {"low_qty": 2}, {"order_cover_days": 30}, {})
        for body in bad:
            response = self.admin.post(BASE + "/settings", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertLess(len(response.get_json()["error"]), 90)
        self.assertEqual(self.get("sold")["settings"]["keep_max"], 3)
        self.assertEqual(self.admin.post(BASE + "/settings", json={"keep_max": 4, "min_sold": 99}).status_code, 400)
        self.assertEqual(self.get("sold")["settings"]["keep_max"], 3)                           # one bad value saves none of them

    def test_old_settings_rows_are_left_alone_and_no_longer_read(self):
        self.sql("INSERT INTO stock_mgmt_settings VALUES ('low_days', '5')")
        self.sql("INSERT INTO stock_mgmt_settings VALUES ('low_qty', '7')")
        self.app_module._stock_mgmt_cache_clear()
        settings = self.get("sold")["settings"]
        self.assertNotIn("low_days", settings)
        self.assertNotIn("low_qty", settings)
        self.assertEqual(self.db_one("SELECT value FROM stock_mgmt_settings WHERE key = 'low_days'"), "5")


class NoMoreDaysLeft(V2Base):
    def test_the_old_wording_is_gone_from_the_page_and_from_the_data(self):
        a1 = self.item("CARA", 1)
        self.sale(a1, 4, 10); self.in_stock(self.item("CARA", 2), 1); self.sale(self.item("CARA", 2), 3, 10)
        page = self.admin.get(BASE).get_data(as_text=True).lower()
        for word in ("days left", "days_left", "per day", "a day", "gauge", "low_days", "low_qty", "order_cover_days", "running low"):
            self.assertNotIn(word, page, word)
        for section in ("sold", "last", "thin"):
            text = json.dumps(self.get(section)).lower()
            for word in ("days left", "days_left", "per day", "speed", "a day"):
                self.assertNotIn(word, text, (section, word))

    def test_the_other_tabs_still_work(self):
        for tab in ("order", "ordered", "slow"):
            response = self.admin.get(BASE + "/data", query_string={"tab": tab})
            self.assertEqual(response.status_code, 200, tab)

    def test_old_section_names_are_refused(self):
        for section in ("low", "out", "everything"):
            self.assertEqual(self.admin.get(BASE + "/data", query_string={"tab": "needs", "section": section}).status_code, 400, section)


class Banner(V2Base):
    def test_no_sales_history_yet(self):
        self.sql("INSERT INTO capture_meta VALUES ('stock_tracking_started_at_utc', '2026-03-08T06:30:00')")
        self.app_module._stock_mgmt_cache_clear()
        text = self.get("sold")["banner"]["text"]
        self.assertEqual(text, "No sales history yet. This list uses stock changes seen since 2026-03-08.")

    def test_no_sales_and_no_tracking_at_all(self):
        self.assertIn("No sales history yet", self.get("sold")["banner"]["text"])

    def test_short_and_long_sales_history(self):
        self.sale(self.item("CARA", 1), 1, 20)
        data = self.get("sold")["banner"]
        self.assertEqual(data["text"], f"Sales history from {day(20)} (20 days).")
        self.assertFalse(data["calm"])
        self.sale(self.item("CARA", 2), 1, 120)
        data = self.get("sold")["banner"]
        self.assertEqual(data["text"], f"Sales history from {day(120)} (120 days).")
        self.assertTrue(data["calm"])


class AdminOnlyV2(V2Base):
    def test_dealers_and_logged_out_visitors_are_denied_on_the_data_and_settings_routes(self):
        for client, allowed in ((self.dealer, (403,)), (self.anon, (401, 403))):
            for section in ("sold", "last", "thin"):
                self.assertIn(client.get(BASE + "/data", query_string={"tab": "needs", "section": section}).status_code, allowed)
            self.assertIn(client.get(BASE + "/data", query_string={"tab": "needs", "section": "thin", "block": "car_items", "car": "CARA"}).status_code, allowed)
            self.assertIn(client.post(BASE + "/settings", json={"keep_max": 5}).status_code, allowed)
        self.assertEqual(self.get("sold")["settings"]["keep_max"], 3)


class Speed(V2Base):
    def test_each_section_is_fast_with_production_sized_data(self):
        cars = [f"PCAR{n:03d}" for n in range(710)]
        pushed = self.anon.post("/admin/intake/sync_data", json=catalogue_payload(cars, 21), headers=FORCE)       # about 14,900 catalogue items
        self.assertEqual(pushed.status_code, 200)
        keys = [f"{car} ITEM {n:03d}" for car in cars for n in range(1, 22)]
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM stock_state")
            conn.executemany("INSERT INTO stock_state VALUES (?, ?, ?, 'x')", [(k.upper(), k, 1 + i % 3) for i, k in enumerate(keys[:2800])])
            conn.executemany("INSERT INTO sales_lines VALUES (?, ?, ?, ?, ?, ?)",
                             [(f"{i:08x}", day(i % 365), "return" if i % 17 == 0 else "sale", keys[(i * 7) % len(keys)].upper(), keys[(i * 7) % len(keys)], 1 + i % 3)
                              for i in range(100000)])
            conn.executemany("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES (?, ?, ?, ?, ?, 'x')",
                             [(day(i % 300), keys[i % len(keys)].upper(), keys[i % len(keys)], 0 if i % 2 else 2, 3 if i % 2 else 0) for i in range(6000)])
            conn.executemany("INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (1, '2026-03-10T05:00:00', 'view_car', ?)", [(c,) for c in cars[:300]])
        self.app_module.get_data_versions()          # the feeder-file check every page shares, done once per push: not part of this tab's cost
        timings = []
        for label, params in (("sold", {"section": "sold"}), ("last", {"section": "last"}), ("thin", {"section": "thin"}),
                              ("thin car items", {"section": "thin", "block": "car_items", "car": "PCAR001"})):
            elapsed = 9.0
            for _attempt in range(3):                    # every attempt starts with an empty cache; the best one is the cost, the rest is machine noise
                self.app_module._stock_mgmt_cache_clear()
                start = time.perf_counter()
                data = self.get(**params)
                elapsed = min(elapsed, time.perf_counter() - start)
            timings.append(f"{label} {elapsed * 1000:.0f} ms ({data['total']} rows)")
            self.assertLess(elapsed, 0.3, label)
        print("\n  needs restocking timings: " + ", ".join(timings))


if __name__ == "__main__":
    unittest.main()
