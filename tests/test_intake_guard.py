"""A feeder push that is empty, malformed or implausible is refused (HTTP 422) before anything is written."""
import copy
import json
import os
import re
import unittest

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


if __name__ == "__main__":
    unittest.main()
