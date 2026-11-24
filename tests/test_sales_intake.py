"""Sales data intake (POST /admin/intake/sales), its storage, its status box and the pin that ties it to one Tally
company. The contract is fixed: the shop-side feeder is written against exactly this."""
import hashlib
import json
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import support
from tests.support import PAYLOAD, INTAKE_HEADERS

URL = "/admin/intake/sales"
H1 = hashlib.sha256(b"Super Seatings").hexdigest()
H2 = hashlib.sha256(b"Another Company").hexdigest()
FORCE = dict(INTAKE_HEADERS, **{"X-Intake-Force": "1"})
NOW = datetime(2026, 3, 20, 6, 30, tzinfo=timezone.utc)


def line(v="aaaaaaaa", d="2026-01-10", k="sale", i="ALTO 7D MAT BLACK", q=1):
    return {"v": v, "d": d, "k": k, "i": i, "q": q}


def body(lines, frm="2026-01-01", to="2026-01-31", company=H1):
    return {"company": company, "from": frm, "to": to, "lines": lines}


def many(count, day="2026-01-10", prefix="b"):
    """`count` sale lines, each its own voucher."""
    return [line(v=f"{prefix}{n:07x}", d=day, i="ALTO 7D MAT BLACK") for n in range(count)]


class SalesBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")
        self.admin = support.client_for("admin_locked")
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sales_lines")
            conn.execute("DELETE FROM capture_meta WHERE key LIKE 'sales_%' OR key = 'last_sales_prune_day_ist'")
        self.app_module._stock_mgmt_cache_clear()

    def tearDown(self):
        support.restore_state(self.snapshot)

    def push(self, payload, headers=INTAKE_HEADERS):
        return self.anon.post(URL, json=payload, headers=headers)

    def rows(self, sql="SELECT voucher_key, day, kind, item_key, qty FROM sales_lines ORDER BY day, voucher_key, kind, item_key", args=()):
        with self.db.get_connection() as conn:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]

    def meta(self, key):
        found = self.rows("SELECT value FROM capture_meta WHERE key = ?", (key,))
        return found[0][0] if found else None

    def at(self, year, month, day):
        return mock.patch.object(self.app_module, "_stock_now_utc", return_value=datetime(year, month, day, 6, 30, tzinfo=timezone.utc))


