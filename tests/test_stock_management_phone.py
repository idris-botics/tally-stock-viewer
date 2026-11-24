"""Stock Management on a phone, in a REAL browser (headless Microsoft Edge through Playwright): no sideways
scrolling, every tap target big enough, the tab bar reachable, and every tab renders its cards. Runs against the
scratch copy's fake data. Set STOCK_SHOTS_DIR to also save a screenshot of each tab at each size.

What this does not cover: Edge only (not Safari or a real phone), and real fingers."""
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import support
from tests.support import INTAKE_HEADERS

try:
    from playwright.sync_api import sync_playwright
except Exception:       # pragma: no cover
    sync_playwright = None

NOW = datetime(2026, 3, 20, 6, 30, tzinfo=timezone.utc)
SIZES = {"phone-390": (390, 844), "phone-small-360": (360, 640), "tablet-768": (768, 1024), "desktop-1280": (1280, 800)}
MIN_TAP = 44


def day(offset):
    return (datetime(2026, 3, 20) - timedelta(days=offset)).strftime("%Y-%m-%d")


def payload():
    hierarchy, stock = [], []
    for car in ("ALTO K10 (2014-2019) LONG NAME FOR WRAPPING", "SWIFT", "CRETA"):
        hierarchy.append({"item_name": car, "qty": 6})
        for n in range(1, 7):
            name = f"{car} {n}D MAT PREMIUM SEAT COVER BLACK/RED {n}"
            hierarchy.append({"item_name": name, "qty": 1})
            stock.append({"item_name": name, "qty": 1})
        hierarchy.append({"item_name": "", "qty": ""})
    return {"car_master": ["ALTO K10 (2014-2019) LONG NAME FOR WRAPPING", "SWIFT", "CRETA"], "main_hierarchy": hierarchy, "item_stock": stock}


class PhoneUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("playwright is not installed")
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()
        from werkzeug.serving import make_server
        cls.server = make_server("127.0.0.1", 0, cls.app_module.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:
            cls.pw.stop()
            raise unittest.SkipTest(f"Microsoft Edge could not be started: {exc}")
        context = cls.browser.new_context()
        page = context.new_page()
        page.goto(cls.base + "/login")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=access_code]", "idris123")
        page.click("button[type=submit], input[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url)
        cls.state = context.storage_state()
        context.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.server.shutdown()
        support.restore_state(cls.snapshot)

    def setUp(self):
        self.assertEqual(support.client_for("anonymous").post("/admin/intake/sync_data", json=payload(), headers=dict(INTAKE_HEADERS, **{"X-Intake-Force": "1"})).status_code, 200)   # a fixture push, whatever an earlier test left
        patcher = mock.patch.object(self.app_module, "_stock_now_utc", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app_module._stock_mgmt_cache_clear()
        long_names = [f"{car} {n}D MAT PREMIUM SEAT COVER BLACK/RED {n}" for car in ("ALTO K10 (2014-2019) LONG NAME FOR WRAPPING", "SWIFT", "CRETA") for n in range(1, 7)]
        with self.db.get_connection() as conn:
            for table in ("stock_state", "stock_events", "capture_meta", "restock_draft", "restock_orders", "stock_mgmt_settings", "sales_lines"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("INSERT INTO capture_meta VALUES ('stock_tracking_started_at_utc', ?)", ((NOW - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%S"),))
            for i, name in enumerate(long_names):
                key = name.upper()
                if i % 3 != 2:      # two of every three are in stock; the rest are sold out
                    conn.execute("INSERT INTO stock_state VALUES (?, ?, ?, 'x')", (key, name, [1, 2, 3, 9, 14, 40][i % 6]))
                conn.execute("INSERT INTO stock_events (day_ist, item_key, item_name, qty_before, qty_after, at_utc) VALUES (?, ?, ?, ?, ?, 'x')",
                             (day(3 + i % 5), key, name, 30, 30 - (4 + i * 2) if i % 3 != 2 else 0))
                # every design sold a little in the last weeks: the ones not in stock are "Sold out", qty-1 ones are "Last piece"
                conn.execute("INSERT INTO sales_lines VALUES (?, ?, 'sale', ?, ?, ?)", (f"{i:08x}", day(1 + i % 10), key, name, 2 + i % 4))
            conn.execute("INSERT INTO restock_orders (item_key, item_name, qty_ordered, ordered_at_utc, expected_date) VALUES (?, ?, 12, '2026-03-10T05:00:00', '2026-03-15')",
                         (long_names[0].upper(), long_names[0]))
            conn.execute("INSERT INTO restock_orders (item_key, item_name, qty_ordered, ordered_at_utc, expected_date) VALUES (?, ?, 5, '2026-03-18T05:00:00', '2026-04-02')",
                         (long_names[1].upper(), long_names[1]))
            conn.execute("INSERT INTO restock_orders (item_key, item_name, qty_ordered, ordered_at_utc, status, received_at_utc, qty_received) VALUES (?, ?, 6, '2026-03-01T05:00:00', 'received', '2026-03-17T05:00:00', 6)",
                         (long_names[3].upper(), long_names[3]))
            for name, qty in ((long_names[2], 20), (long_names[7], 6), (long_names[12], 3)):
                conn.execute("INSERT INTO restock_draft VALUES (?, ?, ?, ?, '2026-03-19T05:00:00')", (name.upper(), name, qty, "dark colour" if qty == 6 else None))

    def open(self, size, tab, scheme="light"):
        width, height = SIZES[size]
        context = self.browser.new_context(viewport={"width": width, "height": height}, storage_state=self.state, color_scheme=scheme,
                                           device_scale_factor=2, has_touch=width < 700, is_mobile=width < 700)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.goto(f"{self.base}/admin/stock_mgmt#{tab}")
        page.wait_for_selector("#panel .bin, #panel .empty, #panel .bar")
        page.wait_for_timeout(300)
        return page

    def shot(self, page, name):
        folder = os.environ.get("STOCK_SHOTS_DIR")
        if folder:
            os.makedirs(folder, exist_ok=True)
            page.screenshot(path=os.path.join(folder, name + ".png"), full_page=True)

    def problems(self, page):
        """Sideways overflow and any visible control smaller than 44 px."""
        return page.evaluate("""(minTap) => {
            const out = [];
            const doc = document.documentElement;
            if (doc.scrollWidth > window.innerWidth + 1) out.push('page scrolls sideways: ' + doc.scrollWidth + ' > ' + window.innerWidth);
            for (const el of document.querySelectorAll('button, input:not([type=checkbox]), select, a.btn, summary')) {
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                if (r.width === 0 || r.height === 0 || style.visibility === 'hidden') continue;
                if (r.height < minTap - 0.5 || r.width < minTap - 0.5) out.push((el.textContent || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 30) + ' is ' + Math.round(r.width) + 'x' + Math.round(r.height));
                if (r.right > window.innerWidth + 1) out.push('off the right edge: ' + (el.textContent || el.tagName).trim().slice(0, 30));
            }
            for (const el of document.querySelectorAll('input[type=checkbox]')) {
                const r = el.getBoundingClientRect();
                if (r.width && (r.width < 24 || r.height < 24)) out.push('checkbox too small');
            }
            return out;
        }""", MIN_TAP)

    def test_every_tab_fits_every_size_and_has_big_tap_targets(self):
        for size in SIZES:
            for tab in ("needs", "order", "ordered", "slow"):
                page = self.open(size, tab)
                for summary in page.locator("#panel details.bin > summary").all():      # cars start closed: open them so their rows are checked too
                    summary.click()
                self.shot(page, f"{size}-{tab}")
                found = self.problems(page)
                self.assertEqual(found, [], f"{size} / {tab}: {found}")

    def test_dark_mode_phone_shots(self):
        for tab in ("needs", "order"):
            page = self.open("phone-390", tab, scheme="dark")
            self.shot(page, f"phone-390-dark-{tab}")
            self.assertEqual(self.problems(page), [])

    def test_tabs_sit_at_the_top_like_the_rest_of_the_site_and_switch_sections(self):
        page = self.open("phone-390", "needs")
        self.assertLess(page.locator("#tabs").bounding_box()["y"], 400)             # near the top, not a bottom bar
        self.assertEqual(page.locator("#tabs button").count(), 4)
        self.assertIn("Home", page.locator(".topbar").inner_text())                  # the same top bar as Manage Accounts
        page.locator("#tabs button[data-tab=order]").click()
        page.wait_for_selector("#panel .bin")
        self.assertIn("Order list", page.locator("#tabs button[aria-selected=true]").inner_text())
        page.locator("#tabs button[data-tab=ordered]").click()
        page.wait_for_selector("#panel .bin")
        self.assertTrue(page.locator("text=Overdue").count() >= 1)

    def test_it_uses_the_site_font_and_colours(self):
        page = self.open("phone-390", "needs")
        self.assertIn("Outfit", page.evaluate("getComputedStyle(document.body).fontFamily"))
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#tabs button[aria-selected=true]')).backgroundColor"), "rgb(31, 111, 235)")

    def test_stepper_add_and_remove_work_by_tapping(self):
        page = self.open("phone-390", "needs")
        page.locator("#panel details.bin > summary").first.click()                  # cars start closed
        first = page.locator("#panel .item", has=page.locator("button:has-text('Add to order list')")).first
        item = page.locator("#panel .item", has_text=first.locator(".name").inner_text()).first     # pinned by name: its button text changes
        before = int(item.locator("input[type=number]").input_value())
        item.locator("button[aria-label='One more']").click()
        self.assertEqual(int(item.locator("input[type=number]").input_value()), before + 1)
        item.locator("button:has-text('Add to order list')").click()
        page.wait_for_selector("#msg.success")
        self.assertIn("✓ On list", item.locator("button.secondary:has-text(\"On list\")").inner_text())
        item.locator("button:has-text('Remove from list')").click()
        page.wait_for_function("document.querySelector('#msg').textContent.includes('taken off')")
        self.assertTrue(item.locator("button:has-text('Add to order list')").is_visible())

    def test_cars_are_grouped_and_every_control_has_hover_text(self):
        page = self.open("phone-390", "needs")
        self.assertGreaterEqual(page.locator("#panel details.bin").count(), 2)           # items sit under their car
        self.assertIn("ALTO", page.locator("#panel details.bin summary b").first.inner_text() + page.locator("#panel").inner_text())
        missing = page.evaluate("""() => Array.from(document.querySelectorAll('#panel button, #tabs button, #refreshBtn, #panel input, #panel select, .jump'))
            .filter(e => e.getBoundingClientRect().width > 0 && !(e.getAttribute('title') || '').trim() && e.textContent.trim().length < 2 && !e.getAttribute('aria-label'))
            .map(e => e.outerHTML.slice(0, 80))""")
        self.assertEqual(missing, [])
        untitled = page.evaluate("""() => Array.from(document.querySelectorAll('#panel button, #tabs button, #refreshBtn'))
            .filter(e => e.getBoundingClientRect().width > 0 && !(e.getAttribute('title') || '').trim()).map(e => e.textContent.trim().slice(0, 30))""")
        self.assertEqual(untitled, [], "every button needs hover text")
        self.assertIn("Always check before you order", page.locator("#panel").inner_text())            # the disclaimer is on the page

    def test_a_search_jumps_to_the_list_that_has_the_matches(self):
        """A design that is only a Last piece must not leave you looking at an empty Sold out list."""
        page = self.open("phone-390", "needs")
        self.assertIn("Sold out", page.locator(".tile[aria-pressed=true]").inner_text())
        page.fill("input[aria-label=Search]", "ALTO K10 (2014-2019) LONG NAME FOR WRAPPING 1D MAT PREMIUM SEAT COVER BLACK/RED 1")
        page.press("input[aria-label=Search]", "Enter")
        page.wait_for_function("(document.querySelector('.tile[aria-pressed=true]') || {textContent: ''}).textContent.includes('Last piece')")
        self.assertIn("which does", page.locator("#panel .notice.info").inner_text())
        page.locator("#panel details.bin > summary").first.click()
        self.assertIn("only 1 left", page.locator("#panel details.bin").first.inner_text())
        # tapping the other card shows where the matches are instead of an unexplained empty list
        page.locator(".tile", has_text="Sold out").click()
        page.wait_for_selector("#panel .empty")
        self.assertIn("matches in \u201cLast piece\u201d", page.locator("#panel .empty").inner_text())

    def test_the_search_has_a_clear_button_and_the_old_extras_are_gone(self):
        page = self.open("phone-390", "needs")
        text = page.locator("#panel").inner_text()
        for gone in ("Open all", "Close all", "Whole catalogue"):
            self.assertNotIn(gone, text)
        self.assertFalse(page.locator("button[aria-label='Clear search']").is_visible())      # nothing typed yet
        page.fill("input[aria-label=Search]", "ALTO K10 (2014-2019) LONG NAME FOR WRAPPING 3D")
        self.assertTrue(page.locator("button[aria-label='Clear search']").is_visible())
        page.press("input[aria-label=Search]", "Enter")
        page.wait_for_function("(document.querySelector('.tile[aria-pressed=true]') || {textContent: ''}).textContent.includes('Sold out')")
        page.locator("button[aria-label='Clear search']").click()
        page.wait_for_function("document.querySelector('input[aria-label=Search]').value === '' && document.querySelectorAll('#panel details.bin').length >= 2")
        self.assertFalse(page.locator("button[aria-label='Clear search']").is_visible())
        box = page.locator(".search .field input").bounding_box()
        self.assertGreaterEqual(box["height"], 44)

    def test_every_car_is_listed_and_starts_closed(self):
        page = self.open("phone-390", "needs")
        bins = page.locator("#panel details.bin")
        self.assertEqual(bins.count(), 3)                                              # all three cars are there
        for index in range(bins.count()):
            self.assertIsNone(bins.nth(index).get_attribute("open"))                   # and every one is closed
        self.assertEqual(page.locator("#panel .item").count(), 0)                      # rows are only built when a car is opened
        self.assertEqual(page.locator("text=Show more").count(), 0)
        bins.first.locator("summary").click()
        self.assertGreaterEqual(page.locator("#panel .item").count(), 1)
        self.assertEqual(bins.first.get_attribute("open"), "")

    def test_a_long_list_adds_cars_in_big_steps(self):
        """140 sold-out designs over 70 cars: 30 cars first, then 50 more per tap, with no car split or lost."""
        hierarchy, stock, names = [], [], []
        for c in range(70):
            car = f"LONGCAR{c:02d}"
            names.append(car)
            hierarchy.append({"item_name": car, "qty": 2})
            for n in (1, 2):
                hierarchy.append({"item_name": f"{car} ITEM {n}", "qty": 1})
                stock.append({"item_name": f"{car} ITEM {n}", "qty": 1})
            hierarchy.append({"item_name": "", "qty": ""})
        pushed = {"car_master": names, "main_hierarchy": hierarchy, "item_stock": stock}
        self.assertEqual(support.client_for("anonymous").post("/admin/intake/sync_data", json=pushed, headers=dict(INTAKE_HEADERS, **{"X-Intake-Force": "1"})).status_code, 200)
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM stock_state")
            conn.execute("DELETE FROM stock_events")                  # the page fixture's old stock changes would add cars of their own
            conn.execute("DELETE FROM sales_lines")
            conn.executemany("INSERT INTO sales_lines VALUES (?, '2026-03-10', 'sale', ?, ?, 2)", [(f"{n:08x}", row["item_name"].upper(), row["item_name"]) for n, row in enumerate(stock)])
        self.app_module._stock_mgmt_cache_clear()
        page = self.open("phone-390", "needs")
        page.wait_for_function("document.querySelectorAll('#panel details.bin').length === 30")
        button = page.locator("#panel button", has_text="more cars")
        self.assertEqual(button.count(), 1)
        self.assertIn("50 more cars", button.inner_text())
        button.click()
        page.wait_for_function("document.querySelectorAll('#panel details.bin').length === 70")        # 30 + 50 is more than are left: all 70
        self.assertEqual(page.locator("#panel button", has_text="more cars").count(), 0)
        titles = page.evaluate("Array.from(document.querySelectorAll('#panel details.bin summary b')).map(e => e.textContent)")
        self.assertEqual(len(set(titles)), 70)                                                          # no car shown twice or lost

    def test_settings_are_at_the_top_and_save_and_a_short_list_has_no_more_button(self):
        page = self.open("phone-390", "needs")
        self.assertEqual(page.locator("#panel > details.settings").count(), 1)
        self.assertEqual(page.evaluate("document.querySelector('#panel').firstElementChild.className"), "settings")        # the very first thing on the tab
        self.assertEqual(page.locator("#panel button", has_text="more cars").count(), 0)
        page.locator("#panel details.settings > summary").click()
        page.fill("#panel details.settings input[type=number] >> nth=0", "2")                                             # most pieces you keep
        page.locator("#panel details.settings button", has_text="Save settings").click()
        page.wait_for_selector("#msg.success")
        with self.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT value FROM stock_mgmt_settings WHERE key = 'keep_max'").fetchone()[0], "2")

    def meta(self, **values):
        with self.db.get_connection() as conn:
            for key, value in values.items():
                conn.execute("INSERT OR REPLACE INTO capture_meta VALUES (?, ?)", (key, value))
        self.app_module._stock_mgmt_cache_clear()

    def stamp(self, hours_ago):
        return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")

    def sales_box(self, page):
        page.locator("#salesBox > summary").click()
        page.wait_for_selector("#salesBody > div")                                 # the box replaced its starting text with what the site answered
        return page.inner_text("#salesBody")

    def test_the_history_note_shows_while_the_history_is_still_loading_and_goes_when_it_reaches_the_window(self):
        page = self.open("phone-390", "needs")              # the fixture's sales start 10 days ago, the window is 90 days, no coverage is recorded
        self.assertEqual(page.locator("#historyNote").count(), 1)
        self.assertEqual(page.inner_text("#historyNote"), f"Based on sales since {day(10)}. Older history is still loading.")
        self.assertGreater(page.locator("#panel .bin").count(), 0)                    # the lists are still there below it
        self.meta(sales_covered_from=day(200))              # the shop's pushes reached back far enough
        page = self.open("phone-390", "needs")
        self.assertEqual(page.locator("#historyNote").count(), 0)
        self.assertGreater(page.locator("#panel .bin").count(), 0)

    def test_the_sales_box_shows_received_refused_and_silent_shop_lines_only_when_they_apply(self):
        # nothing recorded yet (older data): no warning and no refusal
        page = self.open("phone-390", "needs")
        self.sales_box(page)
        self.assertEqual((page.locator("#salesNoNews").count(), page.locator("#salesRefused").count()), (0, 0))
        # received 40 hours ago, and a refusal after that: both warnings show
        self.meta(sales_last_accepted='{"utc": "%s", "from": "2026-02-01", "to": "2026-02-28", "lines": 5}' % self.stamp(40),
                  sales_last_refused='{"utc": "%s", "status": 409, "reason": "company mismatch", "from": null, "to": null, "lines": null}' % self.stamp(2))
        page = self.open("phone-390", "needs")
        self.sales_box(page)
        self.assertIn("Last sales received: 2026-03-18 20:00", page.inner_text("#salesReceived"))
        self.assertEqual(page.inner_text("#salesNoNews"), "No sales received from the shop for 40 hours. Check the shop PC and Tally.")
        self.assertEqual(page.inner_text("#salesRefused"), "The shop's last push was refused (409) on 2026-03-20 10:00: company mismatch")
        self.assertTrue(page.locator("#salesRefused").is_visible() and page.locator("#salesNoNews").is_visible())
        # a later accepted push: the refusal and the warning are gone
        self.meta(sales_last_accepted='{"utc": "%s", "from": "2026-03-01", "to": "2026-03-31", "lines": 9}' % self.stamp(1))
        page = self.open("phone-390", "needs")
        self.sales_box(page)
        self.assertEqual((page.locator("#salesNoNews").count(), page.locator("#salesRefused").count()), (0, 0))
        self.assertIn("Last sales received: 2026-03-20 11:00", page.inner_text("#salesReceived"))

    def test_the_tab_counts_show(self):
        page = self.open("phone-390", "needs")
        page.wait_for_function("document.querySelector('[data-count=order]').textContent === '3'")
        self.assertEqual(page.locator("[data-count=ordered]").inner_text(), "2")
        self.assertIn("hot", page.locator("[data-count=ordered]").get_attribute("class"))      # one is overdue


if __name__ == "__main__":
    unittest.main()
