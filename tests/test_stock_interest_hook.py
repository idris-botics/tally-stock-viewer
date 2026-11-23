"""The after-request interest hook: which requests reach the database, what happens when the database is locked, that
only a 200 answer is counted, that no file is touched, and what it costs. Measured numbers are printed."""
import builtins
import sqlite3
import time
import unittest
from unittest import mock

import flask

from tests import support
from tests.support import INTAKE_HEADERS

CARS = 60


def payload():
    hierarchy, stock, names = [], [], []
    for i in range(CARS):
        car = f"HCAR{i:03d}"
        names.append(car)
        hierarchy.append({"item_name": car, "qty": 2})
        for n in (1, 2):
            hierarchy.append({"item_name": f"{car} ITEM {n}", "qty": 1})
            stock.append({"item_name": f"{car} ITEM {n}", "qty": 1})
        hierarchy.append({"item_name": "", "qty": ""})
    return {"car_master": names, "main_hierarchy": hierarchy, "item_stock": stock}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")
        self.assertEqual(self.anon.post("/admin/intake/sync_data", json=payload(), headers=INTAKE_HEADERS).status_code, 200)
        with self.db.get_connection() as conn:
            for table in ("car_interest_daily", "search_miss_daily", "capture_meta"):
                conn.execute(f"DELETE FROM {table}")
        self.app_module._INTEREST_RECENT.clear()
        self.app_module._SEARCH_MISS_LAST.clear()

    def tearDown(self):
        support.restore_state(self.snapshot)

    def count(self, table="car_interest_daily"):
        with self.db.get_connection() as conn:
            return conn.execute(f"SELECT COUNT(*), COALESCE(SUM({'opens' if table == 'car_interest_daily' else 'count'}), 0) FROM {table}").fetchone()


class HookEarlyReturns(Base):
    def test_only_car_and_search_requests_reach_the_recording_code(self):
        with mock.patch.object(self.app_module, "_record_car_open") as opened, \
                mock.patch.object(self.app_module, "_record_search_miss") as missed:
            for path in ("/static/logo.jpg", "/static/shared.js", "/last_update", "/health", "/cars", "/", "/api/get_stock_items_for_car?car=HCAR001",
                         "/api/search_cars?q=hcar", "/designs", "/designs?car=NOT+A+CAR", "/mapping_stats", "/api/activity"):
                self.dealer.get(path)
            self.anon.post("/admin/intake/sync_data", json=payload(), headers=INTAKE_HEADERS)
            self.assertEqual(opened.call_count, 0)
            self.assertEqual(missed.call_count, 0)
            self.dealer.get("/designs", query_string={"car": "HCAR001"})        # the one kind that must reach it
            self.assertEqual(opened.call_count, 1)
            self.dealer.get("/api/search_cars", query_string={"q": "nosuchcar"})
            self.assertEqual(missed.call_count, 1)

    def test_a_request_that_is_not_for_a_car_or_search_never_opens_a_database_connection_in_the_hook(self):
        calls = []
        real = self.db.get_connection
        hook = self.app_module._record_stock_interest

        def watching(response):
            before = len(calls)
            with mock.patch.object(self.db, "get_connection", side_effect=lambda: (calls.append(1), real())[1]):
                result = hook(response)
            watching.used.append(len(calls) - before)
            return result
        watching.used = []
        funcs = self.app_module.app.after_request_funcs[None]
        index = funcs.index(hook)
        funcs[index] = watching
        try:
            self.dealer.get("/static/logo.jpg")
            self.dealer.get("/last_update")
            self.dealer.get("/cars")
            self.anon.post("/admin/intake/sync_data", json=payload(), headers=INTAKE_HEADERS)
            self.assertEqual(watching.used, [0, 0, 0, 0])
        finally:
            funcs[index] = hook

    def _hook_with(self, status, path="/designs?car=HCAR001", role="customer"):
        app = self.app_module.app
        with app.test_request_context(path):
            flask.session["user_id"] = 1
            flask.session["role"] = role
            with mock.patch.object(self.app_module, "_record_car_open") as opened:
                response = app.response_class("[]", status=status)
                returned = self.app_module._record_stock_interest(response)
                self.assertIs(returned, response)
                return opened.call_count

    def test_only_a_200_answer_is_counted(self):
        self.assertEqual(self._hook_with(200), 1)
        for status in (204, 301, 302, 304, 400, 401, 403, 404, 500):
            self.assertEqual(self._hook_with(status), 0, status)


