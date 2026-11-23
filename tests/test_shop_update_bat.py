"""tools/shop/SHOP_UPDATE.bat, the paste-into-Notepad shop-PC updater, run for real under cmd.exe.

Real: cmd.exe, git (a real origin repo + a shop clone at an OLD commit), the real Python venv (junctioned), the real
feeder.py of this checkout talking to a mock Tally + mock intake, serve.py's real guard.
Fake: only schtasks (SHOP_UPDATE_SCHTASKS), which starts/kills feeder.py instead of touching Windows tasks.
Every scenario runs with LF-only line endings (what pasting GitHub's raw view can give) AND with CRLF; the good
update also with a UTF-8 BOM. Windows only; never touches a real task, repo, data folder or .env.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.support import ROOT
from tests.test_deploy_checks import clean_env, free_port
from tests.test_feeder_refresh import Rig

BAT = os.path.join(ROOT, "tools", "shop", "SHOP_UPDATE.bat")
TOKEN = "tok123"
CODE_SKIP = shutil.ignore_patterns("data", "logs", ".git", ".venv", "__pycache__", "tests", ".env", "*.db", "brag-output",
                                   "ai_tryon_test", "archived_unneeded", "node_modules", ".state_snapshot", "SHOP_UPDATE_*")
FAKE = r'''import json, os, subprocess, sys
st = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
open(st["calls"], "a").write(" ".join(args) + "\n")
folder = st["folder"]
if args[0] == "/Query":
    if "/TN" in args:
        sys.exit(0 if st["exists"] and args[args.index("/TN") + 1].lstrip("\\") == st["task"] else 1)
    rows = ['"PC","\\OtherTask","N/A","Ready","x","N/A","0","u","C:\\elsewhere\\run.exe","C:\\elsewhere","x"',
            '"PC","\\Old Tally Task","N/A","Ready","x","N/A","0","u","C:\\elsewhere\\t.exe SECRETARG","C:\\elsewhere","x"',
            '"PC","\\Nightly","N/A","Ready","x","N/A","0","u","C:\\other\\feeder.bat SECRETARG","C:\\other","x"']
    if st["listed"]:
        rows.append('"PC","\\%s","N/A","Ready","x","N/A","0","u","%s\\.venv\\Scripts\\python.exe feeder.py","%s","x"' % (st["task"], folder, folder))
    print("\n".join(rows)); sys.exit(0)
if args[0] == "/End":
    if not st["end_noop"] and os.path.exists(st["pids"]):
        for pid in open(st["pids"]).read().split():
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
        os.remove(st["pids"])
    sys.exit(0)
if args[0] == "/Run":
    if st["run_fails"]: sys.exit(1)
    if st["silent"]: sys.exit(0)
    p = subprocess.Popen([os.path.join(folder, ".venv", "Scripts", "python.exe"), "feeder.py"], cwd=folder, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000 | 0x00000200)   # CREATE_NO_WINDOW | NEW_PROCESS_GROUP: a detached venv python.exe opens a console window for the interpreter it starts
    open(st["pids"], "a").write("%d\n" % p.pid); open(st["allpids"], "a").write("%d\n" % p.pid)
    sys.exit(0)
sys.exit(0)
'''
OLD_FEEDER = "import os, time\nos.makedirs('logs', exist_ok=True)\nf = open('logs/feeder.log', 'a')\nf.write('OLD FEEDER STARTED\\n'); f.flush()\ntime.sleep(3600)\n"


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


class Deny401(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="x"'); self.send_header("Content-Length", "0"); self.end_headers()
    do_POST = do_GET
    def log_message(self, *a): pass


@unittest.skipUnless(sys.platform == "win32" and sys.prefix != sys.base_prefix, "needs Windows and a venv to junction")
class ShopUpdateBat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = tempfile.mkdtemp(prefix="shopcode_")
        shutil.rmtree(cls.code); shutil.copytree(ROOT, cls.code, ignore=CODE_SKIP)
        cls.bat = open(BAT, "rb").read().replace(b"\r\n", b"\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.code, ignore_errors=True)

    # ---- scenario plumbing -------------------------------------------------------------------------------------
    def build(self, sabotage=None, env_lines=("MACHINE_ROLE=feeder",), env=True, listed=True, task="TallyFeeder", env_task=None,
              end_noop=False, silent=False, intake=200, remote=None, git_dir=True, keep_origin=True, feeder_file=True, bigdata=False, tally_down=False):
        root = tempfile.mkdtemp(prefix="shopupd_"); self.addCleanup(self.cleanup, root)
        seed, origin, shop = (os.path.join(root, n) for n in ("seed", "origin.git", "shop"))
        os.makedirs(seed); git(root, "init", "-q", "--bare", "-b", "main", origin); git(seed, "init", "-q", "-b", "main")
        open(os.path.join(seed, "feeder.py"), "w").write(OLD_FEEDER); open(os.path.join(seed, "serve.py"), "w").write("print('old web app')\n")
        open(os.path.join(seed, "requirements-feeder.txt"), "w").write("# nothing\n")
        git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "old"); old = git(seed, "rev-parse", "HEAD")
        for name in os.listdir(self.code):
            src = os.path.join(self.code, name)
            shutil.copytree(src, os.path.join(seed, name)) if os.path.isdir(src) else shutil.copy(src, seed)
        git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "new"); new = git(seed, "rev-parse", "HEAD")
        if sabotage:
            edit = {"pip": ("requirements-feeder.txt", "no-such-package-zz==9.9.9\n", "a"), "import": ("feeder.py", "raise RuntimeError('boom')\n", "w"),
                    "guard": ("serve.py", "print('web app running')\n", "w")}[sabotage]
            open(os.path.join(seed, edit[0]), edit[2]).write(edit[1])
            git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "sabotage"); new = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "-q", origin, "main")
        git(root, "clone", "-q", origin, shop); git(shop, "reset", "-q", "--hard", old)
        rig = Rig(); self.addCleanup(rig.stop); rig.intake_status = intake
        lines = list(env_lines) + [f"TALLY_URL=http://127.0.0.1:{free_port() if tally_down else rig.tally_port}", f"CLOUD_SYNC_URL=http://127.0.0.1:{rig.intake_port}/admin/intake/sync_data",
                                   f"CLOUD_SYNC_TOKEN={TOKEN}", "TALLY_RETRY_ATTEMPTS=1", "TALLY_EXPORT_INTERVAL=3"] + ([f"FEEDER_TASK_NAME={env_task}"] if env_task else [])
        if env:
            open(os.path.join(shop, ".env"), "wb").write(("\r\n".join(lines) + "\r\n").encode())
        os.makedirs(os.path.join(shop, "data")); open(os.path.join(shop, "data", "cache.json"), "w").write('{"k": 1}')
        for name in ("b.db", "c.flag", "skip.xlsx"):
            open(os.path.join(shop, "data", name), "w").write("x")
        if bigdata:                                   # like data\S.S IMAGE: thousands of files plus a very large one
            images = os.path.join(shop, "data", "S.S IMAGE", "CAR")
            os.makedirs(images)
            for i in range(3000):
                open(os.path.join(images, f"{i}.jpg"), "wb").write(b"x" * 100)
            with open(os.path.join(images, "huge.bin"), "wb") as big:
                big.seek(400 * 1024 * 1024 - 1); big.write(b"\0")
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(shop, ".venv"), sys.prefix], capture_output=True, check=True)
        if not feeder_file:
            os.remove(os.path.join(shop, "feeder.py"))
        if remote == "401":
            server = HTTPServer(("127.0.0.1", free_port()), Deny401); threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown); git(shop, "remote", "set-url", "origin", f"http://127.0.0.1:{server.server_port}/x.git")
        elif remote == "none":
            git(shop, "remote", "remove", "origin")
        if not git_dir:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", os.path.join(shop, ".git")], capture_output=True)   # git objects are read-only
        state = dict(folder=shop, task=task, exists=True, listed=listed, end_noop=end_noop, silent=False, run_fails=False,
                     calls=os.path.join(root, "calls.txt"), pids=os.path.join(root, "pids.txt"), allpids=os.path.join(root, "allpids.txt"))
        json.dump(state, open(os.path.join(root, "state.json"), "w"))
        open(os.path.join(root, "fake_schtasks.py"), "w").write(FAKE)
        open(os.path.join(root, "schtasks.cmd"), "w").write(f'@"{sys.executable}" "{os.path.join(root, "fake_schtasks.py")}" %*\r\n')
        env_vars = clean_env(FAKE_STATE=os.path.join(root, "state.json"), SHOP_UPDATE_SCHTASKS=os.path.join(root, "schtasks.cmd"),
                             PIP_INDEX_URL="http://127.0.0.1:9/simple", PIP_RETRIES="0", PIP_TIMEOUT="3", PIP_DISABLE_PIP_VERSION_CHECK="1")
        env_vars.pop("PYTHONUNBUFFERED", None)
        return dict(root=root, shop=shop, old=old, new=new, rig=rig, state=state, env=env_vars, silent=silent)

    def cleanup(self, root):
        pids = os.path.join(root, "allpids.txt")
        if os.path.exists(pids):
            for pid in open(pids).read().split():
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
        time.sleep(0.5)
        junction = os.path.join(root, "shop", ".venv")
        if os.path.exists(junction):
            os.rmdir(junction)               # removes the link only, never the venv it points to
        shutil.rmtree(root, ignore_errors=True)

    def start_old_feeder(self, s):
        """The shop's old feeder is running (and holds feeder.log open) when the update starts."""
        subprocess.run([s["env"]["SHOP_UPDATE_SCHTASKS"], "/Run", "/TN", s["state"]["task"]], env=s["env"], check=True)
        end = time.time() + 20
        while time.time() < end and "OLD FEEDER" not in self.read(s, "logs", "feeder.log"):
            time.sleep(0.3)

    def read(self, s, *parts):
        try:
            return open(os.path.join(s["shop"], *parts), encoding="utf-8", errors="replace").read()
        except OSError:
            return ""

    def run_bat(self, s, form, wait="60", cut=0):
        data = self.bat
        if cut:
            data = b"\n".join(data.rstrip(b"\n").split(b"\n")[:-cut]) + b"\n"
        if form == "crlf":
            data = data.replace(b"\n", b"\r\n")
        if form == "bom":
            data = b"\xef\xbb\xbf" + data
        open(os.path.join(s["shop"], "SHOP_UPDATE.bat"), "wb").write(data)
        env = dict(s["env"], SHOP_UPDATE_WAIT_SECONDS=wait)
        if s.get("nogit"):
            env["PATH"] = os.path.join(os.environ["SystemRoot"], "System32") + ";" + os.environ["SystemRoot"]
        r = subprocess.run(["cmd.exe", "/c", os.path.join(s["shop"], "SHOP_UPDATE.bat")], cwd=s["shop"], env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
        out = (r.stdout + r.stderr).decode("cp437", "replace").replace("\r", "")
        self.assertNotIn(TOKEN, out, "the sync token must never be printed")
        self.assertNotIn(TOKEN, self.read(s, "SHOP_UPDATE_log.txt"))
        return r.returncode, out

    def calls(self, s):
        try:
            return open(s["state"]["calls"]).read()
        except OSError:
            return ""

    def env_bytes(self, s):
        p = os.path.join(s["shop"], ".env")
        return open(p, "rb").read() if os.path.exists(p) else None

    # ---- the scenarios -----------------------------------------------------------------------------------------
    def check_good(self, form, env_lines, **opts):
        s = self.build(env_lines=env_lines, **opts); self.start_old_feeder(s)
        started = time.time(); code, out = self.run_bat(s, form)
        if opts.get("bigdata"):
            self.assertLess(time.time() - started, 150, "a 400 MB subfolder in data must not slow the update")
        self.assertEqual(code, 0, out[-1500:]); self.assertIn("PASS", out)
        self.assertEqual(git(s["shop"], "rev-parse", "HEAD"), s["new"])
        env = self.env_bytes(s).decode().splitlines()
        self.assertEqual([l for l in env if l.upper().startswith("MACHINE_ROLE")], ["MACHINE_ROLE=feeder"])
        bk = [d for d in os.listdir(s["shop"]) if d.startswith("SHOP_UPDATE_BACKUP_")]
        self.assertEqual(len(bk), 1); self.assertTrue(os.path.exists(os.path.join(s["shop"], bk[0], ".env")))
        self.assertTrue(os.path.exists(os.path.join(s["shop"], bk[0], "data", "cache.json")))
        self.assertEqual(sorted(os.listdir(os.path.join(s["shop"], bk[0], "data"))), ["b.db", "c.flag", "cache.json"], "only *.json, *.db, *.flag at the top of data")
        self.assertRegex(out, r"4 files, about \d+ KB"); self.assertIn("never touched", out)
        images = os.path.join(s["shop"], "data", "S.S IMAGE", "CAR")
        if os.path.isdir(images):
            self.assertEqual(len(os.listdir(images)), 3001); self.assertEqual(os.path.getsize(os.path.join(images, "huge.bin")), 400 * 1024 * 1024)
            self.assertLess(sum(os.path.getsize(os.path.join(d, f)) for d, _x, fs in os.walk(os.path.join(s["shop"], bk[0])) for f in fs), 1024 * 1024)
        log = self.read(s, "logs", "feeder.log")
        for text in ("machine role=feeder", "reason=startup", "Cloud sync push succeeded"):
            self.assertIn(text, log)
        self.assertNotIn("OLD FEEDER", log); self.assertTrue(any(f.startswith("feeder.log.before_update_") for f in os.listdir(os.path.join(s["shop"], "logs"))))
        self.assertTrue(s["rig"].pushes, "intake got nothing"); self.assertTrue(os.path.exists(os.path.join(s["shop"], "refresh_feeder_now.bat")))
        self.assertTrue(os.path.exists(os.path.join(s["shop"], "SHOP_UPDATE_log.txt")))

    def check_rolled_back(self, form, why, wait="60", **opts):
        s = self.build(**opts); self.start_old_feeder(s); before = self.env_bytes(s)
        code, out = self.run_bat(s, form, wait=wait)
        self.assertEqual(code, 1, out[-1500:]); self.assertIn("FAIL", out); self.assertIn(why, out)
        self.assertEqual(git(s["shop"], "rev-parse", "HEAD"), s["old"], "previous commit must be back")
        self.assertEqual(self.env_bytes(s), before, ".env must be restored exactly")
        self.assertTrue(self.calls(s).strip().splitlines()[-1].startswith("/Run /TN"), "task must be restarted last")
        end = time.time() + 20
        while time.time() < end and self.read(s, "logs", "feeder.log").count("OLD FEEDER STARTED") < 1:
            time.sleep(0.3)
        self.assertIn("OLD FEEDER STARTED", self.read(s, "logs", "feeder.log"), "the old feeder must be running again")

    def check_nothing_changed(self, form, why, code_expected=1, cut=0, nogit=False, **opts):
        s = self.build(**opts); s["nogit"] = nogit; before = self.env_bytes(s)
        code, out = self.run_bat(s, form, cut=cut)
        self.assertEqual(code, code_expected, out[-1500:]); self.assertIn(why, out)
        self.assertNotIn("PASS", out.replace("PASSWORD", ""))
        if os.path.isdir(os.path.join(s["shop"], ".git")):
            self.assertEqual(git(s["shop"], "rev-parse", "HEAD"), s["old"])
        self.assertEqual(self.env_bytes(s), before)
        self.assertNotIn("/End", self.calls(s)); self.assertNotIn("/Run", self.calls(s))

    FORMS = ("lf", "crlf")

    def test_good_update_adds_the_role_line(self):
        for form in self.FORMS + ("bom",):
            with self.subTest(form):
                self.check_good(form, env_lines=())

    def test_big_data_subfolder_is_skipped_and_untouched(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_good(form, env_lines=("MACHINE_ROLE=feeder",), bigdata=True)

    def test_good_update_with_the_role_already_there(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_good(form, env_lines=("MACHINE_ROLE=feeder",))

    def test_pip_failure_rolls_back(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_rolled_back(form, "pip could not install", sabotage="pip", env_lines=())

    def test_import_failure_rolls_back(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_rolled_back(form, "does not load", sabotage="import")

    def test_guard_failure_rolls_back(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_rolled_back(form, "safety guard did not stop serve.py", sabotage="guard")

    def test_feeder_never_pushes_rolls_back(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_rolled_back(form, "did not show all of", wait="12", intake=500)

    def test_old_feeder_that_will_not_stop_rolls_back(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_rolled_back(form, "still in use", end_noop=True)

    def test_git_asking_for_a_login_changes_nothing(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_nothing_changed(form, "git fetch failed", remote="401")

    def test_refusals_change_nothing(self):
        cases = [("no .env", dict(env=False), "The .env file is missing"),
                 ("conflicting role", dict(env_lines=("MACHINE_ROLE=full",)), "not exactly MACHINE_ROLE=feeder"),
                 ("duplicate role", dict(env_lines=("MACHINE_ROLE=feeder", "MACHINE_ROLE=feeder")), "mentions MACHINE_ROLE 2 times"),
                 ("commented role", dict(env_lines=("#MACHINE_ROLE=feeder",)), "not exactly MACHINE_ROLE=feeder"),
                 ("task not found", dict(listed=False), "Could not find a scheduled task"),
                 ("named task missing", dict(env_task="Nope"), "There is no scheduled task called Nope"),
                 ("no .git", dict(git_dir=False), "no .git folder"),
                 ("no origin", dict(remote="none"), "no remote called origin"),
                 ("wrong folder", dict(feeder_file=False), "feeder.py is not in this folder")]
        for form in self.FORMS:
            for label, opts, why in cases:
                with self.subTest(f"{form}: {label}"):
                    self.check_nothing_changed(form, why, **opts)

    def tally_down_run(self, form, press_key):
        """Run with Tally closed, stdin open: the update must stop and wait; nothing may change until a key is pressed."""
        s = self.build(tally_down=True); before = self.env_bytes(s)
        data = self.bat.replace(b"\n", b"\r\n") if form == "crlf" else self.bat
        open(os.path.join(s["shop"], "SHOP_UPDATE.bat"), "wb").write(data)
        proc = subprocess.Popen(["cmd.exe", "/c", os.path.join(s["shop"], "SHOP_UPDATE.bat")], cwd=s["shop"], env=dict(s["env"], SHOP_UPDATE_WAIT_SECONDS="10"),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        chunks = []
        threading.Thread(target=lambda: [chunks.append(c) for c in iter(lambda: proc.stdout.read(1), b"")], daemon=True).start()
        text = lambda: b"".join(chunks).decode("cp437", "replace")
        end = time.time() + 60
        while time.time() < end and "continue anyway" not in text():
            time.sleep(0.2)
        self.assertIn("Tally does not seem to be open. Open Tally with the company loaded, then press any key to continue anyway, or close this window.", text())
        time.sleep(4)                                    # still waiting: nothing may have happened
        self.assertNotIn("Step 3 of 6", text())
        self.assertFalse([d for d in os.listdir(s["shop"]) if d.startswith("SHOP_UPDATE_BACKUP_")])
        self.assertEqual(git(s["shop"], "rev-parse", "HEAD"), s["old"]); self.assertEqual(self.env_bytes(s), before)
        self.assertNotIn("/End", self.calls(s)); self.assertNotIn("/Run", self.calls(s))
        if press_key:
            proc.stdin.write(b"\r\n"); proc.stdin.flush()
            end = time.time() + 120
            while time.time() < end and proc.poll() is None and "FAIL" not in text():
                time.sleep(0.3)
            self.assertIn("Step 3 of 6", text())         # it carried on only after the key
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)   # "close this window"
        return s

    def test_tally_not_open_waits_for_a_key_and_changes_nothing(self):
        for form in self.FORMS:
            with self.subTest(f"{form}: close the window"):
                s = self.tally_down_run(form, press_key=False)
                self.assertEqual(git(s["shop"], "rev-parse", "HEAD"), s["old"])
                self.assertFalse([d for d in os.listdir(s["shop"]) if d.startswith("SHOP_UPDATE_BACKUP_")])
            with self.subTest(f"{form}: press a key"):
                self.tally_down_run(form, press_key=True)

    def test_task_not_found_lists_only_task_names(self):
        for form in self.FORMS:
            with self.subTest(form):
                s = self.build(listed=False); code, out = self.run_bat(s, form)
                self.assertEqual(code, 1); self.assertIn("Could not find a scheduled task", out)
                self.assertIn("Scheduled tasks that mention feeder or tally:", out); self.assertIn("Old Tally Task", out); self.assertIn("Nightly", out)
                self.assertNotIn("OtherTask", out); self.assertIn("Take a photo and send it to Idris.", out)
                for leak in ("SECRETARG", "elsewhere", "feeder.bat", "t.exe"):
                    self.assertNotIn(leak, out, "names only: no paths or arguments")

    def test_git_not_installed(self):
        for form in self.FORMS:
            with self.subTest(form):
                self.check_nothing_changed(form, "Git is not installed", nogit=True)

    def test_a_cut_off_paste_changes_nothing(self):
        for form in self.FORMS:
            for cut in (1, 3, 40):
                with self.subTest(f"{form} cut {cut}"):
                    self.check_nothing_changed(form, "The file looks cut off. Copy it again and paste the whole thing.", code_expected=2, cut=cut)

    def test_file_is_paste_safe(self):
        raw = open(BAT, "rb").read()
        self.assertTrue(all(b < 128 for b in raw), "pure ASCII"); self.assertFalse(raw.startswith(b"\xef\xbb\xbf")); self.assertNotIn(b"\t", raw)
        lines = raw.decode().replace("\r\n", "\n").rstrip("\n").split("\n")
        self.assertLessEqual(len(lines), 150, "keep it short enough to paste")
        self.assertTrue(lines[0].startswith("rem SHOP_UPDATE.bat  version 20")); self.assertEqual(lines[-1], "rem END-OF-SHOP-UPDATE")


if __name__ == "__main__":
    unittest.main()
