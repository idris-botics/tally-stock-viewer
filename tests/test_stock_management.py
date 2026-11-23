"""Stock Management, Part B: the admin-only pages (Needs restocking, Order list, Ordered, Slow stock, Asked for).

History and interest rows are written directly into the capture tables, so every number in a test is known."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import support
from tests.support import INTAKE_HEADERS

BASE = "/admin/stock_mgmt"
NOW = datetime(2026, 3, 20, 6, 30, tzinfo=timezone.utc)      # 12:00 IST on 2026-03-20


def catalogue_payload(cars, items_per_car=3):
    """cars: list of car names. Each car has `items_per_car` items called '<CAR> ITEM <n>'; every item is in stock."""
    hierarchy, stock = [], []
    for car in cars:
        hierarchy.append({"item_name": car, "qty": items_per_car})
        for n in range(1, items_per_car + 1):
            hierarchy.append({"item_name": f"{car} ITEM {n:03d}", "qty": 1})
            stock.append({"item_name": f"{car} ITEM {n:03d}", "qty": 1})
        hierarchy.append({"item_name": "", "qty": ""})
    return {"car_master": list(cars), "main_hierarchy": hierarchy, "item_stock": stock}


def day(offset):
    """IST day string `offset` days before 2026-03-20."""
    return (datetime(2026, 3, 20) - timedelta(days=offset)).strftime("%Y-%m-%d")


class StockMgmtBase(unittest.TestCase):
    cars = ["CARA", "CARB", "CARC"]
    items_per_car = 3

    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()
        cls.pushed = False

    def setUp(self):
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")
        self.admin = support.client_for("admin_locked")
        self.app_module._INTEREST_RECENT.clear()
        self.app_module._stock_mgmt_cache_clear()
        r = self.anon.post("/admin/intake/sync_data", json=catalogue_payload(self.cars, self.items_per_car), headers=INTAKE_HEADERS)
        self.assertEqual(r.status_code, 200)
        with self.db.get_connection() as conn:
            for table in ("stock_state", "stock_events", "capture_meta", "car_interest_daily", "search_miss_daily",
                          "restock_draft", "restock_orders", "stock_mgmt_settings"):
                conn.execute(f"DELETE FROM {table}")
        patcher = mock.patch.object(self.app_module, "_stock_now_utc", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        support.restore_state(self.snapshot)

    # ---- helpers
    def sql(self, statement, args=()):
        with self.db.get_connection() as conn:
            conn.execute(statement, args)

    def rows(self, statement, args=()):
        with self.db.get_connection() as conn:
            return [tuple(r) for r in conn.execute(statement, args).fetchall()]

    def tracking_since(self, days_ago):
        started = NOW - timedelta(days=days_ago)
        self.sql("INSERT OR REPLACE INTO capture_meta (key, value) VALUES ('stock_tracking_started_at_utc', ?)",
                 (started.strftime("%Y-%m-%dT%H:%M:%S"),))

    def interest_since(self, days_ago):
        started = NOW - timedelta(days=days_ago)
        self.sql("INSERT OR REPLACE INTO capture_meta (key, value) VALUES ('interest_tracking_started_at_utc', ?)",
                 (started.strftime("%Y-%m-%dT%H:%M:%S"),))

    def in_stock(self, **items):
        for name, qty in items.items():
            name = name.replace("_", " ")
            self.sql("INSERT OR REPLACE INTO stock_state (item_key, item_name, qty, updated_at_utc) VALUES (?, ?, ?, 'x')",
                     (name.upper(), name, qty))

    def event(self, name, before, after, days_ago=1):
        self.sql("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES (?, ?, ?, ?, ?, 'x')",
                 (day(days_ago), name.upper(), name, before, after))

    def opens(self, car, role, user_id, opens, days_ago=1):
        self.sql("INSERT OR REPLACE INTO car_interest_daily VALUES (?, ?, ?, ?, ?)", (day(days_ago), car, role, user_id, opens))

    def get(self, tab, client=None, **params):
        response = (client or self.admin).get(BASE + "/data", query_string=dict(tab=tab, **params))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, path, body, client=None):
        return (client or self.admin).post(BASE + path, json=body)

    def names(self, rows, key="item_name"):
        return [r[key] for r in rows]


class Settings(StockMgmtBase):
    def test_defaults(self):
        data = self.get("needs", section="low")
        self.assertEqual(data["settings"], {"low_days": 14, "low_qty": 2, "order_cover_days": 30, "asked_max_items": 3, "include_admin": 1})

    def test_saved_and_returned(self):
        self.assertEqual(self.post("/settings", {"low_days": 20, "low_qty": 0, "order_cover_days": 45}).status_code, 200)
        data = self.get("needs", section="low")
        self.assertEqual((data["settings"]["low_days"], data["settings"]["low_qty"], data["settings"]["order_cover_days"]), (20, 0, 45))

    def test_invalid_values_are_refused_with_short_fixed_messages(self):
        for body in ({"low_days": 0}, {"low_days": 366}, {"low_days": "abc"}, {"low_days": True}, {"low_days": 1.5},
                     {"low_qty": -1}, {"low_qty": 1001}, {"order_cover_days": 0}, {"order_cover_days": 366},
                     {"include_admin": 2}, {"unknown_key": 1}, {}):
            response = self.post("/settings", body)
            self.assertEqual(response.status_code, 400, body)
            message = response.get_json()["error"]
            self.assertLess(len(message), 80)
            self.assertNotIn("Traceback", message)
        self.assertEqual(self.get("needs", section="low")["settings"]["low_days"], 14)   # nothing was saved

    def test_a_bad_value_saves_nothing_even_if_another_is_good(self):
        self.assertEqual(self.post("/settings", {"low_days": 30, "low_qty": 5000}).status_code, 400)
        self.assertEqual(self.get("needs", section="low")["settings"]["low_days"], 14)

    def test_body_must_be_json(self):
        self.assertEqual(self.admin.post(BASE + "/settings", data="low_days=3").status_code, 400)


class VelocityAndNeeds(StockMgmtBase):
    def test_only_decreases_count_and_a_restock_is_ignored(self):
        self.tracking_since(10)
        self.in_stock(WIDGET=5)
        self.event("WIDGET", 10, 8, 9)      # sold 2
        self.event("WIDGET", 8, 5, 5)       # sold 3
        self.event("WIDGET", 5, 20, 3)      # restock: not a negative sale, not a sale
        self.event("WIDGET", 20, 18, 1)     # sold 2
        row = self.get("needs", section="low")["rows"][0]
        self.assertEqual(row["item_name"], "WIDGET")
        self.assertEqual(row["speed"], 0.7)                         # 7 sold over the 10 days tracked
        self.assertEqual(row["days_left"], 7)                       # 5 / 0.7 = 7.1

    def test_window_is_days_since_tracking_began_between_1_and_30(self):
        app = self.app_module
        self.assertEqual(app._stock_window_days(NOW - timedelta(hours=2)), 1)           # same day: at least 1
        self.assertEqual(app._stock_window_days(NOW - timedelta(days=10)), 10)
        self.assertEqual(app._stock_window_days(NOW - timedelta(days=400)), 30)
        self.assertEqual(app._stock_window_days(None), 1)

    def test_sales_older_than_30_days_do_not_count(self):
        self.tracking_since(200)
        self.post("/settings", {"low_days": 365})
        self.in_stock(WIDGET=10)
        self.event("WIDGET", 100, 10, 45)       # 90 sold, but 45 days ago
        self.event("WIDGET", 12, 10, 2)         # 2 sold in the window
        row = self.get("needs", section="low")["rows"][0]
        self.assertEqual(row["speed"], 0.1)                          # 2 / 30 days
        self.assertEqual(row["days_left"], 150)

    def test_running_low_by_days_left_and_by_quantity(self):
        self.tracking_since(10)
        self.in_stock(FAST=50, SLOW=100, FEW=2, PLENTY=10)
        self.event("FAST", 100, 50, 2)           # 50 over 10 days = 5/day: 10 days left
        self.event("SLOW", 110, 100, 2)          # 1/day: 100 days left
        rows = self.get("needs", section="low")["rows"]
        self.assertEqual(self.names(rows), ["FAST", "FEW"])          # days_left first, then the quantity-only item
        self.assertEqual(rows[0]["days_left"], 10)
        self.assertIsNone(rows[1]["days_left"])
        self.assertTrue(rows[1]["no_recent_sales"])

    def test_days_rule_needs_seven_days_of_history_but_the_quantity_rule_does_not(self):
        self.tracking_since(3)
        self.in_stock(FAST=5, FEW=1)
        self.event("FAST", 50, 5, 1)             # would be about 0.3 days left, but only 3 days of history
        self.assertEqual(self.names(self.get("needs", section="low")["rows"]), ["FEW"])

    def test_low_sorting_is_by_days_left_then_quantity(self):
        self.tracking_since(10)
        self.in_stock(A=20, B=10, C=1, D=2)
        self.event("A", 40, 20, 1)               # 2/day -> 10 days
        self.event("B", 30, 10, 1)               # 2/day -> 5 days
        self.assertEqual(self.names(self.get("needs", section="low")["rows"]), ["B", "A", "C", "D"])

    def test_settings_change_what_is_low(self):
        self.tracking_since(10)
        self.in_stock(SLOW=100)
        self.event("SLOW", 110, 100, 2)          # 100 days left
        self.assertEqual(self.get("needs", section="low")["rows"], [])
        self.post("/settings", {"low_days": 120})
        self.assertEqual(self.names(self.get("needs", section="low")["rows"]), ["SLOW"])

    def test_suggested_quantity_covers_the_cover_days(self):
        self.tracking_since(10)
        self.in_stock(FAST=5, FEW=1)
        self.event("FAST", 55, 5, 1)             # 5/day; 30 days cover = 150; minus 5 in stock
        rows = {r["item_name"]: r for r in self.get("needs", section="low")["rows"]}
        self.assertEqual(rows["FAST"]["suggested_qty"], 145)
        self.assertEqual(rows["FEW"]["suggested_qty"], 1)

    def test_open_order_shows_a_badge_and_the_row_stays(self):
        self.tracking_since(10)
        self.in_stock(FEW=1)
        self.sql("INSERT INTO restock_orders (item_key, item_name, qty_ordered, ordered_at_utc) VALUES ('FEW', 'FEW', 5, '2026-03-17T05:00:00')")
        row = self.get("needs", section="low")["rows"][0]
        self.assertEqual(row["open_order"]["ordered_on"], "2026-03-17")

    def test_out_of_stock_relevant_items_only_unless_the_switch_is_on(self):
        self.tracking_since(10)
        self.in_stock(**{"CARB_ITEM_001": 1, "CARB_ITEM_002": 1, "CARB_ITEM_003": 1})
        self.event("CARC ITEM 001", 4, 0, 2)                        # sold out and gone: relevant
        self.opens("CARA", "dealer", 1, 3)                          # dealers open CARA: its items are relevant
        rows = self.get("needs", section="out")["rows"]
        self.assertEqual(sorted(self.names(rows)), ["CARA ITEM 001", "CARA ITEM 002", "CARA ITEM 003", "CARC ITEM 001"])
        everything = self.get("needs", section="out", show_all=1)
        self.assertEqual(everything["total"], 6)                    # CARA x3 + CARC x3 are out of stock (CARB is in stock)

    def test_out_of_stock_sorted_by_selling_speed_then_interest(self):
        self.tracking_since(10)
        self.in_stock(**{"CARB_ITEM_001": 1})
        self.event("CARC ITEM 001", 10, 0, 2)                       # sold 10
        self.event("CARC ITEM 002", 30, 0, 2)                       # sold 30: faster
        self.opens("CARA", "dealer", 1, 5)
        rows = self.get("needs", section="out")["rows"]
        self.assertEqual(self.names(rows)[:2], ["CARC ITEM 002", "CARC ITEM 001"])

    def test_catalogue_search_and_the_100_row_cap(self):
        pushed = self.anon.post("/admin/intake/sync_data", json=catalogue_payload(["BIGCAR"], 250), headers=INTAKE_HEADERS)
        self.assertEqual(pushed.status_code, 200)
        self.sql("DELETE FROM stock_state")                          # the push put every item in stock; make them all out of stock
        self.app_module._stock_mgmt_cache_clear()
        first = self.get("needs", section="out", show_all=1)
        self.assertLessEqual(len(first["rows"]), 100)
        self.assertTrue(first["more"])
        total = first["total"]
        last = self.get("needs", section="out", show_all=1, page=3)
        self.assertEqual(len(last["rows"]), total - 200)
        self.assertFalse(last["more"])
        found = self.get("needs", section="out", show_all=1, q="bigcar item 250")
        self.assertEqual(self.names(found["rows"]), ["BIGCAR ITEM 250"])

    def test_search_text_too_long_is_refused(self):
        response = self.admin.get(BASE + "/data", query_string={"tab": "needs", "section": "out", "q": "x" * 81})
        self.assertEqual(response.status_code, 400)

    def test_banner_says_still_collecting_under_7_days(self):
        self.tracking_since(3)
        data = self.get("needs", section="low")
        self.assertEqual(data["tracking"]["stock_days"], 3)
        self.assertIn("Still collecting data", data["tracking"]["banner"])
        self.assertIn("2026-03-17", data["tracking"]["banner"])
        self.tracking_since(10)
        self.app_module._stock_mgmt_cache_clear()
        self.assertNotIn("Still collecting data", self.get("needs", section="low")["tracking"]["banner"])


class OrderList(StockMgmtBase):
    def add(self, name, qty, note=None):
        body = {"action": "add", "item_name": name, "qty": qty}
        if note is not None:
            body["note"] = note
        return self.post("/draft", body)

    def draft(self):
        return self.get("order")

    def test_add_edit_remove(self):
        self.assertEqual(self.add("CARA ITEM 001", 5, "red").status_code, 200)
        data = self.draft()
        self.assertEqual(data["total_items"], 1)
        line = data["groups"][0]["items"][0]
        self.assertEqual((line["item_name"], line["qty"], line["note"]), ("CARA ITEM 001", 5, "red"))
        self.assertEqual(self.post("/draft", {"action": "update", "item_key": "CARA ITEM 001", "qty": 9, "note": "blue"}).status_code, 200)
        line = self.draft()["groups"][0]["items"][0]
        self.assertEqual((line["qty"], line["note"]), (9, "blue"))
        self.assertEqual(self.post("/draft", {"action": "remove", "item_key": "CARA ITEM 001"}).status_code, 200)
        self.assertEqual(self.draft()["total_items"], 0)

    def test_adding_the_same_item_twice_keeps_one_line(self):
        self.add("CARA ITEM 001", 5)
        self.add("cara  item 001", 7)
        data = self.draft()
        self.assertEqual(data["total_items"], 1)
        self.assertEqual(data["groups"][0]["items"][0]["qty"], 7)

    def test_unknown_items_and_bad_input_are_refused(self):
        self.assertEqual(self.add("NO SUCH ITEM", 5).status_code, 400)
        for qty in (0, -1, 100001, "x", 1.5, True, None):
            self.assertEqual(self.add("CARA ITEM 001", qty).status_code, 400, qty)
        self.assertEqual(self.add("CARA ITEM 001", 5, "n" * 201).status_code, 400)
        self.assertEqual(self.add("CARA ITEM 001", 100000, "n" * 200).status_code, 200)
        self.assertEqual(self.post("/draft", {"action": "bogus"}).status_code, 400)
        self.assertEqual(self.post("/draft", {"action": "update", "item_key": "NOT THERE", "qty": 2}).status_code, 404)

    def test_grouping_by_category_with_other_for_items_not_in_the_catalogue(self):
        self.db.upsert_design_category("CARA ITEM 001", self.db.get_all_categories()[0]["name"], 1)
        self.in_stock(**{"STOCK_ONLY_THING": 1})                    # known from stock history, not in the hierarchy
        self.add("CARA ITEM 001", 5)
        self.add("CARB ITEM 002", 3)
        self.add("STOCK ONLY THING", 2)
        groups = {g["name"]: self.names(g["items"]) for g in self.draft()["groups"]}
        self.assertEqual(groups[self.db.get_all_categories()[0]["name"]], ["CARA ITEM 001"])
        self.assertEqual(groups["No category yet"], ["CARB ITEM 002"])
        self.assertEqual(groups["Other"], ["STOCK ONLY THING"])
        self.assertEqual([g["name"] for g in self.draft()["groups"]][-1], "Other")   # Other always last

    def test_copy_text_is_exact(self):
        category = self.db.get_all_categories()[0]["name"]
        self.db.upsert_design_category("CARA ITEM 001", category, 1)
        self.add("CARA ITEM 001", 5, "dark")
        self.add("CARB ITEM 002", 3)
        self.add("CARB ITEM 001", 12)
        expected = (f"Order list - 20 Mar 2026\n\n{category}\nCARA ITEM 001 - 5 (dark)\n\n"
                    "No category yet\nCARB ITEM 001 - 12\nCARB ITEM 002 - 3")
        self.assertEqual(self.draft()["text"], expected)

    def test_mark_all_as_ordered_moves_rows_in_one_step(self):
        self.add("CARA ITEM 001", 5)
        self.add("CARB ITEM 002", 3)
        response = self.post("/orders", {"action": "place", "all": True, "expected_date": "2026-04-01"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.draft()["total_items"], 0)
        orders = self.rows("SELECT item_key, qty_ordered, status, expected_date FROM restock_orders ORDER BY item_key")
        self.assertEqual(orders, [("CARA ITEM 001", 5, "ordered", "2026-04-01"), ("CARB ITEM 002", 3, "ordered", "2026-04-01")])

    def test_mark_ticked_as_ordered_leaves_the_rest(self):
        self.add("CARA ITEM 001", 5)
        self.add("CARB ITEM 002", 3)
        self.assertEqual(self.post("/orders", {"action": "place", "item_keys": ["CARA ITEM 001"]}).status_code, 200)
        self.assertEqual(self.names(self.draft()["groups"][0]["items"]), ["CARB ITEM 002"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM restock_orders"), [(1,)])

    def test_ordering_an_item_that_already_has_an_open_order_asks_first(self):
        self.add("CARA ITEM 001", 5)
        self.post("/orders", {"action": "place", "all": True})
        self.add("CARA ITEM 001", 2)
        response = self.post("/orders", {"action": "place", "all": True})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["needs_confirm"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM restock_orders"), [(1,)])      # nothing changed
        self.assertEqual(self.draft()["total_items"], 1)
        self.assertTrue(self.draft()["groups"][0]["items"][0]["has_open_order"])
        self.assertEqual(self.post("/orders", {"action": "place", "all": True, "confirm_duplicates": True}).status_code, 200)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM restock_orders"), [(2,)])

    def test_nothing_to_order_and_bad_dates(self):
        self.assertEqual(self.post("/orders", {"action": "place", "all": True}).status_code, 400)
        self.add("CARA ITEM 001", 5)
        for bad in ("2026-13-01", "01-04-2026", "2019-12-31", "soon", 5):
            self.assertEqual(self.post("/orders", {"action": "place", "all": True, "expected_date": bad}).status_code, 400, bad)
        self.assertEqual(self.draft()["total_items"], 1)

    def test_items_already_on_the_order_list_are_flagged_on_needs_restocking(self):
        self.add("CARA ITEM 001", 5)
        rows = {r["item_key"]: r for r in self.get("needs", section="out", show_all=1)["rows"]}
        self.assertTrue(rows["CARA ITEM 001"]["on_order_list"])


class OrderedTracker(StockMgmtBase):
    def make_order(self, name, qty, ordered="2026-03-10T05:00:00", expected=None):
        self.sql("INSERT INTO restock_orders (item_key, item_name, qty_ordered, ordered_at_utc, expected_date) VALUES (?, ?, ?, ?, ?)",
                 (name.upper(), name, qty, ordered, expected))
        return self.rows("SELECT MAX(id) FROM restock_orders")[0][0]

    def status(self, order_id):
        return self.rows("SELECT status, qty_received FROM restock_orders WHERE id = ?", (order_id,))[0]

    def test_a_restock_marks_the_oldest_open_order_received(self):
        first = self.make_order("CARA ITEM 001", 10, "2026-03-01T05:00:00")
        second = self.make_order("CARA ITEM 001", 10, "2026-03-05T05:00:00")
        self.app_module._capture_stock_changes([{"item_name": "CARA ITEM 001", "qty": 2}])        # baseline
        self.app_module._capture_stock_changes([{"item_name": "CARA ITEM 001", "qty": 7}])        # +5: less than ordered, still received
        self.assertEqual(self.status(first), ("received", 5))
        self.assertEqual(self.status(second), ("ordered", None))

    def test_a_decrease_does_not_receive_anything(self):
        order = self.make_order("CARA ITEM 001", 10)
        self.app_module._capture_stock_changes([{"item_name": "CARA ITEM 001", "qty": 9}])
        self.app_module._capture_stock_changes([{"item_name": "CARA ITEM 001", "qty": 4}])
        self.assertEqual(self.status(order), ("ordered", None))

    def test_an_item_coming_back_from_zero_counts_as_received(self):
        order = self.make_order("CARA ITEM 001", 10)
        self.app_module._capture_stock_changes([{"item_name": "OTHER", "qty": 1}])
        self.app_module._capture_stock_changes([{"item_name": "OTHER", "qty": 1}, {"item_name": "CARA ITEM 001", "qty": 12}])
        self.assertEqual(self.status(order), ("received", 12))

    def test_through_the_intake_route(self):
        order = self.make_order("CARA ITEM 001", 10)
        self.app_module._capture_stock_changes([{"item_name": "CARA ITEM 001", "qty": 1}, {"item_name": "CARB ITEM 001", "qty": 1}])
        payload = catalogue_payload(self.cars, self.items_per_car)
        payload["item_stock"][0]["qty"] = 6                          # CARA ITEM 001: 1 -> 6
        self.assertEqual(self.anon.post("/admin/intake/sync_data", json=payload, headers=INTAKE_HEADERS).status_code, 200)
        self.assertEqual(self.status(order), ("received", 5))

    def test_open_list_age_overdue_and_recent_received(self):
        overdue = self.make_order("CARA ITEM 001", 10, "2026-03-10T05:00:00", "2026-03-15")
        fine = self.make_order("CARA ITEM 002", 4, "2026-03-18T05:00:00", "2026-04-01")
        self.make_order("CARA ITEM 003", 4, "2026-03-19T05:00:00")
        done = self.make_order("CARB ITEM 001", 6, "2026-03-01T05:00:00")
        self.sql("UPDATE restock_orders SET status='received', received_at_utc='2026-03-18T05:00:00', qty_received=6 WHERE id=?", (done,))
        old = self.make_order("CARB ITEM 002", 6, "2026-01-01T05:00:00")
        self.sql("UPDATE restock_orders SET status='received', received_at_utc='2026-01-05T05:00:00', qty_received=6 WHERE id=?", (old,))
        data = self.get("ordered")
        rows = data["open"]
        self.assertEqual(rows[0]["id"], overdue)                     # overdue first
        self.assertTrue(rows[0]["overdue"])
        self.assertEqual(rows[0]["age_days"], 10)
        self.assertEqual(rows[0]["ordered_on"], "2026-03-10")
        self.assertFalse(next(r for r in rows if r["id"] == fine)["overdue"])
        self.assertEqual([r["item_name"] for r in data["recent"]], ["CARB ITEM 001"])     # 30 days only

    def test_mark_received_cancel_edit_date(self):
        a, b, c = (self.make_order(f"CARA ITEM 00{i}", 5, expected="2026-03-25") for i in (1, 2, 3))
        self.assertEqual(self.post("/orders", {"action": "receive", "id": a}).status_code, 200)
        self.assertEqual(self.status(a), ("received", 5))
        self.assertEqual(self.post("/orders", {"action": "receive", "id": b, "qty_received": 3}).status_code, 200)
        self.assertEqual(self.status(b), ("received", 3))
        self.assertEqual(self.post("/orders", {"action": "cancel", "id": c}).status_code, 200)
        self.assertEqual(self.status(c)[0], "cancelled")
        d = self.make_order("CARB ITEM 001", 5, expected="2026-03-25")
        self.assertEqual(self.post("/orders", {"action": "expected", "id": d, "expected_date": "2026-04-30"}).status_code, 200)
        self.assertEqual(self.rows("SELECT expected_date FROM restock_orders WHERE id=?", (d,)), [("2026-04-30",)])
        self.assertEqual(self.post("/orders", {"action": "receive", "id": a}).status_code, 409)       # already received
        self.assertEqual(self.post("/orders", {"action": "receive", "id": 99999}).status_code, 404)

    def test_undo_the_last_action(self):
        order = self.make_order("CARA ITEM 001", 5)
        self.post("/orders", {"action": "receive", "id": order})
        self.assertEqual(self.post("/orders", {"action": "undo"}).status_code, 200)
        self.assertEqual(self.status(order), ("ordered", None))
        self.assertEqual(self.post("/orders", {"action": "undo"}).status_code, 409)       # only the last action
        self.post("/orders", {"action": "cancel", "id": order})
        self.post("/orders", {"action": "undo"})
        self.assertEqual(self.status(order)[0], "ordered")

    def test_undo_of_an_order_placement_restores_the_order_list(self):
        self.post("/draft", {"action": "add", "item_name": "CARA ITEM 001", "qty": 5, "note": "dark"})
        self.post("/orders", {"action": "place", "all": True})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM restock_orders"), [(1,)])
        self.assertEqual(self.post("/orders", {"action": "undo"}).status_code, 200)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM restock_orders"), [(0,)])
        self.assertEqual(self.rows("SELECT item_key, qty, note FROM restock_draft"), [("CARA ITEM 001", 5, "dark")])

    def test_bad_input(self):
        for body in ({"action": "receive"}, {"action": "receive", "id": "x"}, {"action": "receive", "id": 1, "qty_received": -1},
                     {"action": "expected", "id": 1, "expected_date": "nope"}, {"action": "bogus"}):
            self.assertEqual(self.post("/orders", body).status_code, 400, body)


class SlowStock(StockMgmtBase):
    def test_short_history_shows_a_message_not_a_list(self):
        self.tracking_since(20)
        self.in_stock(IDLE=5)
        data = self.get("slow", days=60)
        self.assertTrue(data["insufficient_history"])
        self.assertNotIn("rows", data)
        self.assertEqual(data["message"], "Tracking only since 2026-02-28 (20 days). This list will appear after 2026-04-29.")

    def test_threshold_logic(self):
        self.tracking_since(100)
        self.in_stock(IDLE=5, SELLING=5, JUST_SOLD=5, NEW_ARRIVAL=5, RESTOCKED_IDLE=9)
        self.event("SELLING", 6, 5, 10)               # decreased 10 days ago: not slow
        self.event("JUST SOLD", 6, 5, 59)             # 59 days ago: not slow at 60
        self.event("NEW ARRIVAL", 0, 5, 80)           # came into stock 80 days ago, never sold: slow (80 days)
        self.event("RESTOCKED IDLE", 3, 9, 5)         # a restock is not a decrease: still sitting
        self.event("RESTOCKED IDLE", 4, 3, 75)        # last decrease 75 days ago
        rows = self.get("slow", days=60)["rows"]
        by_name = {r["item_name"]: r for r in rows}
        self.assertEqual(sorted(by_name), ["IDLE", "NEW ARRIVAL", "RESTOCKED IDLE"])
        self.assertEqual(by_name["IDLE"]["days_sitting"], 100)       # nothing since tracking began
        self.assertIsNone(by_name["IDLE"]["last_decrease"])
        self.assertEqual(by_name["NEW ARRIVAL"]["days_sitting"], 80)
        self.assertEqual(by_name["RESTOCKED IDLE"]["days_sitting"], 75)
        self.assertEqual(by_name["RESTOCKED IDLE"]["last_decrease"], day(75))
        self.assertEqual(self.names(rows)[0], "IDLE")                # longest sitting first

    def test_90_day_selector_and_out_of_stock_items_are_never_listed(self):
        self.tracking_since(100)
        self.in_stock(IDLE=5, OLDER=5)
        self.event("OLDER", 6, 5, 95)
        self.event("GONE", 6, 0, 95)                                 # sold out: not in stock now
        self.assertEqual(sorted(self.names(self.get("slow", days=90)["rows"])), ["IDLE", "OLDER"])
        self.event("IDLE", 7, 5, 70)
        self.app_module._stock_mgmt_cache_clear()
        self.assertEqual(self.names(self.get("slow", days=90)["rows"]), ["OLDER"])

    def test_only_60_or_90_days_are_accepted(self):
        self.assertEqual(self.admin.get(BASE + "/data", query_string={"tab": "slow", "days": 30}).status_code, 400)


class AskedFor(StockMgmtBase):
    def test_counts_by_role_with_the_admin_switch(self):
        self.interest_since(20)
        self.in_stock(**{"CARB_ITEM_001": 1})                       # CARB: 1 of 3 in stock
        self.opens("CARA", "dealer", 11, 2, 1)
        self.opens("CARA", "dealer", 12, 3, 3)
        self.opens("CARA", "dealer", 12, 4, 20)                      # 20 days ago: in the 30-day count, not the 7-day
        self.opens("CARA", "admin", 1, 6, 2)
        data = self.get("asked", max_items=5)
        row = next(r for r in data["cars"] if r["car_key"] == "CARA")
        self.assertEqual((row["dealer_opens_7"], row["dealer_opens_30"]), (5, 9))
        self.assertEqual((row["admin_opens_7"], row["admin_opens_30"]), (6, 6))
        self.assertEqual((row["total_7"], row["total_30"]), (11, 15))
        self.assertEqual(row["dealers"], 2)
        self.assertEqual((row["in_stock"], row["out_of_stock"]), (0, 3))
        without = self.get("asked", max_items=5, include_admin=0)
        row = next(r for r in without["cars"] if r["car_key"] == "CARA")
        self.assertEqual((row["total_7"], row["total_30"]), (5, 9))

    def test_least_stock_first_then_most_opens_and_the_max_items_filter(self):
        self.interest_since(20)
        self.in_stock(**{"CARB_ITEM_001": 1, "CARB_ITEM_002": 1, "CARC_ITEM_001": 1, "CARC_ITEM_002": 1, "CARC_ITEM_003": 1})
        self.opens("CARB", "dealer", 11, 50)
        self.opens("CARA", "dealer", 11, 2)
        self.opens("CARC", "dealer", 11, 80)
        self.assertEqual([r["car_key"] for r in self.get("asked", max_items=3)["cars"]], ["CARA", "CARB", "CARC"])   # 0, 2, 3 in stock
        self.assertEqual([r["car_key"] for r in self.get("asked", max_items=2)["cars"]], ["CARA", "CARB"])
        self.assertEqual([r["car_key"] for r in self.get("asked", max_items=0)["cars"]], ["CARA"])
        self.opens("CARB", "dealer", 12, 60)
        self.app_module._stock_mgmt_cache_clear()
        self.in_stock(**{"CARB_ITEM_003": 1})
        everything = self.get("asked", max_items=0, show_all=1)
        self.assertEqual([r["car_key"] for r in everything["cars"]], ["CARA", "CARB", "CARC"])

    def test_saved_default_is_used_when_the_switch_is_not_sent(self):
        self.interest_since(20)
        self.opens("CARA", "dealer", 11, 1)
        self.opens("CARA", "admin", 1, 10)
        self.post("/settings", {"include_admin": 0})
        row = self.get("asked")["cars"][0]
        self.assertEqual(row["total_30"], 1)

    def test_out_of_stock_items_of_a_car(self):
        self.interest_since(20)
        self.in_stock(**{"CARA_ITEM_002": 1})
        self.opens("CARA", "dealer", 11, 1)
        data = self.get("asked", block="car_items", car="CARA")
        self.assertEqual(self.names(data["items"]), ["CARA ITEM 001", "CARA ITEM 003"])
        self.assertIn("suggested_qty", data["items"][0])

    def test_searched_but_not_found(self):
        self.interest_since(20)
        self.sql("INSERT INTO search_miss_daily VALUES (?, 'zzz car', 'dealer', 4)", (day(1),))
        self.sql("INSERT INTO search_miss_daily VALUES (?, 'zzz car', 'admin', 3)", (day(2),))
        self.sql("INSERT INTO search_miss_daily VALUES (?, 'qqq car', 'dealer', 1)", (day(1),))
        self.sql("INSERT INTO search_miss_daily VALUES (?, 'old car', 'dealer', 99)", (day(45),))
        terms = self.get("asked", block="misses")["terms"]
        self.assertEqual([(t["term"], t["dealer"], t["admin"], t["total"]) for t in terms], [("zzz car", 4, 3, 7), ("qqq car", 1, 0, 1)])
        terms = self.get("asked", block="misses", include_admin=0)["terms"]
        self.assertEqual([(t["term"], t["total"]) for t in terms], [("zzz car", 4), ("qqq car", 1)])

    def test_tracking_started_message(self):
        self.interest_since(2)
        data = self.get("asked")
        self.assertEqual(data["tracking"]["interest_since"], "2026-03-18")
        self.assertIn("Tracking started on 2026-03-18", data["tracking"]["interest_banner"])

    def test_only_counts_are_returned_never_people(self):
        self.interest_since(20)
        self.opens("CARA", "dealer", 11, 2)
        self.opens("CARA", "admin", 1, 1)
        self.sql("INSERT INTO search_miss_daily VALUES (?, 'zzz car', 'dealer', 4)", (day(1),))
        cars = self.get("asked", max_items=5)
        misses = self.get("asked", block="misses")
        allowed = {"car_key", "car_name", "dealer_opens_7", "dealer_opens_30", "admin_opens_7", "admin_opens_30",
                   "total_7", "total_30", "dealers", "in_stock", "out_of_stock"}
        for row in cars["cars"]:
            self.assertEqual(set(row), allowed)
        for row in misses["terms"]:
            self.assertEqual(set(row), {"term", "dealer", "admin", "total"})
        text = json.dumps([cars, misses]).lower()
        for forbidden in ('"star"', "jeewajee", "username", "user_id"):
            self.assertNotIn(forbidden, text)


class AdminOnly(StockMgmtBase):
    def new_routes(self):
        found = []
        for rule in self.app_module.app.url_map.iter_rules():
            if rule.rule.startswith(BASE):
                for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                    found.append((method, rule.rule))
        return found

    def test_every_new_route_is_closed_to_dealers_and_logged_out_visitors(self):
        routes = self.new_routes()
        self.assertGreaterEqual(len(routes), 5)
        self.assertIn(("GET", BASE), routes)
        for client, allowed in ((self.dealer, (403,)), (self.anon, (401, 403))):   # logged-out visitors are stopped even earlier, with 401
            for method, path in routes:
                response = client.open(path, method=method, json={"action": "add", "item_name": "CARA ITEM 001", "qty": 1} if method == "POST" else None,
                                       query_string={"tab": "needs"} if method == "GET" else None)
                self.assertIn(response.status_code, allowed, f"{method} {path}")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM restock_draft"), [(0,)])       # and nothing was written

    def test_admin_can_open_everything(self):
        for method, path in self.new_routes():
            if method == "GET":
                response = self.admin.get(path, query_string={"tab": "needs", "section": "low"})
                self.assertEqual(response.status_code, 200, path)

    def test_the_page_is_a_page(self):
        response = self.admin.get(BASE)
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for tab in ("Needs restocking", "Order list", "Ordered", "Slow stock", "Asked for"):
            self.assertIn(tab, text)
        self.assertNotIn("setInterval", text)                        # no new polling timers

    def test_data_route_rejects_unknown_tabs(self):
        self.assertEqual(self.admin.get(BASE + "/data", query_string={"tab": "nope"}).status_code, 400)

    def test_more_menu_entry_is_for_admins_only(self):
        admin_page = self.admin.get("/").get_data(as_text=True)
        dealer_page = self.dealer.get("/").get_data(as_text=True)
        self.assertIn("Stock Management", admin_page)
        self.assertIn(BASE, admin_page)
        self.assertNotIn("Stock Management", dealer_page)
        self.assertNotIn(BASE, dealer_page)

    def test_intake_still_answers_200(self):
        response = self.anon.post("/admin/intake/sync_data", json=catalogue_payload(self.cars, self.items_per_car), headers=INTAKE_HEADERS)
        self.assertEqual(response.status_code, 200)


class Performance(StockMgmtBase):
    def test_each_tab_is_fast_with_production_sized_history(self):
        import time
        self.tracking_since(60)
        with self.db.get_connection() as conn:
            conn.executemany("INSERT INTO stock_state VALUES (?, ?, ?, 'x')", [(f"ITEM {i}", f"ITEM {i}", 5) for i in range(2800)])
            conn.executemany("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES (?, ?, ?, ?, ?, 'x')",
                             [(day(i % 30), f"ITEM {i % 2800}", f"ITEM {i % 2800}", 6, 5) for i in range(30000)])
            conn.executemany("INSERT OR IGNORE INTO car_interest_daily VALUES (?, ?, 'dealer', ?, 2)",
                             [(day(i % 30), f"CAR {i % 700}", i % 40) for i in range(20000)])
        timings = []
        for label, params in (("needs/low", {"tab": "needs", "section": "low"}), ("needs/out", {"tab": "needs", "section": "out", "show_all": 1}),
                              ("order", {"tab": "order"}), ("ordered", {"tab": "ordered"}), ("slow", {"tab": "slow", "days": 60}),
                              ("asked", {"tab": "asked", "show_all": 1}), ("asked/misses", {"tab": "asked", "block": "misses"})):
            self.app_module._stock_mgmt_cache_clear()
            start = time.perf_counter()
            response = self.admin.get(BASE + "/data", query_string=params)
            elapsed = time.perf_counter() - start
            self.assertEqual(response.status_code, 200, label)
            timings.append(f"{label} {elapsed * 1000:.0f} ms")
            self.assertLess(elapsed, 2.0, label)
        print("\n  stock management timings: " + ", ".join(timings))


if __name__ == "__main__":
    unittest.main()
