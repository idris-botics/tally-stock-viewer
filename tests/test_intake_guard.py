"""A feeder push that is empty, malformed or implausible is refused (HTTP 422) before anything is written."""
import copy
import json
import os
import re
import unittest
from unittest import mock

from tests import support
from tests.support import PAYLOAD, INTAKE_HEADERS

FORCE_HEADERS = dict(INTAKE_HEADERS, **{"X-Intake-Force": "1"})


def big_payload(cars):
    """`cars` cars, each with a parent row and two designs; two stock rows per car."""
    names = [f"CAR{i:02d}" for i in range(cars)]
    hierarchy, stock = [], []
    for name in names:
        hierarchy += [{"item_name": name, "qty": 4}, {"item_name": f"{name} 7D MAT BLACK", "qty": 2},
                      {"item_name": f"{name} 9D MAT RED", "qty": 2}, {"item_name": "", "qty": ""}]
        stock += [{"item_name": f"{name} 7D MAT BLACK", "qty": 2}, {"item_name": f"{name} 9D MAT RED", "qty": 2}]
    return {"car_master": names, "main_hierarchy": hierarchy, "item_stock": stock}


class IntakeGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)      # every test starts from the fixture push
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")

    def tearDown(self):
        support.restore_state(self.snapshot)

    def push(self, payload, headers=INTAKE_HEADERS):
        return self.anon.post("/admin/intake/sync_data", json=payload, headers=headers)

    def file_bytes(self, name):
        with open(os.path.join(support.data_dir(), name), "rb") as handle:
            return handle.read()

    def served(self):
        return (self.dealer.get("/cars").get_data(), self.dealer.get("/designs", query_string={"car": "ALTO"}).get_data())

    # ---- empty pushes
    def test_all_empty_push_is_refused_and_changes_nothing(self):
        before_file, before_served = self.file_bytes("car_master.json"), self.served()
        response = self.push({"car_master": [], "main_hierarchy": [], "item_stock": []})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.get_json()["refused"])
        self.assertEqual(self.file_bytes("car_master.json"), before_file)
        self.assertEqual(self.served(), before_served)

    def test_empty_stock_is_refused(self):
        before = self.served()
        bad = copy.deepcopy(PAYLOAD); bad["item_stock"] = []
        self.assertEqual(self.push(bad).status_code, 422)
        self.assertEqual(self.served(), before)

    def test_empty_cars_is_refused(self):
        bad = copy.deepcopy(PAYLOAD); bad["car_master"] = []
        self.assertEqual(self.push(bad).status_code, 422)

    def test_refused_push_does_not_move_the_heartbeat(self):
        before = self.app_module.FEEDER_LAST_SEEN_AT
        bad = copy.deepcopy(PAYLOAD); bad["item_stock"] = []
        self.assertEqual(self.push(bad).status_code, 422)
        self.assertEqual(self.app_module.FEEDER_LAST_SEEN_AT, before)

    # ---- implausible drops
    # The drop check is skipped while the site serves fewer than 20 of something, so the "before" push has 20 cars.
    def test_large_drop_is_refused(self):
        self.assertEqual(self.push(big_payload(20), FORCE_HEADERS).status_code, 200)
        response = self.push(big_payload(8))
        self.assertEqual(response.status_code, 422)
        message = response.get_json()["error"]
        self.assertIn("20", message)
        self.assertIn("8", message)
        self.assertEqual(len(json.loads(self.file_bytes("car_master.json"))), 20, "a refused push must change nothing")

    def test_force_header_allows_large_drop(self):
        self.assertEqual(self.push(big_payload(20), FORCE_HEADERS).status_code, 200)
        self.assertEqual(self.push(big_payload(8), FORCE_HEADERS).status_code, 200)
        self.assertEqual(len(json.loads(self.file_bytes("car_master.json"))), 8)

    def test_small_change_and_small_site_are_allowed(self):
        self.assertEqual(self.push(big_payload(20), FORCE_HEADERS).status_code, 200)
        self.assertEqual(self.push(big_payload(12)).status_code, 200)     # 40% drop: allowed
        self.assertEqual(self.push(big_payload(9)).status_code, 200)      # 12 -> 9 cars, 24 -> 18 stock rows
        self.assertEqual(self.push(PAYLOAD).status_code, 200)             # fewer than 20 cars and stock rows served: not checked

    def test_first_ever_push_is_allowed(self):
        self.assertEqual(self.push(big_payload(20), FORCE_HEADERS).status_code, 200)
        for name in ("car_master.json", "main_hierarchy.json", "item stock list.auto.json"):
            os.remove(os.path.join(support.data_dir(), name))
        self.assertEqual(self.push(PAYLOAD).status_code, 200)

    # ---- wrong row contents
    def test_bad_row_types_are_refused(self):
        good = {"item_name": "ALTO 7D MAT BLACK", "qty": 3}
        cases = [dict(good, qty="abc"), dict(good, qty=2.5), dict(good, qty=None), dict(good, qty=True),
                 dict(good, item_name=123), dict(good, item_name=""), dict(good, item_name=None)]
        for bad_row in cases:
            with self.subTest(row=bad_row):
                payload = copy.deepcopy(PAYLOAD)
                payload["item_stock"] = [good, bad_row]
                response = self.push(payload)
                self.assertEqual(response.status_code, 422)
                message = response.get_json()["error"]
                self.assertIn("item_stock", message)
                self.assertIn("row 1", message)

    def test_negative_whole_quantity_is_accepted(self):
        # Tally allows negative stock and main.xlsx is read back raw, so a negative whole number must never freeze the push.
        payload = copy.deepcopy(PAYLOAD)
        payload["item_stock"][0]["qty"] = -3
        payload["item_stock"][1]["qty"] = -2.0
        payload["main_hierarchy"][1]["qty"] = -3
        payload["main_hierarchy"][2]["qty"] = -2.0
        self.assertEqual(self.push(payload).status_code, 200)
        stored = json.loads(self.file_bytes("item stock list.auto.json"))
        self.assertEqual([stored[0]["qty"], stored[1]["qty"]], [-3, -2])
        self.assertTrue(all(isinstance(row["qty"], int) for row in stored))
        # still refused: wrong types and fractions
        for bad in (True, None, "-3", 2.5, -2.5):
            with self.subTest(qty=bad):
                payload = copy.deepcopy(PAYLOAD); payload["item_stock"][0]["qty"] = bad
                self.assertEqual(self.push(payload).status_code, 422)
                payload = copy.deepcopy(PAYLOAD); payload["main_hierarchy"][1]["qty"] = bad
                self.assertEqual(self.push(payload).status_code, 422)

    def test_bad_hierarchy_and_car_rows_are_refused(self):
        payload = copy.deepcopy(PAYLOAD); payload["main_hierarchy"][1]["qty"] = "abc"
        response = self.push(payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn("main_hierarchy row 1", response.get_json()["error"])
        payload = copy.deepcopy(PAYLOAD); payload["main_hierarchy"][1]["item_name"] = 5
        self.assertEqual(self.push(payload).status_code, 422)
        payload = copy.deepcopy(PAYLOAD); payload["car_master"] = ["ALTO", ""]
        response = self.push(payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn("car_master row 1", response.get_json()["error"])

    def test_duplicate_car_names_are_refused(self):
        payload = copy.deepcopy(PAYLOAD); payload["car_master"] = ["ALTO", "alto ", "SWIFT"]
        response = self.push(payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn("car_master", response.get_json()["error"])

    def test_whole_number_floats_are_accepted(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["item_stock"][0]["qty"] = 3.0
        payload["main_hierarchy"][1]["qty"] = 3.0
        self.assertEqual(self.push(payload).status_code, 200)
        stored = json.loads(self.file_bytes("item stock list.auto.json"))
        self.assertEqual(stored[0]["qty"], 3)
        self.assertIsInstance(stored[0]["qty"], int)

    def test_error_body_has_no_path(self):
        bodies = []
        for mutate in (lambda p: p.update(car_master=[], main_hierarchy=[], item_stock=[]),
                       lambda p: p.update(item_stock=[{"item_name": 5, "qty": 1}]),
                       lambda p: p.update(car_master=["ALTO", "ALTO"])):
            payload = copy.deepcopy(PAYLOAD); mutate(payload)
            response = self.push(payload)
            self.assertEqual(response.status_code, 422)
            bodies.append(response.get_data(as_text=True))
        self.assertEqual(self.push(big_payload(20), FORCE_HEADERS).status_code, 200)
        bodies.append(self.push(big_payload(8)).get_data(as_text=True))
        for body in bodies:
            self.assertIsNone(re.search(r"[A-Za-z]:[\\/]|[\\/]data|Traceback|\.json", body), body)

    # ---- what the real feeder sends (every version since the first cloud push builds the payload the same way)
    def test_old_feeder_shapes_are_accepted(self):
        # shape 1: ints from Tally, "" qty on the blank separator row, car names already upper case
        self.assertEqual(self.push(PAYLOAD).status_code, 200)
        # shape 2: the hierarchy is read back from main.xlsx, so a quantity can arrive as a whole-number float and a blank row as None -> ""
        excel = copy.deepcopy(PAYLOAD)
        for row in excel["main_hierarchy"]:
            if isinstance(row["qty"], int):
                row["qty"] = float(row["qty"])
        self.assertEqual(self.push(excel).status_code, 200)
        # and the success response is unchanged
        body = self.push(PAYLOAD).get_json()
        self.assertEqual(body, {"ok": True, "car_count": 2, "hierarchy_rows": 6, "stock_rows": 3})


DATA_FILES = ("car_master.json", "main_hierarchy.json", "item stock list.auto.json")


class IntakeApplyIsOneUnit(unittest.TestCase):
    """A push is either fully on disk or not at all, and a failed write is an error (F002, F010, F034)."""

    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)
        self.anon = support.client_for("anonymous")
        self.dealer = support.client_for("dealer")

    def tearDown(self):
        support.restore_state(self.snapshot)

    def push(self, payload=PAYLOAD):
        return self.anon.post("/admin/intake/sync_data", json=payload, headers=INTAKE_HEADERS)

    def all_bytes(self):
        out = {}
        for name in DATA_FILES:
            with open(os.path.join(support.data_dir(), name), "rb") as handle:
                out[name] = handle.read()
        return out

    def changed_push(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["item_stock"][0]["qty"] = 99
        payload["main_hierarchy"][1]["qty"] = 99
        payload["car_master"] = ["ALTO", "SWIFT", "ZEN"]
        payload["main_hierarchy"] += [{"item_name": "ZEN", "qty": 1}, {"item_name": "ZEN 7D MAT BLACK", "qty": 1}]
        return payload

    def fail_writing(self, which, when):
        """Make the n-th (1-based) call of app.<which> that touches one of the three data files raise OSError."""
        app_module = self.app_module
        real = getattr(app_module, which, None)
        data_paths = {os.path.abspath(p) for p in (app_module.CAR_MASTER_CACHE_JSON, app_module.MAIN_HIERARCHY_CACHE_JSON, app_module.ITEM_STOCK_CACHE_JSON)}
        calls = []

        def wrapper(*args, **kwargs):
            touched = [a for a in args[:2] if isinstance(a, str) and os.path.abspath(a) in data_paths]
            if touched:
                calls.append(touched[0])
                if when(len(calls), touched[0]):
                    raise OSError("disk full")
            return real(*args, **kwargs)
        return mock.patch.object(app_module, which, wrapper, create=True)

    def test_failed_stock_write_is_an_error(self):
        before_seen = self.app_module.FEEDER_LAST_SEEN_AT
        stock_path = os.path.abspath(self.app_module.ITEM_STOCK_CACHE_JSON)
        with self.fail_writing("_write_json_temp", lambda n, path: os.path.abspath(path) == stock_path):
            response = self.push(self.changed_push())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("disk full", response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"error": "Failed to apply pushed data. See the server log."})
        self.assertEqual(self.app_module.FEEDER_LAST_SEEN_AT, before_seen)

    def test_failure_midway_leaves_all_three_files_unchanged(self):
        before = self.all_bytes()
        with self.fail_writing("_write_json_temp", lambda n, path: n == 2):
            self.assertEqual(self.push(self.changed_push()).status_code, 500)
        self.assertEqual(self.all_bytes(), before)

    def test_failure_on_the_last_write_leaves_all_three_files_unchanged(self):
        before = self.all_bytes()
        with self.fail_writing("_write_json_temp", lambda n, path: n == 3):
            self.assertEqual(self.push(self.changed_push()).status_code, 500)
        self.assertEqual(self.all_bytes(), before)

    def test_failure_while_replacing_files_restores_the_previous_ones(self):
        before = self.all_bytes()
        with self.fail_writing("_commit_json", lambda n, path: n == 3):
            self.assertEqual(self.push(self.changed_push()).status_code, 500)
        self.assertEqual(self.all_bytes(), before)
        self.assertEqual(self.dealer.get("/cars").get_json(), ["ALTO", "SWIFT"])

    def test_failed_reload_restores_previous_files(self):
        before = self.all_bytes()
        served_before = self.dealer.get("/cars").get_data()
        real_load = self.app_module.load_data
        calls = []

        def flaky_load(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("secret reload failure")
            return real_load(*args, **kwargs)
        with mock.patch.object(self.app_module, "load_data", flaky_load):
            response = self.push(self.changed_push())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret reload failure", response.get_data(as_text=True))
        self.assertEqual(self.all_bytes(), before)
        self.assertEqual(self.dealer.get("/cars").status_code, 200)
        self.assertEqual(self.dealer.get("/cars").get_data(), served_before)

    def test_good_push_leaves_no_temporary_or_previous_files(self):
        self.assertEqual(self.push(self.changed_push()).status_code, 200)
        self.assertEqual(self.dealer.get("/cars").get_json(), ["ALTO", "SWIFT", "ZEN"])
        leftovers = [n for n in os.listdir(support.data_dir()) if n.endswith((".prev", ".tmp"))]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