class StoreAndReplace(SalesBase):
    def test_a_valid_push_stores_the_lines(self):
        response = self.push(body([line(), line(v="bbbbbbbb", d="2026-01-12", i="SWIFT 7D MAT GREY", q=4)]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "from": "2026-01-01", "to": "2026-01-31", "deleted": 0, "inserted": 2})
        self.assertEqual(self.rows(), [("aaaaaaaa", "2026-01-10", "sale", "ALTO 7D MAT BLACK", 1),
                                       ("bbbbbbbb", "2026-01-12", "sale", "SWIFT 7D MAT GREY", 4)])

    def test_the_same_push_twice_leaves_the_same_data(self):
        payload = body([line(), line(v="bbbbbbbb", d="2026-01-12", k="return", i="SWIFT 7D MAT GREY", q=2)])
        self.push(payload)
        first = self.rows()
        second = self.push(payload)
        self.assertEqual(second.get_json()["deleted"], 2)
        self.assertEqual(second.get_json()["inserted"], 2)
        self.assertEqual(self.rows(), first)

    def test_replace_covers_only_the_range_and_leaves_other_days_alone(self):
        self.push(body([line(v="aaaaaaa1", d="2026-01-05"), line(v="aaaaaaa2", d="2026-01-20"), line(v="aaaaaaa3", d="2026-02-03")],
                       frm="2026-01-01", to="2026-02-28"))
        response = self.push(body([line(v="ccccccc1", d="2026-01-05", q=7)], frm="2026-01-01", to="2026-01-31"))
        self.assertEqual((response.status_code, response.get_json()["deleted"], response.get_json()["inserted"]), (200, 2, 1))
        self.assertEqual([(r[0], r[1], r[4]) for r in self.rows()], [("ccccccc1", "2026-01-05", 7), ("aaaaaaa3", "2026-02-03", 1)])

    def test_sale_and_return_are_stored_separately(self):
        self.push(body([line(k="sale", q=3), line(k="return", q=1)]))
        self.assertEqual([(r[2], r[4]) for r in self.rows()], [("return", 1), ("sale", 3)])

    def test_duplicate_lines_inside_one_request_are_summed(self):
        self.push(body([line(q=2), line(q=3), line(i="alto  7d mat black", q=4), line(k="return", q=1)]))
        self.assertEqual([(r[2], r[3], r[4]) for r in self.rows()], [("return", "ALTO 7D MAT BLACK", 1), ("sale", "ALTO 7D MAT BLACK", 9)])

    def test_the_item_key_is_the_cleaned_upper_case_name(self):
        self.push(body([line(i="  Alto   7D mat Black ")]))
        self.assertEqual(self.rows()[0][3], "ALTO 7D MAT BLACK")

    def test_whole_number_floats_are_accepted_like_the_stock_intake(self):
        self.assertEqual(self.push(body([line(q=3.0)])).status_code, 200)
        self.assertEqual(self.rows()[0][4], 3)

    def test_an_empty_push_with_nothing_stored_is_fine(self):
        response = self.push(body([]))
        self.assertEqual((response.status_code, response.get_json()["inserted"]), (200, 0))

    def test_the_stock_tables_and_the_dealer_pages_are_untouched(self):
        before = (self.dealer.get("/cars").get_data(), self.dealer.get("/designs", query_string={"car": "ALTO"}).get_data())
        with self.db.get_connection() as conn:
            stock_before = [tuple(r) for r in conn.execute("SELECT * FROM stock_state ORDER BY item_key").fetchall()]
        self.push(body([line()]))
        after = (self.dealer.get("/cars").get_data(), self.dealer.get("/designs", query_string={"car": "ALTO"}).get_data())
        self.assertEqual(before, after)
        with self.db.get_connection() as conn:
            self.assertEqual([tuple(r) for r in conn.execute("SELECT * FROM stock_state ORDER BY item_key").fetchall()], stock_before)

    def test_old_lines_are_pruned_once_per_ist_day_not_every_push(self):
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO sales_lines VALUES ('oldoldol', '2020-01-01', 'sale', 'OLD', 'OLD', 1)")
        with self.at(2026, 3, 20):
            self.push(body([line()]))
            self.assertEqual(self.rows("SELECT COUNT(*) FROM sales_lines WHERE day = '2020-01-01'"), [(0,)])
            self.assertEqual(self.meta("last_sales_prune_day_ist"), "2026-03-20")
            with self.db.get_connection() as conn:
                conn.execute("INSERT INTO sales_lines VALUES ('oldoldol', '2020-01-01', 'sale', 'OLD', 'OLD', 1)")
            self.push(body([line()]))
            self.assertEqual(self.rows("SELECT COUNT(*) FROM sales_lines WHERE day = '2020-01-01'"), [(1,)])      # same day: no prune
        with self.at(2026, 3, 21):
            self.push(body([line()]))
            self.assertEqual(self.rows("SELECT COUNT(*) FROM sales_lines WHERE day = '2020-01-01'"), [(0,)])

    def test_recent_lines_survive_the_prune(self):
        with self.at(2026, 3, 20):
            self.push(body([line(d="2026-01-10")]))
        self.assertEqual(len(self.rows()), 1)

    def test_the_last_sync_is_remembered(self):
        with self.at(2026, 3, 20):
            self.push(body([line()]))
        self.assertEqual((self.meta("sales_last_from"), self.meta("sales_last_to")), ("2026-01-01", "2026-01-31"))
        self.assertEqual(self.meta("sales_last_sync_utc"), "2026-03-20T06:30:00")

    def test_one_info_line_without_item_names_and_one_warning_when_refused(self):
        with mock.patch.object(self.app_module.logger, "info") as info:
            self.push(body([line(i="SECRET DESIGN NAME")]))
        messages = [c.args[0] % c.args[1:] for c in info.call_args_list if "Cloud sales intake" in str(c.args[0])]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0], "Cloud sales intake: 2026-01-01..2026-01-31, 1 lines (0 replaced)")
        self.assertNotIn("SECRET", messages[0])
        self.push(body(many(60)))
        with mock.patch.object(self.app_module.logger, "warning") as warning:
            self.assertEqual(self.push(body([])).status_code, 422)
        self.assertEqual(warning.call_count, 1)
        self.assertNotIn("ALTO", str(warning.call_args))


BAD = []


def bad(label, payload):
    BAD.append((label, payload))


for label, value in (("not 64 characters", "abc"), ("63 hex", "a" * 63), ("65 hex", "a" * 65), ("upper case", "A" * 64), ("not hex", "g" * 64), ("a number", 5), ("missing", None)):
    payload = body([line()], company=value)
    if value is None:
        del payload["company"]
    bad("company " + label, payload)
for label, key, value in (("from bad month", "frm", "2026-13-01"), ("from wrong format", "frm", "01-01-2026"), ("from a number", "frm", 20260101),
                          ("to bad day", "to", "2026-01-32"), ("to wrong format", "to", "31/01/2026"), ("to None", "to", None)):
    payload = body([line()])
    payload["from" if key == "frm" else "to"] = value
    bad(label, payload)
