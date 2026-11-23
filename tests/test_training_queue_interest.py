"""Training Mode: cars that dealers (and admins) open most come first. Only the ORDER changes -- no new field, no
change to matching, saving or suggestions."""
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import support
from tests.support import INTAKE_HEADERS

NOW = datetime(2026, 3, 20, 6, 30, tzinfo=timezone.utc)
CARS = ["CARA", "CARB", "CARC", "CARD"]


def payload():
    hierarchy, stock = [], []
    for car in CARS:
        hierarchy.append({"item_name": car, "qty": 3})
        for n in (1, 2, 3):
            hierarchy.append({"item_name": f"{car} ITEM {n}", "qty": 1})
            stock.append({"item_name": f"{car} ITEM {n}", "qty": 1})
        hierarchy.append({"item_name": "", "qty": ""})
    return {"car_master": list(CARS), "main_hierarchy": hierarchy, "item_stock": stock}


def day(offset):
    return (datetime(2026, 3, 20) - timedelta(days=offset)).strftime("%Y-%m-%d")


class TrainingQueueBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        self.admin = support.client_for("admin_locked")
        self.dealer = support.client_for("dealer")
        self.anon = support.client_for("anonymous")
        self.assertEqual(self.anon.post("/admin/intake/sync_data", json=payload(), headers=INTAKE_HEADERS).status_code, 200)
        with self.db.get_connection() as conn:
            for table in ("car_interest_daily", "stock_mgmt_settings"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM user_activity WHERE action = 'view_car'")
            conn.execute("DELETE FROM mappings")
            conn.execute("DELETE FROM images")
            conn.execute("DELETE FROM folder_car_mapping")
        patcher = mock.patch.object(self.app_module, "_stock_now_utc", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        support.restore_state(self.snapshot)

    def add_images(self, folder, names):
        """Unmapped images; the file name decides the original order (it sorts by lower-case file name)."""
        with self.db.get_connection() as conn:
            for name in names:
                conn.execute("INSERT INTO images (car_folder, filename, filepath, scan_date) VALUES (?, ?, ?, '2026-01-01')",
                             (folder, name, f"{folder}/{name}"))

    def opens(self, car, role, user_id, count, days_ago=1):
        """Car opens as the Usage Charts already record them (user_activity, action 'view_car'); user_id is unused."""
        at = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.db.get_connection() as conn:
            user = conn.execute("SELECT id FROM users WHERE username = ?", ("star" if role == "dealer" else "admin",)).fetchone()[0]
            conn.executemany("INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (?, ?, 'view_car', ?)", [(user, at, car)] * count)

    def queue(self, client=None, **params):
        response = (client or self.admin).get("/train_images", query_string=dict({"limit": 200}, **params))
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def names(self, data):
        return [image["filename"] for image in data["images"]]

    def standard_images(self):
        # original order by file name: a1 a2 a3 a4 a5 a6 (folders alternate)
        self.add_images("CARA", ["a1.jpg", "a4.jpg"])
        self.add_images("CARB", ["a2.jpg", "a5.jpg"])
        self.add_images("CARC", ["a3.jpg", "a6.jpg"])


class ImageQueueOrder(TrainingQueueBase):
    def test_cars_opened_most_come_first(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        self.opens("CARB", "dealer", 11, 4)
        self.assertEqual(self.names(self.queue()), ["a3.jpg", "a6.jpg", "a2.jpg", "a5.jpg", "a1.jpg", "a4.jpg"])

    def test_without_any_interest_the_order_is_exactly_the_original(self):
        self.standard_images()
        original = self.names(self.queue(sort="original"))
        self.assertEqual(original, ["a1.jpg", "a2.jpg", "a3.jpg", "a4.jpg", "a5.jpg", "a6.jpg"])
        self.assertEqual(self.names(self.queue()), original)

    def test_original_order_can_be_chosen(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        self.assertEqual(self.names(self.queue(sort="original")), ["a1.jpg", "a2.jpg", "a3.jpg", "a4.jpg", "a5.jpg", "a6.jpg"])
        self.assertEqual(self.names(self.queue(sort="most_searched"))[:2], ["a3.jpg", "a6.jpg"])

    def test_ties_and_cars_without_interest_keep_their_original_order(self):
        self.standard_images()
        self.add_images("CARD", ["a0.jpg"])
        self.opens("CARB", "dealer", 11, 5)
        self.opens("CARC", "dealer", 12, 5)                      # a tie with CARB
        # interest first (CARB and CARC tied, in original order), then the rest (CARD's a0 and CARA) in original order
        self.assertEqual(self.names(self.queue()), ["a2.jpg", "a3.jpg", "a5.jpg", "a6.jpg", "a0.jpg", "a1.jpg", "a4.jpg"])

    def test_only_the_last_30_days_count(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 99, days_ago=45)
        self.opens("CARB", "dealer", 11, 1, days_ago=2)
        self.assertEqual(self.names(self.queue())[:2], ["a2.jpg", "a5.jpg"])

    def test_the_include_admin_setting_changes_the_order(self):
        self.standard_images()
        self.opens("CARA", "dealer", 11, 3)
        self.opens("CARC", "admin", 1, 50)
        self.assertEqual(self.names(self.queue())[:2], ["a3.jpg", "a6.jpg"])               # admin counted by default
        self.assertEqual(self.admin.post("/admin/stock_mgmt/settings", json={"include_admin": 0}).status_code, 200)
        self.assertEqual(self.names(self.queue())[:2], ["a1.jpg", "a4.jpg"])               # dealers only

    def test_an_explicit_folder_mapping_is_used_like_everywhere_else_in_training(self):
        self.add_images("Old Folder Name", ["z1.jpg"])
        self.add_images("CARA", ["a1.jpg"])
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO folder_car_mapping (folder_name, car_model_name, created_at) VALUES ('Old Folder Name', 'CARD', '2026-01-01')")
        self.opens("CARD", "dealer", 11, 5)
        self.assertEqual(self.names(self.queue()), ["z1.jpg", "a1.jpg"])

    def test_a_chosen_folder_is_not_reordered(self):
        self.add_images("CARA", ["a2.jpg", "a1.jpg"])
        self.opens("CARA", "dealer", 11, 5)
        self.assertEqual(self.names(self.queue(folder="CARA")), self.names(self.queue(folder="CARA", sort="original")))

    def test_mapped_images_stay_out_of_the_queue(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        first = self.queue()["images"][0]
        self.db.add_mapping(first["id"], "CARC ITEM 1", "CARC", 1.0, "human")
        self.assertEqual(self.names(self.queue())[:2], ["a6.jpg", "a1.jpg"])

    def test_the_limit_still_applies_after_ordering(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        self.assertEqual(self.names(self.queue(limit=1)), ["a3.jpg"])
        self.assertEqual(self.queue(limit=1)["count"], 1)

    def test_no_new_fields_anywhere(self):
        self.standard_images()
        before = self.queue(sort="original")
        self.opens("CARC", "dealer", 11, 9)
        after = self.queue()
        self.assertEqual(set(before), set(after))
        self.assertEqual({tuple(sorted(i)) for i in before["images"]}, {tuple(sorted(i)) for i in after["images"]})
        self.assertEqual(before["stats"], after["stats"])
        self.assertEqual(before["count"], after["count"])

    def test_dealers_get_no_interest_data(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        text = self.dealer.get("/train_images", query_string={"limit": 200}).get_data(as_text=True)
        for word in ("opens", "interest", "searched", "score"):
            self.assertNotIn(word, text)


class WorkQueueOrder(TrainingQueueBase):
    def cars(self, path="/api/needs_image_matching_queue", **params):
        response = self.admin.get(path, query_string=params)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_cars_inside_a_tier_follow_interest_and_tiers_stay_together(self):
        # CARD has a category on every item: tier 2 for the image queue. The others are tier 3.
        category = self.db.get_all_categories()[0]["name"]
        for n in (1, 2, 3):
            self.db.upsert_design_category(f"CARD ITEM {n}", category, 1)
        self.app_module._invalidate_queue_stats_cache()
        self.opens("CARC", "dealer", 11, 9)
        self.opens("CARA", "dealer", 11, 2)
        self.opens("CARD", "dealer", 11, 99)
        original = [c["car"] for c in self.cars(sort="original")["cars"]]
        ordered = [c["car"] for c in self.cars()["cars"]]
        tiers = {c["car"]: c["tier"] for c in self.cars()["cars"]}
        self.assertEqual(original, sorted(original, key=lambda car: (tiers[car], car)))        # the old order: by tier, then name
        self.assertEqual(ordered, ["CARD", "CARC", "CARA", "CARB"])                           # tier 2 first, then tier 3 by interest
        self.assertEqual([tiers[c] for c in ordered], sorted(tiers[c] for c in ordered))

    def test_without_interest_it_is_the_original_order_and_no_new_fields(self):
        for path in ("/api/needs_image_matching_queue", "/api/needs_category_queue"):
            original = self.cars(path, sort="original")
            default = self.cars(path)
            self.assertEqual(original, default)
        self.opens("CARC", "dealer", 11, 9)
        changed = self.cars()["cars"]
        original = self.cars(sort="original")["cars"]
        self.assertEqual(sorted(c["car"] for c in changed), sorted(c["car"] for c in original))
        self.assertEqual({tuple(sorted(c)) for c in changed}, {tuple(sorted(c)) for c in original})
        self.assertEqual(changed[0]["car"], "CARC")


class MatchingUnchanged(TrainingQueueBase):
    def test_matching_and_stats_routes_answer_the_same_with_and_without_interest(self):
        self.standard_images()
        paths = ("/api/resolve_car_from_folder?folder=CARA", "/mapping_stats", "/api/get_unmapped_images?limit=1")
        before = [self.admin.get(p).get_data() for p in paths]
        self.opens("CARC", "dealer", 11, 9)
        self.assertEqual([self.admin.get(p).get_data() for p in paths], before)

    def test_saving_a_match_still_works(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        image = self.queue()["images"][0]
        response = self.admin.post("/confirm_mapping", json={"image_id": image["id"], "stock_item_name": "CARC ITEM 1", "car_model": "CARC"})
        self.assertIn(response.status_code, (200, 201))
        self.assertNotIn(image["id"], [i["id"] for i in self.queue()["images"]])


class TrainPage(TrainingQueueBase):
    def test_the_page_opens_on_the_most_searched_car_and_has_the_sort_control(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        text = self.admin.get("/train").get_data(as_text=True)
        self.assertIn("Most searched cars first", text)
        self.assertIn("Original order", text)
        self.assertRegex(text, r'const initialImage = \{[^;]*"car_folder":\s*"CARC"')
        original = self.admin.get("/train", query_string={"sort": "original"}).get_data(as_text=True)
        self.assertRegex(original, r'const initialImage = \{[^;]*"car_folder":\s*"CARA"')

    def test_an_explicit_image_in_the_link_wins(self):
        self.standard_images()
        self.opens("CARC", "dealer", 11, 9)
        image = self.queue(sort="original")["images"][0]
        text = self.admin.get("/train", query_string={"image_id": image["id"]}).get_data(as_text=True)
        head = text[text.index("const initialImage"):].split(";")[0]
        self.assertIn('"id": %d,' % image["id"], head)


class Speed(TrainingQueueBase):
    def test_a_large_queue_is_not_noticeably_slower(self):
        with self.db.get_connection() as conn:
            conn.executemany("INSERT INTO images (car_folder, filename, filepath, scan_date) VALUES (?, ?, ?, '2026-01-01')",
                             [(f"FOLDER {i % 400}", f"img{i:05d}.jpg", f"FOLDER {i % 400}/img{i:05d}.jpg") for i in range(5000)])
            star = conn.execute("SELECT id FROM users WHERE username = 'star'").fetchone()[0]
            conn.executemany("INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (?, ?, 'view_car', ?)",
                             [(star, (NOW - timedelta(days=i % 20)).strftime("%Y-%m-%dT%H:%M:%S"), f"CAR {i % 300}") for i in range(3000)])
            conn.executemany("INSERT INTO user_activity (user_id, at_utc, action, detail) VALUES (?, ?, 'view_car', 'CARA')", [(star, (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"))] * 5)
        timings = {}
        for label, params in (("original", {"sort": "original"}), ("interest", {})):
            best = 9.0
            for _ in range(3):
                start = time.perf_counter()
                data = self.queue(**params)
                best = min(best, time.perf_counter() - start)
            timings[label] = best
            self.assertEqual(data["count"], 200)
        start = time.perf_counter()
        self.assertEqual(self.admin.get("/train").status_code, 200)
        page = time.perf_counter() - start
        print(f"\n  training queue, 5000 unmapped images: original {timings['original'] * 1000:.0f} ms, "
              f"most searched first {timings['interest'] * 1000:.0f} ms, /train page {page * 1000:.0f} ms")
        self.assertLess(timings["interest"] - timings["original"], 1.0)
        self.assertLess(timings["interest"], 1.0)


if __name__ == "__main__":
    unittest.main()
