"""feeder.py refresh triggers, against a mock Tally and a mock intake (separate process, throw-away copy):
new item -> hierarchy refresh in the next cycle, 10-minute throttle (shortened here), back-off after a Tally
error, the refresh_now.flag file, items that stay unmatched not looping, and a Tally error never killing the feeder.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.test_deploy_checks import clean_env, free_port, make_copy

THROTTLE_MIN = 0.15          # minutes -> 9 s


class Rig:
    """Mock Tally + mock intake + a running feeder.py in a scratch copy."""

    def __init__(self, **env):
        self.cars = ["ALTO", "SWIFT"]
        self.items = {"ALTO": [("ALTO 7D MAT BLACK", 3), ("ALTO 9D MAT RED", 2)], "SWIFT": [("SWIFT 7D MAT GREY", 4)]}
        self.orphans = []            # in stock in Tally but never in the hierarchy
        self.fail_all = False        # Tally answers every request with an error
        self.fail_hierarchy = False  # ... only the master/hierarchy requests (stock still works)
        self.intake_status = 200     # what the mock intake answers to a push
        self.requests = []           # (time, kind)
        self.pushes = []             # (time, body)
        rig = self

        class Tally(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
                out = rig.reply(body).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass

        class Intake(BaseHTTPRequestHandler):
            def do_POST(self):
                rig.pushes.append((time.time(), json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))))
                out = b'{"ok": true}'; self.send_response(rig.intake_status); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass

        tp, ip = free_port(), free_port()
        self.tally_port, self.intake_port = tp, ip
        self.servers = [HTTPServer(("127.0.0.1", tp), Tally), HTTPServer(("127.0.0.1", ip), Intake)]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.dir = make_copy("refresh_")
        open(os.path.join(self.dir, ".env"), "w", newline="").write(
            f"MACHINE_ROLE=feeder\r\nTALLY_URL=http://127.0.0.1:{tp}\r\nCLOUD_SYNC_URL=http://127.0.0.1:{ip}/admin/intake/sync_data\r\nCLOUD_SYNC_TOKEN=tok123\r\n")
        self.flag = os.path.join(self.dir, "refresh_now.flag")
        self.env = clean_env(TALLY_RETRY_ATTEMPTS="1", TALLY_EXPORT_INTERVAL="2", FEEDER_NEW_ITEM_MIN_MINUTES=str(THROTTLE_MIN), **env)
        self.proc = None

    def reply(self, b):
        every = [(n, q) for k in self.items.values() for n, q in k]
        if self.fail_all or (self.fail_hierarchy and ("List of Stock Groups" in b or "List of Stock Items with Parent" in b or "Stock Summary" in b)):
            self.requests.append((time.time(), "error")); return "<ENVELOPE><LINEERROR>boom</LINEERROR></ENVELOPE>"
        if "List of Stock Groups" in b:
            self.requests.append((time.time(), "cars"))
            return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(f'<STOCKGROUP NAME="{c}"/>' for c in self.cars) + "</COLLECTION></DATA></BODY></ENVELOPE>"
        if "List of Stock Items with Parent" in b:
            self.requests.append((time.time(), "hierarchy"))
            return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(f'<STOCKITEM NAME="{n}" PARENT="{p}"/>' for p, k in self.items.items() for n, _ in k) + "</COLLECTION></DATA></BODY></ENVELOPE>"
        if "Stock Summary" in b:
            return "<ENVELOPE><BODY><DATA>" + "".join(f"<DSPACCNAME><DSPDISPNAME>{n}</DSPDISPNAME></DSPACCNAME><DSPSTKINFO><DSPSTKCL><DSPCLQTY>{q} pcs</DSPCLQTY></DSPSTKCL></DSPSTKINFO>" for n, q in every) + "</DATA></BODY></ENVELOPE>"
        if "Item Names With Closing" in b:
            self.requests.append((time.time(), "stock"))
            return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(f'<STOCKITEM NAME="{n}"><CLOSINGBALANCE>{q} pcs</CLOSINGBALANCE></STOCKITEM>' for n, q in every + self.orphans) + "</COLLECTION></DATA></BODY></ENVELOPE>"
        return "<ENVELOPE><LINEERROR>x</LINEERROR></ENVELOPE>"

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "feeder.py"], cwd=self.dir, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self

    def log(self):
        try:
            return open(os.path.join(self.dir, "logs", "feeder.log"), encoding="utf-8", errors="replace").read()
        except OSError:
            return ""

    def reasons(self):
        return [line.split("reason=")[1].split(",")[0].strip() for line in self.log().splitlines() if "Full refresh starting: reason=" in line]

    def wait(self, predicate, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.25)
        return False

    def pushed_names(self):
        return {r["item_name"] for r in self.pushes[-1][1]["item_stock"]} if self.pushes else set()

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try: self.proc.wait(10)
            except Exception: self.proc.kill()
        for s in self.servers:
            s.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)


class FeederRefreshTriggers(unittest.TestCase):
    def rig(self, **env):
        r = Rig(**env); self.addCleanup(r.stop); return r.start()

    def test_startup_refresh_then_quiet_cycles_do_not_refresh(self):
        r = self.rig()
        self.assertTrue(r.wait(lambda: len(r.pushes) >= 3))
        self.assertEqual(r.reasons(), ["startup"])
        self.assertIn("Tally export", r.log())              # timing is logged
        self.assertEqual(sum(1 for _t, k in r.requests if k == "hierarchy"), 1, "ordinary cycles must not touch the hierarchy")

    def test_new_item_appears_in_the_next_push(self):
        r = self.rig()
        self.assertTrue(r.wait(lambda: len(r.pushes) >= 2))
        before = len(r.pushes)
        r.items["ALTO"].append(("ALTO 11D NEW ITEM", 5))
        self.assertTrue(r.wait(lambda: "ALTO 11D NEW ITEM" in r.pushed_names(), 30), "new item never reached the intake")
        self.assertIn("ALTO 11D NEW ITEM", json.dumps(r.pushes[-1][1]["main_hierarchy"]), "hierarchy must carry it too")
        self.assertEqual(r.reasons(), ["startup", "new item"])
        self.assertLess(len(r.pushes) - before, 4, "should be picked up within a cycle or two")
        # payload shape is unchanged
        self.assertEqual(set(r.pushes[-1][1]), {"car_master", "main_hierarchy", "item_stock"})

    def test_throttle_and_items_that_stay_unmatched(self):
        r = self.rig()
        r.orphans = [("STOCK OUTSIDE ANY CAR", 9)]      # never in the hierarchy: must not cause refresh loops
        self.assertTrue(r.wait(lambda: len(r.pushes) >= 2))
        r.items["ALTO"].append(("ALTO NEW ONE", 1))
        self.assertTrue(r.wait(lambda: r.reasons().count("new item") == 1, 30))
        self.assertTrue(r.wait(lambda: "Full refresh from Tally completed: reason=new item" in r.log(), 10))      # the first refresh is over before the second item appears
        first = time.time()
        r.items["SWIFT"].append(("SWIFT NEW TWO", 1))    # a second new item straight after
        self.assertTrue(r.wait(lambda: "throttled" in r.log(), 20), "second request should be throttled")
        self.assertEqual(r.reasons().count("new item"), 1)
        self.assertTrue(r.wait(lambda: r.reasons().count("new item") == 2, 40))
        self.assertGreaterEqual(time.time() - first, THROTTLE_MIN * 60 - 4, "second new-item refresh came before the throttle gap")
        time.sleep(7)                                    # the orphan alone never triggered anything
        self.assertEqual(r.reasons(), ["startup", "new item", "new item"])
        self.assertNotIn("STOCK OUTSIDE ANY CAR", r.pushed_names())

    def test_manual_flag(self):
        r = self.rig()
        self.assertTrue(r.wait(lambda: len(r.pushes) >= 2))
        open(r.flag, "w").write("x")
        self.assertTrue(r.wait(lambda: r.reasons().count("manual") == 1, 30))
        self.assertTrue(r.wait(lambda: not os.path.exists(r.flag), 10), "flag must be removed after the refresh")
        self.assertIn("Manual refresh flag cleared", r.log())
        time.sleep(5)
        self.assertEqual(r.reasons(), ["startup", "manual"])

    def test_tally_errors_never_kill_the_feeder_and_back_off(self):
        r = self.rig()
        self.assertTrue(r.wait(lambda: len(r.pushes) >= 2))
        r.fail_hierarchy = True                          # new item appears, but the hierarchy export fails
        r.items["ALTO"].append(("ALTO FAILING ITEM", 1))
        self.assertTrue(r.wait(lambda: "New-item refresh failed" in r.log(), 30))
        self.assertIn("next one allowed in", r.log())
        n = len(r.pushes)
        self.assertTrue(r.wait(lambda: len(r.pushes) > n, 20), "normal pushes must continue while the refresh is failing")
        r.fail_all = True                                # whole Tally down, with a manual request pending
        open(r.flag, "w").write("x")
        self.assertTrue(r.wait(lambda: "Full refresh (manual) failed" in r.log() or "Full refresh FAILED: reason=manual" in r.log(), 30))
        self.assertIsNone(r.proc.poll(), "feeder died on a Tally error")
        self.assertTrue(os.path.exists(r.flag), "flag must stay when the refresh failed")
        failures = r.log().count("Full refresh FAILED: reason=manual")
        time.sleep(1)
        r.fail_all = False; r.fail_hierarchy = False     # Tally is back: the pending request is honoured
        self.assertTrue(r.wait(lambda: not os.path.exists(r.flag), 30))
        self.assertGreaterEqual(r.log().count("Full refresh FAILED: reason=manual"), failures)
        self.assertTrue(r.wait(lambda: "ALTO FAILING ITEM" in r.pushed_names(), 30))
        self.assertIsNone(r.proc.poll())


class BackoffByDuration(unittest.TestCase):
    """NewItemWatch gap after refreshes of 30 s (normal, ~what production takes), 150 s and 600 s, and after failures."""

    def test_gap_after_simulated_durations(self):
        d = make_copy("backoff_"); self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        code = "\n".join([
            "import os",
            "os.environ.update(CLOUD_SYNC_URL='http://127.0.0.1:9/x', CLOUD_SYNC_TOKEN='t')",
            "import feeder",
            "w = feeder.NewItemWatch(); base = w.gap; out = []",
            "def step(label, ok, secs):",
            "    w.attempt_finished(ok, secs); out.append((label, w.gap // 60))",
            "step('30s', True, 30); step('30s', True, 30)",
            "step('150s', True, 150)",
            "step('600s', True, 600); step('600s', True, 600); step('failed', False, 0)",
            "for i in range(5): step('failed', False, 0)",
            "step('30s again', True, 30)",
            "print('BASE', base // 60); print('OUT', out)",
        ])
        r = subprocess.run([sys.executable, "-c", code], cwd=d, env=clean_env(), capture_output=True, text=True, timeout=120)
        self.assertIn("BASE 10", r.stdout, r.stderr[-500:])
        got = eval(r.stdout.split("OUT ")[1])
        self.assertEqual(got[:3], [("30s", 10), ("30s", 10), ("150s", 10)], "a normal refresh must never double the gap")
        self.assertEqual(got[3:6], [("600s", 20), ("600s", 40), ("failed", 60)], "slow/failed refreshes double it, capped at 60 min")
        self.assertEqual(got[-2], ("failed", 60))
        self.assertEqual(got[-1], ("30s again", 10), "one normal-duration success resets the back-off")


if __name__ == "__main__":
    unittest.main()