bad("from after to", body([line()], frm="2026-02-01", to="2026-01-01"))
bad("range over 400 days", body([line(d="2025-06-01")], frm="2025-01-01", to="2026-03-01"))
bad("line day before the range", body([line(d="2025-12-31")]))
bad("line day after the range", body([line(d="2026-02-01")]))
bad("line day wrong format", body([line(d="10-01-2026")]))
bad("line day missing", body([{k: v for k, v in line().items() if k != "d"}]))
for kind in ("refund", "Sale", "", None, 1):
    bad(f"kind {kind!r}", body([line(k=kind)]))
for qty in (0, -1, 1.5, True, False, "3", None, 1000001):
    bad(f"qty {qty!r}", body([line(q=qty)]))
for name in ("", "   ", "x" * 201, 5, None, ["a"]):
    bad(f"item {str(name)[:12]!r}", body([line(i=name)]))
for voucher in ("xyz12345", "abcdef1", "a" * 65, "ABCDEF12", "", 12345678, None):
    bad(f"voucher {str(voucher)[:10]!r}", body([line(v=voucher)]))
bad("lines is not a list", {"company": H1, "from": "2026-01-01", "to": "2026-01-31", "lines": "none"})
bad("lines is missing", {"company": H1, "from": "2026-01-01", "to": "2026-01-31"})
bad("a line is not an object", body([line(), "text"]))
bad("a line is a list", body([[1, 2, 3]]))
bad("too many lines", body(many(30001)))


class Validation(SalesBase):
    def test_every_invalid_body_is_refused_with_a_short_fixed_message_and_changes_nothing(self):
        for label, payload in BAD:
            response = self.push(payload)
            self.assertEqual(response.status_code, 422, label)
            data = response.get_json()
            self.assertTrue(data["refused"], label)
            self.assertLess(len(data["error"]), 120, label)
            for word in ("Traceback", "File ", "Error:", "\\", "/"):
                self.assertNotIn(word, data["error"], label)
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.meta("sales_company_hash"))

    def test_the_message_names_the_field_and_the_line_number(self):
        data = self.push(body([line(), line(q=0)])).get_json()
        self.assertIn("line 1", data["error"])
        self.assertIn("q must", data["error"])                      # the field is named the way the feeder sends it
        self.assertIn("company", self.push(body([line()], company="abc")).get_json()["error"])

    def test_the_messages_never_contain_the_item_name(self):
        data = self.push(body([line(i="SECRET DESIGN NAME", q=0)])).get_json()
        self.assertNotIn("SECRET", data["error"])

    def test_a_body_that_is_not_json_or_not_an_object_is_refused(self):
        for raw in ("not json at all", "[1, 2]", "5", '"text"', "null"):
            response = self.anon.post(URL, data=raw, content_type="application/json", headers=INTAKE_HEADERS)
            self.assertEqual(response.status_code, 422, raw)
            self.assertTrue(response.get_json()["refused"])

    def test_exactly_400_days_and_30000_lines_are_allowed(self):
        self.assertEqual(self.push(body([line(d="2025-01-01")], frm="2025-01-01", to="2026-02-05")).status_code, 200)   # 400 days apart
        self.assertEqual(self.push(body(many(30000)), headers=FORCE).status_code, 200)

    def test_an_oversized_body_is_answered_413_and_changes_nothing(self):
        big = body([line(i="X" * 150 + str(n)) for n in range(25000)])
        self.assertGreater(len(json.dumps(big)), 3 * 1024 * 1024)
        response = self.push(big)
        self.assertEqual(response.status_code, 413)
        self.assertTrue(response.get_json()["refused"])
        self.assertEqual(self.rows(), [])


