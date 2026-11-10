"""The "The app was updated" banner and its idle-only auto-reload, in a REAL browser (headless Microsoft Edge
through Playwright). The app runs in a thread of this test process against the scratch copy's fake data, so
the real data/ folder is never touched. A "deploy" is simulated by changing app.APP_BUILD_ID while the
browser tab stays open; the tab's own embedded id is the old one, exactly like a tab left open for days.

What this does not cover: Edge only (not Chrome/Safari/a phone), and the restart-stability of the id itself is
covered by tests/test_app_build_id.py with real new processes."""
import threading
import unittest

from tests import support

try:
    from playwright.sync_api import sync_playwright
except Exception:       # pragma: no cover
    sync_playwright = None

BANNER = "#appBuildBanner"
# A share that never finishes until the test says so (real navigator.share needs a user gesture + a share target).
STUB_SHARE = """
window.__shareCalls = 0;
Object.defineProperty(navigator, 'canShare', {value: () => true, configurable: true});
Object.defineProperty(navigator, 'share', {configurable: true, value: () => new Promise(resolve => {
    window.__shareCalls += 1; window.__finishShare = resolve; }) });
"""


class BannerBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("playwright is not installed")
        cls.app_module, cls.db = support.bootstrap()
        from werkzeug.serving import make_server
        cls.server = make_server("127.0.0.1", 0, cls.app_module.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.states = {}      # logged-in cookies per user: the app throttles repeated logins from one address
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:
            cls.pw.stop()
            raise unittest.SkipTest(f"Microsoft Edge could not be started: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.server.shutdown()

    def setUp(self):
        self._saved_id = self.app_module.APP_BUILD_ID
        self.app_module.APP_BUILD_ID = "build-A"
        self.context = None

    def storage_state(self, user, code):
        if user not in self.states:
            context = self.browser.new_context()
            page = context.new_page()
            page.goto(self.base + "/login")
            page.fill("input[name=username]", user)
            page.fill("input[name=access_code]", code)
            page.click("button[type=submit], input[type=submit]")
            page.wait_for_url(lambda url: "/login" not in url)
            self.states[user] = context.storage_state()
            context.close()
        return self.states[user]

    def tearDown(self):
        self.app_module.APP_BUILD_ID = self._saved_id
        if self.context:
            self.context.close()

    # ---- helpers
    def open_car_page(self, user="star", code="111", stub_share=False, dismiss_popup=True):
        if self.context:
            self.context.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900}, storage_state=self.storage_state(user, code))
        self.page = self.context.new_page()
        self.last_update_calls = []
        self.page.on("request", lambda r: self.last_update_calls.append(r.url) if "/last_update" in r.url else None)
        self.page.set_default_timeout(15000)
        if stub_share:
            self.page.add_init_script(STUB_SHARE)
        self.page.goto(self.base + "/?car=ALTO")
        self.page.wait_for_selector("#designsList .design-item[data-image-id]")
        if dismiss_popup:
            self.page.wait_for_timeout(500)
            if self.page.locator(".notice-popup-x").count():
                self.page.click(".notice-popup-x")                  # the dealer popup is remembered as dismissed
        self.page.evaluate("window.__tabMarker = 'old-tab'")      # survives only if the page is NOT reloaded

    def deploy(self, build="build-B"):
        self.app_module.APP_BUILD_ID = build

    def check_now(self):
        """What visibilitychange / pageshow do: an immediate /last_update check."""
        with self.page.expect_response(lambda r: "/last_update" in r.url):
            self.page.evaluate("DataSync.checkNow()")
        self.page.wait_for_timeout(300)

    def marker(self):
        return self.page.evaluate("window.__tabMarker")

    def banner_visible(self):
        return self.page.locator(BANNER).count() == 1 and self.page.locator(BANNER).is_visible()

    def assert_banner_and_no_reload(self, why):
        self.assertTrue(self.banner_visible(), f"banner should show ({why})")
        self.assertEqual(self.marker(), "old-tab", f"must NOT reload ({why})")
        self.assertEqual(self.page.locator(BANNER).inner_text(), "The app was updated. Tap to reload.")

    # ---- the flows
    def test_same_build_shows_nothing_and_never_reloads(self):
        self.open_car_page()
        self.check_now(); self.check_now()
        self.assertFalse(self.banner_visible())
        self.assertEqual(self.marker(), "old-tab")

    def test_idle_tab_reloads_itself_onto_the_new_code(self):
        self.open_car_page()
        self.assertIn('"build-A"', self.page.content())
        self.deploy("build-B")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.page.wait_for_selector("#designsList .design-item[data-image-id]")
        self.assertIsNone(self.marker(), "the page was reloaded")
        self.assertIn('"build-B"', self.page.content(), "and it now carries the new build id")
        self.assertFalse(self.banner_visible())
        self.assertIn("car=ALTO", self.page.url, "the open car survives the reload")

    def test_pageshow_and_visibilitychange_both_check(self):
        self.open_car_page()
        self.page.evaluate("window.DataSync.checkNow && 0")
        before = len(self.last_update_calls)
        self.page.evaluate("window.dispatchEvent(new Event('pageshow'))")
        self.page.wait_for_timeout(600)
        self.assertEqual(len(self.last_update_calls), before + 1, "pageshow runs exactly one check")
        before = len(self.last_update_calls)
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        self.page.wait_for_timeout(600)
        self.assertEqual(len(self.last_update_calls), before + 1, "visibilitychange (visible) runs exactly one check")
        # and they actually detect the update
        self.deploy("build-B")
        with self.page.expect_navigation():
            self.page.evaluate("window.dispatchEvent(new Event('pageshow'))")
        self.assertIsNone(self.marker())

    def test_selection_mode_defers_then_reloads_when_it_ends(self):
        self.open_car_page()
        self.page.evaluate("enterSelectMode()")
        self.page.click("#designsList .design-item[data-image-id]")
        self.assertTrue(self.page.evaluate("selectMode && selectedImageIds.size === 1"))
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("selection mode")
        self.check_now(); self.check_now()
        self.assert_banner_and_no_reload("selection mode, still")
        self.page.evaluate("exitSelectMode()")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.assertIsNone(self.marker())

    def test_open_modal_defers(self):
        self.open_car_page()
        self.page.evaluate("openImageModal(document.querySelector('#designsList .thumbnail-button[data-src]'))")
        self.assertTrue(self.page.evaluate("document.querySelector('#imageModal').classList.contains('open')"))
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("image modal open")
        self.page.evaluate("closeImageModal()")
        self.page.wait_for_timeout(300)
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")

    def test_share_in_progress_defers_and_even_a_tap_does_not_reload(self):
        self.open_car_page(stub_share=True)
        self.page.evaluate("enterSelectMode()")
        self.page.click("#designsList .design-item[data-image-id]")
        self.page.click("#copySelectedBtn")
        self.page.wait_for_function("window.__shareCalls === 1")
        self.assertTrue(self.page.evaluate("shareInProgress"))
        self.deploy("build-B")
        self.check_now(); self.check_now()
        self.assert_banner_and_no_reload("share in progress")
        self.page.click(BANNER + " button")                         # the person taps the banner mid-share
        self.page.wait_for_timeout(400)
        self.assertEqual(self.marker(), "old-tab", "a tap never reloads during a share")
        self.assertIn("Finish sharing", self.page.locator(BANNER).inner_text())
        # the share ends -> exitSelectMode() -> the page is idle -> the next check reloads
        self.page.evaluate("window.__finishShare()")
        self.page.wait_for_function("!shareInProgress")
        self.page.wait_for_timeout(300)
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.assertIsNone(self.marker())

    def test_open_notice_popup_defers(self):
        self.open_car_page(dismiss_popup=False)
        self.page.wait_for_selector(".notice-popup-x")
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("customer notice popup open")
        self.page.click(".notice-popup-x")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")

    def test_waiting_between_share_batches_defers(self):
        self.open_car_page()
        self.page.evaluate("batchContinuePending = true")
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("batch-continue bar waiting")

    def test_typed_text_in_the_car_search_box_defers(self):
        self.open_car_page()
        self.page.evaluate("document.querySelector('.select2-selection').dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))")
        self.page.wait_for_selector(".select2-search__field")
        self.page.fill(".select2-search__field", "swi")
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("text typed in the search box")

    def test_empty_focused_field_does_not_block_the_update(self):
        """A focused but empty box must not stop the reload forever on a desktop."""
        self.open_car_page()
        self.page.evaluate("""() => { const i = document.createElement('input'); i.id = 'probeField'; i.type = 'text';
            document.body.appendChild(i); i.focus(); }""")
        self.deploy("build-B")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.assertIsNone(self.marker())

    def test_typed_text_in_any_visible_field_defers(self):
        self.open_car_page()
        self.page.evaluate("""() => { const i = document.createElement('input'); i.id = 'probeField'; i.type = 'text';
            document.body.appendChild(i); }""")
        self.page.fill("#probeField", "half-typed")
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("typed text in a visible field")
        self.page.fill("#probeField", "")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.assertIsNone(self.marker(), "once the field is empty the page is idle again")

    def test_tap_on_the_banner_reloads_when_not_sharing(self):
        self.open_car_page()
        self.page.evaluate("enterSelectMode()")
        self.page.click("#designsList .design-item[data-image-id]")
        self.deploy("build-B")
        self.check_now()
        self.assert_banner_and_no_reload("selection mode")
        with self.page.expect_navigation():
            self.page.click(BANNER + " button")
        self.assertIsNone(self.marker())
        self.assertIn('"build-B"', self.page.content())

    def test_auto_reload_happens_once_per_new_build_then_stops_at_the_banner(self):
        self.open_car_page()
        # a reload to build-B was already attempted in this tab (e.g. the reloaded page was an old cached copy)
        self.page.evaluate("window.sessionStorage.setItem('appBuildAutoReloadTarget', 'build-B')")
        self.deploy("build-B")
        self.check_now(); self.check_now()
        self.assert_banner_and_no_reload("the one automatic reload for build-B was already used")
        # a different, newer build is a new target and may auto-reload again
        self.deploy("build-C")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.assertIsNone(self.marker())

    def test_polling_cost_is_unchanged(self):
        """Same /last_update, same number of requests as before the feature; nothing new is fetched."""
        self.open_car_page()
        urls = []
        self.page.on("request", lambda r: urls.append(r.url.replace(self.base, "")))
        for _ in range(3):
            self.check_now()
        self.assertEqual([u for u in urls if "/last_update" in u], ["/last_update"] * 3, "one request per check")
        self.assertEqual([u for u in urls if "/last_update" not in u], [], "no other request is made by a check")
        # the timer cadence in the page is exactly what it was (customers 60 s)
        self.assertIn("intervalMs: isAdmin ? 30000 : 60000", open("templates/index.html", encoding="utf-8").read())

    def test_admin_page_gets_the_banner_through_its_own_poll(self):
        self.open_car_page(user="admin", code="idris123")
        self.assertTrue(self.page.evaluate("typeof DataSync === 'object'"))
        self.deploy("build-B")
        with self.page.expect_navigation():
            self.page.evaluate("DataSync.checkNow()")
        self.assertIsNone(self.marker())

    def test_train_page_only_shows_the_banner_never_reloads_itself(self):
        self.open_car_page(user="admin", code="idris123")
        self.page.goto(self.base + "/train")
        self.page.wait_for_function("typeof DataSync === 'object'")
        self.page.evaluate("window.__tabMarker = 'old-tab'")
        self.deploy("build-B")
        self.check_now(); self.check_now()
        self.assert_banner_and_no_reload("train page is banner-only")


if __name__ == "__main__":
    unittest.main()