class HookNeverBreaksTheResponse(Base):
    def test_a_locked_database_does_not_change_the_answer(self):
        expected = self.dealer.get("/designs", query_string={"car": "HCAR002"}).get_data()
        self.app_module._INTEREST_RECENT.clear()
        recorded_before = self.count()[0]                       # the first request above recorded its open
        holder = sqlite3.connect(self.db.DB_PATH, timeout=1)
        holder.execute("BEGIN IMMEDIATE")                       # another writer (like a feeder push) holds the write lock
        try:
            with mock.patch.object(self.app_module.Config, "DB_TIMEOUT", 0.2):
                start = time.perf_counter()
                response = self.dealer.get("/designs", query_string={"car": "HCAR003"})
                waited = time.perf_counter() - start
                missed = self.dealer.get("/api/search_cars", query_string={"q": "nosuchcar"})
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(missed.status_code, 200)
        self.assertEqual(self.dealer.get("/designs", query_string={"car": "HCAR002"}).get_data(), expected)
        self.assertLess(waited, 3.0)                            # it waited at most the timeout (a few times over), never forever
        self.assertEqual(self.count()[0], recorded_before)      # the open was lost, not the page

    def test_a_connection_error_does_not_change_the_answer(self):
        with mock.patch.object(self.app_module, "_record_car_open", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(self.dealer.get("/designs", query_string={"car": "HCAR004"}).status_code, 200)
        with mock.patch.object(self.app_module, "_record_search_miss", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(self.dealer.get("/api/search_cars", query_string={"q": "nosuchcar"}).status_code, 200)

    def test_a_failing_hook_still_returns_the_same_body(self):
        expected = self.dealer.get("/designs", query_string={"car": "HCAR005"}).get_data()
        self.app_module._INTEREST_RECENT.clear()
        with mock.patch.object(self.app_module, "_known_car_keys", side_effect=RuntimeError("boom")):
            response = self.dealer.get("/designs", query_string={"car": "HCAR005"})
        self.assertEqual((response.status_code, response.get_data()), (200, expected))


class HookTouchesNoFiles(Base):
    def test_capture_and_interest_code_never_open_a_file(self):
        def refuse(*args, **kwargs):
            raise AssertionError(f"file opened: {args[:1]}")
        stock = [{"item_name": "A", "qty": 3}, {"item_name": "B", "qty": 2}]
        self.app_module._capture_stock_changes(stock)
        with mock.patch.object(builtins, "open", side_effect=refuse):
            self.app_module._capture_stock_changes([{"item_name": "A", "qty": 1}, {"item_name": "C", "qty": 2}])
            self.app_module._record_car_open(7, "dealer", "HCAR001")
            self.app_module._record_search_miss(7, "dealer", "nosuchcar")
        self.assertEqual(self.count()[0], 1)


class HookCost(Base):
    def get_all(self, cars, clear_each=False):
        start = time.perf_counter()
        for car in cars:
            if clear_each:
                self.app_module._INTEREST_RECENT.clear()
            self.assertEqual(self.dealer.get("/designs", query_string={"car": car}).status_code, 200)
        return (time.perf_counter() - start) * 1000 / len(cars)

    def test_cost_and_write_counts(self):
        names = [f"HCAR{i:03d}" for i in range(50)]
        writes = []
        real = self.db.get_connection

        class Counting:
            def __init__(self, conn): self._conn = conn
            def __getattr__(self, name): return getattr(self._conn, name)
            def execute(self, sql, *args):
                if "INSERT INTO car_interest_daily" in sql:
                    writes.append(1)
                return self._conn.execute(sql, *args)

        from contextlib import contextmanager

        @contextmanager
        def counting():
            with real() as conn:
                yield Counting(conn)
        with mock.patch.object(self.db, "get_connection", counting):
            self.get_all(names)                                   # 50 different cars
            first = len(writes)
            self.get_all(names)                                   # the same 50 again, inside 10 minutes
            second = len(writes) - first
        print(f"\n  hook database writes: first pass over 50 cars {first}, reopening the same 50 {second}")
        self.assertEqual((first, second), (50, 0))
        self.assertEqual(tuple(self.count()), (50, 50))

        hook = self.app_module._record_stock_interest
        funcs = self.app_module.app.after_request_funcs[None]
        cars_200 = [names[i % 50] for i in range(200)]
        self.get_all(cars_200)                                    # warm up
        results = {}
        for label in ("hook disabled", "hook enabled, mostly reloads (throttled)", "hook enabled, every request writes"):
            if label == "hook disabled":
                funcs.remove(hook)
            try:
                self.app_module._INTEREST_RECENT.clear()
                results[label] = self.get_all(cars_200, clear_each=(label.endswith("writes")))
            finally:
                if hook not in funcs:
                    funcs.append(hook)
        print("  /designs mean per request over 200 requests: " + "; ".join(f"{k}: {v:.2f} ms" for k, v in results.items()))
        self.assertLess(results["hook enabled, every request writes"] - results["hook disabled"], 50.0)

    def test_the_30_day_query_plan_and_growth(self):
        with self.db.get_connection() as conn:
            conn.executemany("INSERT OR REPLACE INTO car_interest_daily VALUES (?, ?, ?, ?, 2)",
                             [(f"2026-02-{1 + i % 28:02d}", f"CAR {i % 700}", "dealer" if i % 9 else "admin", i % 60) for i in range(40000)])
            conn.executemany("INSERT OR REPLACE INTO search_miss_daily VALUES (?, ?, 'dealer', 2)",
                             [(f"2026-02-{1 + i % 28:02d}", f"term {i}", ) for i in range(3000)])
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT car_key, SUM(CASE WHEN role = 'dealer' AND day_ist > ? THEN opens ELSE 0 END), "
                "SUM(CASE WHEN role = 'dealer' THEN opens ELSE 0 END), SUM(CASE WHEN role = 'admin' AND day_ist > ? THEN opens ELSE 0 END), "
                "SUM(CASE WHEN role = 'admin' THEN opens ELSE 0 END), COUNT(DISTINCT CASE WHEN role = 'dealer' THEN user_id END) "
                "FROM car_interest_daily WHERE day_ist > ? GROUP BY car_key", ("2026-02-20", "2026-02-20", "2026-02-01")).fetchall()
            plan2 = conn.execute(
                "EXPLAIN QUERY PLAN SELECT term, SUM(count) FROM search_miss_daily WHERE day_ist > ? GROUP BY term", ("2026-02-01",)).fetchall()
            rows = conn.execute("SELECT COUNT(*) FROM car_interest_daily").fetchone()[0]
        print("\n  EXPLAIN QUERY PLAN, Asked-for cars: " + " | ".join(str(tuple(r)[3]) for r in plan))
        print("  EXPLAIN QUERY PLAN, Asked-for misses: " + " | ".join(str(tuple(r)[3]) for r in plan2))
        self.assertTrue(any("SEARCH" in str(tuple(r)[3]) and "day_ist" in str(tuple(r)[3]) for r in plan))
        self.assertGreater(rows, 0)


if __name__ == "__main__":
    unittest.main()