class AuthPinAndGuards(SalesBase):
    def test_auth_failures_are_answered_exactly_like_the_stock_intake(self):
        for headers in ({}, {"X-Intake-Token": "wrong"}, {"X-Intake-Token": ""}):
            sales = self.anon.post(URL, json=body([line()]), headers=headers)
            stock = self.anon.post("/admin/intake/sync_data", json=PAYLOAD, headers=headers)
            self.assertEqual((sales.status_code, sales.get_json()), (stock.status_code, stock.get_json()), headers)
        with mock.patch.object(self.app_module.Config, "INTAKE_SYNC_TOKEN", ""):
            sales = self.anon.post(URL, json=body([line()]), headers=INTAKE_HEADERS)
            stock = self.anon.post("/admin/intake/sync_data", json=PAYLOAD, headers=INTAKE_HEADERS)
        self.assertEqual((sales.status_code, sales.get_json()), (stock.status_code, stock.get_json()))
        self.assertEqual(self.rows(), [])

    def test_a_dealer_session_is_not_a_token(self):
        self.assertEqual(self.dealer.post(URL, json=body([line()])).status_code, 403)
        self.assertEqual(self.admin.post(URL, json=body([line()])).status_code, 403)

    def test_the_first_push_pins_the_company_and_a_different_one_is_refused(self):
        self.push(body([line()]))
        self.assertEqual(self.meta("sales_company_hash"), H1)
        before = self.rows()
        response = self.push(body([line(v="dddddddd", q=9)], company=H2))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), {"error": "company mismatch", "refused": True})
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.meta("sales_company_hash"), H1)
        self.assertEqual(self.push(body([line(q=5)])).status_code, 200)         # the pinned company still works

    def test_force_does_not_bypass_the_company_pin(self):
        self.push(body([line()]))
        self.assertEqual(self.push(body([line()], company=H2), headers=FORCE).status_code, 409)

    def test_guard_one_refuses_an_empty_push_over_50_or_more_stored_lines(self):
        self.push(body(many(60)))
        response = self.push(body([]))
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.get_json()["refused"])
        self.assertEqual(len(self.rows()), 60)
        forced = self.push(body([]), headers=FORCE)
        self.assertEqual((forced.status_code, forced.get_json()["deleted"]), (200, 60))
        self.assertEqual(self.rows(), [])

    def test_guard_one_allows_an_empty_push_when_fewer_than_50_are_stored(self):
        self.push(body(many(49)))
        self.assertEqual(self.push(body([])).status_code, 200)

    def test_guard_two_refuses_under_30_percent_when_100_or_more_are_stored(self):
        self.push(body(many(120)))
        self.assertEqual(self.push(body(many(35, prefix="c"))).status_code, 422)      # 35 of 120 is 29%
        self.assertEqual(len(self.rows()), 120)
        self.assertEqual(self.push(body(many(36, prefix="c"))).status_code, 200)      # 36 of 120 is exactly 30%
        self.assertEqual(len(self.rows()), 36)

    def test_guard_two_can_be_forced(self):
        self.push(body(many(120)))
        self.assertEqual(self.push(body(many(5, prefix="c")), headers=FORCE).status_code, 200)
        self.assertEqual(len(self.rows()), 5)

    def test_guard_two_does_not_apply_under_100_stored_lines(self):
        self.push(body(many(99)))
        self.assertEqual(self.push(body(many(1, prefix="c"))).status_code, 200)

    def test_the_guards_only_look_inside_the_pushed_range(self):
        self.push(body(many(120, day="2026-02-10"), frm="2026-02-01", to="2026-02-28"))
        self.assertEqual(self.push(body([], frm="2026-01-01", to="2026-01-31")).status_code, 200)     # nothing stored in January
        self.assertEqual(len(self.rows()), 120)


class Locking(SalesBase):
    def test_a_held_stock_intake_lock_does_not_block_the_sales_intake(self):
        self.assertTrue(self.app_module._INTAKE_APPLY_LOCK.acquire(timeout=1))
        try:
            started = time.perf_counter()
            self.assertEqual(self.push(body([line()])).status_code, 200)
            self.assertLess(time.perf_counter() - started, 5)
        finally:
            self.app_module._INTAKE_APPLY_LOCK.release()

    def test_a_busy_sales_lock_answers_503(self):
        self.assertTrue(self.app_module._SALES_INTAKE_LOCK.acquire(timeout=1))
        try:
            with mock.patch.object(self.app_module, "SALES_LOCK_TIMEOUT_SECONDS", 0.05):
                response = self.push(body([line()]))
        finally:
            self.app_module._SALES_INTAKE_LOCK.release()
        self.assertEqual((response.status_code, response.get_json()), (503, {"error": "busy"}))
        self.assertEqual(self.rows(), [])

    def test_a_locked_database_answers_503_after_a_short_wait(self):
        holder = sqlite3.connect(self.db.DB_PATH, timeout=1)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(self.app_module, "SALES_DB_TIMEOUT_SECONDS", 0.2):
                started = time.perf_counter()
                response = self.push(body([line()]))
                waited = time.perf_counter() - started
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual((response.status_code, response.get_json()), (503, {"error": "busy"}))
        self.assertLess(waited, 3)
        self.assertEqual(self.push(body([line()])).status_code, 200)                # and it works again once the lock is gone

    def test_the_route_uses_a_five_second_database_timeout(self):
        self.assertEqual(self.app_module.SALES_DB_TIMEOUT_SECONDS, 5)
        self.assertEqual(self.app_module.SALES_LOCK_TIMEOUT_SECONDS, 5)

    def test_an_unexpected_error_answers_with_a_fixed_message(self):
        with mock.patch.object(self.app_module, "_short_db_connection", side_effect=RuntimeError("secret C:\\data\\private.db")):
            response = self.push(body([line()]))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "Could not store the sales data."})
        self.assertNotIn("secret", response.get_data(as_text=True))
        self.assertNotIn("private", response.get_data(as_text=True))

    def test_the_stock_intake_still_works_after_a_sales_push(self):
        self.push(body([line()]))
        self.assertEqual(self.anon.post("/admin/intake/sync_data", json=PAYLOAD, headers=INTAKE_HEADERS).status_code, 200)


class Speed(SalesBase):
    def test_30000_lines_are_stored_in_under_two_seconds_first_time_and_on_a_repeat(self):
        lines = [line(v=f"{n:08x}", d=f"2026-01-{1 + n % 28:02d}", i=f"ITEM {n % 3000}", q=1 + n % 3) for n in range(30000)]
        payload = body(lines)
        timings = []
        for attempt in ("first push", "repeat push"):
            started = time.perf_counter()
            response = self.push(payload, headers=FORCE)
            timings.append((attempt, time.perf_counter() - started))
            self.assertEqual(response.status_code, 200)
        print(chr(10) + "  sales intake, 30000 lines: " + ", ".join(f"{name} {seconds * 1000:.0f} ms" for name, seconds in timings))
        for name, seconds in timings:
            self.assertLess(seconds, 2.0, name)                    # the request, including parsing and checking; the lock is held for less
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sales_lines"), [(30000,)])


class StatusAndReset(SalesBase):
    def status(self, client=None):
        response = (client or self.admin).get("/admin/stock_mgmt/sales_status")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def seed(self):
        with self.at(2026, 3, 20):
            self.push(body([line(v="aaaaaaaa", d="2026-01-10", i="ALTO 7D MAT BLACK", q=3),
                            line(v="aaaaaaaa", d="2026-01-10", i="MYSTERY ITEM ONE", q=2),
                            line(v="bbbbbbbb", d="2026-01-15", k="return", i="ALTO 7D MAT BLACK", q=1),
                            line(v="cccccccc", d="2026-02-03", i="SWIFT 7D MAT GREY", q=4),
                            line(v="dddddddd", d="2026-02-04", i="Mystery  Item Two", q=1)], frm="2026-01-01", to="2026-02-28"))
        self.app_module._stock_mgmt_cache_clear()

    def test_without_data(self):
        data = self.status()
        self.assertFalse(data["has_data"])
        self.assertEqual((data["total_lines"], data["months"], data["company_pin"], data["last_sync_utc"]), (0, [], None, None))

    def test_the_numbers_for_a_known_dataset(self):
        self.seed()
        data = self.status()
        self.assertTrue(data["has_data"])
        self.assertEqual((data["total_lines"], data["vouchers"], data["items"]), (5, 4, 4))
        self.assertEqual((data["first_day"], data["last_day"]), ("2026-01-10", "2026-02-04"))
        self.assertEqual(data["months"], [{"month": "2026-01", "vouchers": 2, "sold": 5, "returned": 1, "net": 4},
                                          {"month": "2026-02", "vouchers": 2, "sold": 5, "returned": 0, "net": 5}])
        self.assertEqual(data["last_sync_utc"], "2026-03-20T06:30:00")
        self.assertEqual(data["last_sync_ist"], "2026-03-20 12:00")
        self.assertEqual((data["last_from"], data["last_to"]), ("2026-01-01", "2026-02-28"))
        self.assertEqual(data["company_pin"], H1[:6])
        self.assertNotIn(H1, json.dumps(data))                                       # only the first 6 characters leave the server

    def test_sold_names_that_match_no_catalogue_item_are_counted_with_samples(self):
        self.seed()
        data = self.status()
        self.assertEqual(data["unmatched_count"], 2)
        self.assertEqual(data["unmatched_samples"], ["MYSTERY ITEM ONE", "MYSTERY ITEM TWO"])

    def test_a_stock_item_counts_as_matched_and_samples_stop_at_20(self):
        with self.db.get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO stock_state VALUES ('ONLY IN STOCK', 'Only In Stock', 1, 'x')")
        self.push(body([line(i="ONLY IN STOCK")] + [line(v=f"e{n:07x}", i=f"UNKNOWN {n:03d}") for n in range(30)]))
        data = self.status()
        self.assertEqual(data["unmatched_count"], 30)
        self.assertEqual(len(data["unmatched_samples"]), 20)

    def test_the_status_is_cached_but_a_sales_push_clears_it(self):
        self.seed()
        first = self.status()
        with self.at(2026, 3, 21):
            self.push(body([line(v="eeeeeeee", d="2026-01-11", q=1)], frm="2026-01-01", to="2026-02-28"))
        self.assertNotEqual(self.status()["total_lines"], first["total_lines"])

    def test_the_reset_route_clears_only_the_pin(self):
        self.seed()
        for wrong in ({}, {"confirm": "reset"}, {"confirm": "YES"}, {"confirm": True}):
            self.assertEqual(self.admin.post("/admin/stock_mgmt/sales_reset_company", json=wrong).status_code, 422, wrong)
        self.assertEqual(self.admin.post("/admin/stock_mgmt/sales_reset_company", data="RESET").status_code, 422)
        self.assertEqual(self.meta("sales_company_hash"), H1)
        response = self.admin.post("/admin/stock_mgmt/sales_reset_company", json={"confirm": "RESET"})
        self.assertEqual((response.status_code, response.get_json()["ok"]), (200, True))
        self.assertIsNone(self.meta("sales_company_hash"))
        self.assertEqual(len(self.rows()), 5)                                        # the data is kept
        self.assertEqual(self.push(body([line(v="ffffffff")], company=H2, frm="2026-01-01", to="2026-02-28")).status_code, 200)
        self.assertEqual(self.meta("sales_company_hash"), H2)

    def test_dealers_and_logged_out_visitors_are_denied_on_every_new_admin_route(self):
        self.seed()
        for client, allowed in ((self.dealer, (403,)), (self.anon, (401, 403))):
            self.assertIn(client.get("/admin/stock_mgmt/sales_status").status_code, allowed)
            self.assertIn(client.post("/admin/stock_mgmt/sales_reset_company", json={"confirm": "RESET"}).status_code, allowed)
            self.assertIn(client.get("/admin/stock_mgmt").status_code, allowed)
        self.assertEqual(self.meta("sales_company_hash"), H1)

    def test_the_page_has_a_collapsed_sales_data_box(self):
        text = self.admin.get("/admin/stock_mgmt").get_data(as_text=True)
        self.assertIn("Sales data", text)
        self.assertIn("No sales data received yet.", text)
        self.assertIn("Reset company pin", text)
        self.assertNotIn("setInterval", text)



class Visibility(SalesBase):
    """What the Sales data box tells the owner: when sales last arrived, whether the shop's last push was refused, a warning when the
    shop goes quiet, and how far back the pushes really reach."""

    def clock(self, moment):
        return mock.patch.object(self.app_module, "_stock_now_utc", return_value=moment)

    def status(self):
        self.app_module._stock_mgmt_cache_clear()
        response = self.admin.get("/admin/stock_mgmt/sales_status")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def meta_json(self, key):
        text = self.meta(key)
        return json.loads(text) if text else None

    # ---- the last refused push
    def test_409_413_and_422_are_recorded_after_a_valid_token_with_the_reason_and_the_range(self):
        with self.clock(NOW):
            self.assertEqual(self.push(body([line()])).status_code, 200)
        with self.clock(NOW + timedelta(minutes=5)):
            self.assertEqual(self.push(body([line(v="dddddddd")], company=H2)).status_code, 409)
        self.assertEqual(self.meta_json("sales_last_refused"), {"utc": "2026-03-20T06:35:00", "status": 409, "reason": "company mismatch",
                                                                "from": "2026-01-01", "to": "2026-01-31", "lines": 1})
        with self.clock(NOW + timedelta(minutes=6)):
            self.assertEqual(self.push(body([line(), line(q=0)])).status_code, 422)
        refused = self.meta_json("sales_last_refused")
        self.assertEqual((refused["utc"], refused["status"], refused["from"], refused["to"], refused["lines"]), ("2026-03-20T06:36:00", 422, "2026-01-01", "2026-01-31", 2))
        self.assertTrue(refused["reason"].startswith("line 1: q must"))
        with self.clock(NOW + timedelta(minutes=7)):
            big = body([line(i="X" * 150 + str(n)) for n in range(25000)])
            self.assertEqual(self.push(big).status_code, 413)
        self.assertEqual(self.meta_json("sales_last_refused"), {"utc": "2026-03-20T06:37:00", "status": 413, "reason": "body too large",
                                                                "from": None, "to": None, "lines": None})
        with self.clock(NOW + timedelta(minutes=8)):
            self.push(body(many(60)))
            self.assertEqual(self.push(body([])).status_code, 422)                       # the guard: an empty push over 50 stored lines
        refused = self.meta_json("sales_last_refused")
        self.assertEqual((refused["status"], refused["reason"], refused["lines"]), (422, "this push would remove most of the stored sales lines", 0))

    def test_a_body_that_is_not_json_is_recorded_without_a_range(self):
        with self.clock(NOW):
            response = self.anon.post(URL, data="not json at all", content_type="application/json", headers=INTAKE_HEADERS)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.meta_json("sales_last_refused"), {"utc": "2026-03-20T06:30:00", "status": 422, "reason": "body must be a JSON object",
                                                                "from": None, "to": None, "lines": None})

    def test_the_reason_is_at_most_120_characters_and_never_holds_an_item_name(self):
        self.push(body([line(i="SECRET DESIGN NAME", q=0)]))
        refused = self.meta_json("sales_last_refused")
        self.assertLessEqual(len(refused["reason"]), 120)
        self.assertNotIn("SECRET", json.dumps(refused))

    def test_401_403_503_and_a_wrong_token_are_not_recorded(self):
        for headers in ({}, {"X-Intake-Token": "wrong"}, {"X-Intake-Token": ""}):
            self.assertEqual(self.anon.post(URL, json=body([line()]), headers=headers).status_code, 403)
            self.anon.post(URL, data="not json at all", content_type="application/json", headers=headers)      # a malformed body that never gets past the token
        with mock.patch.object(self.app_module.Config, "INTAKE_SYNC_TOKEN", ""):
            self.assertEqual(self.push(body([line()])).status_code, 403)
        self.assertTrue(self.app_module._SALES_INTAKE_LOCK.acquire(timeout=1))
        try:
            with mock.patch.object(self.app_module, "SALES_LOCK_TIMEOUT_SECONDS", 0.05):
                self.assertEqual(self.push(body([line()])).status_code, 503)
        finally:
            self.app_module._SALES_INTAKE_LOCK.release()
        self.assertIsNone(self.meta("sales_last_refused"))
        self.assertIsNone(self.meta("sales_last_accepted"))

    def test_an_accepted_push_is_recorded_with_its_range_and_line_count(self):
        with self.clock(NOW):
            self.push(body([line(), line(v="bbbbbbbb")]))
        self.assertEqual(self.meta_json("sales_last_accepted"), {"utc": "2026-03-20T06:30:00", "from": "2026-01-01", "to": "2026-01-31", "lines": 2})
        self.assertIsNone(self.meta("sales_last_refused"))

    # ---- what the box shows
    def test_the_last_sales_received_line_uses_the_ist_time_the_page_already_uses(self):
        with self.clock(NOW):
            self.push(body([line()]))
            data = self.status()
        self.assertEqual(data["last_received_line"], "Last sales received: 2026-03-20 12:00")
        self.assertEqual(data["last_sync_ist"], "2026-03-20 12:00")
        self.assertEqual(data["last_accepted"], {"utc": "2026-03-20T06:30:00", "from": "2026-01-01", "to": "2026-01-31", "lines": 1})

    def test_a_refused_push_newer_than_the_last_accepted_one_shows_and_a_later_accepted_push_hides_it(self):
        with self.clock(NOW):
            self.push(body([line()]))
        with self.clock(NOW + timedelta(hours=1)):
            self.assertEqual(self.push(body([line()], company=H2)).status_code, 409)
            data = self.status()
        self.assertEqual(data["refused_line"], "The shop's last push was refused (409) on 2026-03-20 13:00: company mismatch")
        self.assertEqual(data["last_received_line"], "Last sales received: 2026-03-20 12:00")
        with self.clock(NOW + timedelta(hours=2)):
            self.assertEqual(self.push(body([line(q=2)])).status_code, 200)
            data = self.status()
        self.assertIsNone(data["refused_line"])
        self.assertEqual(data["last_received_line"], "Last sales received: 2026-03-20 14:00")
        self.assertEqual(data["last_refused"]["status"], 409)                       # still on record, just not shown

    def test_a_refusal_older_than_the_last_accepted_push_is_not_shown(self):
        with self.clock(NOW):
            self.push(body([line()], company=H1))
            self.push(body([line()], company=H2))
            self.assertIsNone(self.status()["refused_line"])                          # same minute: the refusal is not NEWER

    def test_a_refusal_with_nothing_ever_accepted_still_shows_and_adds_no_warning(self):
        with self.clock(NOW):
            self.assertEqual(self.push(body([line(q=0)])).status_code, 422)
            data = self.status()
        self.assertFalse(data["has_data"])
        self.assertTrue(data["refused_line"].startswith("The shop's last push was refused (422) on 2026-03-20 12:00: line 0: q must"))
        self.assertIsNone(data["last_received_line"])
        self.assertIsNone(data["no_news_line"])

    def test_the_warning_waits_for_more_than_30_hours(self):
        with self.clock(NOW):
            self.push(body([line()]))
        for delta, expected in ((timedelta(hours=29, minutes=59), None), (timedelta(hours=30), None),
                                (timedelta(hours=30, minutes=1), "No sales received from the shop for 30 hours. Check the shop PC and Tally."),
                                (timedelta(hours=50, minutes=30), "No sales received from the shop for 50 hours. Check the shop PC and Tally.")):
            with self.clock(NOW + delta):
                data = self.status()
            self.assertEqual(data["no_news_line"], expected, delta)
        with self.clock(NOW + timedelta(hours=40)):
            self.push(body([line(q=2)]))                                               # news arrives again: the warning goes
            self.assertIsNone(self.status()["no_news_line"])

    def test_no_warning_and_the_old_wording_when_nothing_was_ever_accepted(self):
        with self.clock(NOW + timedelta(days=400)):
            data = self.status()
        self.assertFalse(data["has_data"])
        self.assertEqual((data["no_news_line"], data["no_news_hours"], data["last_received_line"], data["refused_line"]), (None, None, None, None))
        self.assertIn("No sales data received yet.", self.admin.get("/admin/stock_mgmt").get_data(as_text=True))

    # ---- how far back the pushes reach
    def test_coverage_starts_with_the_first_push_extends_over_touching_pushes_and_ignores_a_gap(self):
        def push(frm, to, day_in, **kw):
            self.assertEqual(self.push(body([line(d=day_in)], frm=frm, to=to, **kw)).status_code, 200)
            return self.meta("sales_covered_from")
        self.assertIsNone(self.meta("sales_covered_from"))
        self.assertEqual(push("2026-03-01", "2026-03-31", "2026-03-10"), "2026-03-01")           # the first push
        self.assertEqual(push("2026-02-01", "2026-02-28", "2026-02-10"), "2026-02-01")           # last month: touches it, so it extends
        self.assertEqual(push("2025-11-01", "2025-11-30", "2025-11-10"), "2026-02-01")           # a gap (Dec and Jan are missing): unchanged
        self.assertEqual(push("2026-02-10", "2026-03-31", "2026-03-10"), "2026-02-01")           # inside what is covered: unchanged
        self.assertEqual(push("2026-04-01", "2026-04-30", "2026-04-10"), "2026-02-01")           # later than the covered stretch: unchanged
        self.assertEqual(push("2026-01-01", "2026-01-31", "2026-01-10"), "2026-01-01")           # ends the day before the covered start: extends
        self.assertEqual(push("2025-12-01", "2025-12-31", "2025-12-10"), "2025-12-01")
        self.assertEqual(self.status()["sales_covered_from"], "2025-12-01")

    def test_a_refused_push_does_not_move_the_coverage(self):
        self.push(body([line(d="2026-03-10")], frm="2026-03-01", to="2026-03-31"))
        self.assertEqual(self.push(body([line(d="2026-02-10")], frm="2026-02-01", to="2026-02-28", company=H2)).status_code, 409)
        self.assertEqual(self.push(body([line(d="2026-02-10", q=0)], frm="2026-02-01", to="2026-02-28")).status_code, 422)
        self.assertEqual(self.meta("sales_covered_from"), "2026-03-01")

    def test_reset_clears_the_coverage_and_what_the_shop_last_sent_and_keeps_the_lines(self):
        with self.clock(NOW):
            self.push(body([line()]))
            self.push(body([line()], company=H2))
        for key in ("sales_covered_from", "sales_last_accepted", "sales_last_refused", "sales_company_hash"):
            self.assertIsNotNone(self.meta(key), key)
        self.assertEqual(self.admin.post("/admin/stock_mgmt/sales_reset_company", json={"confirm": "RESET"}).status_code, 200)
        for key in ("sales_covered_from", "sales_last_accepted", "sales_last_refused", "sales_company_hash"):
            self.assertIsNone(self.meta(key), key)
        data = self.status()
        self.assertEqual((data["sales_covered_from"], data["last_received_line"], data["refused_line"], data["no_news_line"]), (None, None, None, None))
        self.assertEqual(len(self.rows()), 1)                                         # the lines themselves stay, as before

    # ---- the page
    def test_the_page_can_show_each_new_line(self):
        text = self.admin.get("/admin/stock_mgmt").get_data(as_text=True)
        for needed in ("data.last_received_line", "data.refused_line", "data.no_news_line", "salesNoNews", "salesRefused", "salesReceived",
                       "first.history_note", "historyNote", "data.sales_covered_from"):
            self.assertIn(needed, text)
        self.assertIn("No sales data received yet.", text)                              # the old wording is still the starting text

    def test_the_status_payload_without_data_has_every_new_key_empty(self):
        data = self.status()
        for key in ("last_accepted", "last_refused", "last_received_line", "refused_line", "no_news_line", "no_news_hours", "sales_covered_from"):
            self.assertIn(key, data)
            self.assertIsNone(data[key], key)


if __name__ == "__main__":
    unittest.main()
