"""The app build id: one stable id per deployed code, used by open pages to notice they run old code.

Runs inside check_all's scratch copy, so the real data/ folder is never touched. "Restart" = a brand-new
python process importing app.py (the real thing), not just a second function call."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import support
from tests.support import ROOT
from tests.test_deploy_checks import clean_env, make_copy


def build_id_in_new_process(directory, commit=None):
    env = clean_env(DISABLE_TALLY_SCHEDULING="1", DB_PATH="data/b.db", INITIAL_IMAGE_SCAN="0", LOG_FILE="logs/b.log", INTAKE_SYNC_TOKEN="tok")
    env.pop("RENDER_GIT_COMMIT", None)
    if commit is not None:
        env["RENDER_GIT_COMMIT"] = commit
    result = subprocess.run([sys.executable, "-c", "import app; print('BUILD_ID=' + app.APP_BUILD_ID)"],
                            cwd=directory, env=env, capture_output=True, text=True, timeout=120)
    match = re.search(r"BUILD_ID=(\S*)", result.stdout)
    assert match, result.stderr[-600:]
    return match.group(1)


class BuildIdStability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.dir = make_copy("buildid_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    # ---- Render: the deployed commit
    def test_render_commit_is_the_id_and_is_stable_across_restarts(self):
        commit_a = "a" * 40
        first = build_id_in_new_process(self.dir, commit_a)
        second = build_id_in_new_process(self.dir, commit_a)
        self.assertEqual(first, second, "restarting the same code must not change the id")
        self.assertEqual(first, commit_a[:12])

    def test_a_new_commit_changes_the_id(self):
        self.assertNotEqual(build_id_in_new_process(self.dir, "a" * 40), build_id_in_new_process(self.dir, "b" * 40))

    # ---- local fallback: hash of templates + static
    def test_fallback_is_stable_across_restarts_and_ignores_data(self):
        first = build_id_in_new_process(self.dir)
        os.makedirs(os.path.join(self.dir, "data"), exist_ok=True)
        with open(os.path.join(self.dir, "data", "anything.json"), "w") as handle:
            handle.write('{"stock": 1}')                       # data changes...
        second = build_id_in_new_process(self.dir)
        self.assertEqual(first, second, "...must never change the id (and neither may a restart)")
        self.assertTrue(first.startswith("local-"))

    def test_fallback_changes_when_a_template_or_static_file_changes(self):
        compute = self.app_module.compute_app_build_id
        base = compute({}, self.dir)
        self.assertEqual(base, compute({}, self.dir))
        for relative in ("templates/index.html", "static/data_sync.js"):
            path = os.path.join(self.dir, *relative.split("/"))
            original = open(path, "rb").read()
            try:
                open(path, "wb").write(original + b"\n<!-- changed -->\n")
                self.assertNotEqual(compute({}, self.dir), base, relative)
            finally:
                open(path, "wb").write(original)
        self.assertEqual(compute({}, self.dir), base)

    def test_fallback_ignores_line_endings_and_other_folders(self):
        compute = self.app_module.compute_app_build_id
        base = compute({}, self.dir)
        path = os.path.join(self.dir, "templates", "index.html")
        original = open(path, "rb").read()
        try:
            open(path, "wb").write(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(compute({}, self.dir), base, "a CRLF checkout of the same code is the same build")
        finally:
            open(path, "wb").write(original)
        with open(os.path.join(self.dir, "tests_scratch_note.txt"), "w") as handle:
            handle.write("not part of the page code")
        self.assertEqual(compute({}, self.dir), base)

    def test_commit_wins_over_files_and_blank_commit_falls_back(self):
        compute = self.app_module.compute_app_build_id
        self.assertEqual(compute({"RENDER_GIT_COMMIT": "  abc123def4567890  "}, self.dir), "abc123def456")
        self.assertTrue(compute({"RENDER_GIT_COMMIT": "   "}, self.dir).startswith("local-"))


class BuildIdInResponses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)
        self._saved = self.app_module.APP_BUILD_ID
        self.app_module.APP_BUILD_ID = "build-A"

    def tearDown(self):
        self.app_module.APP_BUILD_ID = self._saved

    def test_last_update_carries_build_id_for_every_role(self):
        for role in ("dealer", "admin_locked", "admin_unlocked"):
            data = support.client_for(role).get("/last_update").get_json()
            self.assertEqual(data["build_id"], "build-A", role)
            self.assertIn("data_version", data)

    def test_build_id_is_the_only_new_field_and_costs_few_bytes(self):
        client = support.client_for("dealer")
        data = client.get("/last_update").get_json()
        self.assertEqual(set(data) - {"build_id"}, {"last_update", "timestamp", "formatted", "data_version"},
                         "no other field was added to /last_update")
        with_id = len(client.get("/last_update").data)
        self.app_module.APP_BUILD_ID = ""
        without = len(support.client_for("dealer").get("/last_update").data)
        self.assertLess(with_id - without, 40, "the id adds a few bytes to a poll that already exists")

    def test_no_new_route(self):
        rules = {rule.rule for rule in self.app_module.app.url_map.iter_rules()}
        self.assertFalse([r for r in rules if "build" in r.lower()], "no new endpoint for the build id")

    def test_id_does_not_depend_on_data(self):
        client = support.client_for("dealer")
        before = client.get("/last_update").get_json()
        changed = {"car_master": ["ALTO"], "main_hierarchy": [{"item_name": "ALTO", "qty": 1}, {"item_name": "ALTO 7D MAT BLACK", "qty": 9}],
                   "item_stock": [{"item_name": "ALTO 7D MAT BLACK", "qty": 9}]}
        self.assertEqual(support.client_for("anonymous").post("/admin/intake/sync_data", json=changed, headers=support.INTAKE_HEADERS).status_code, 200)
        after = client.get("/last_update").get_json()
        self.assertNotEqual(before["data_version"], after["data_version"])
        self.assertEqual(before["build_id"], after["build_id"])

    def test_pages_embed_the_id_the_server_had_when_serving(self):
        html = support.client_for("dealer").get("/").get_data(as_text=True)
        self.assertRegex(html, r'buildId:\s*"build-A"')
        self.assertIn("canAutoReload: isIdleForAutoReload", html)
        for path in ("/train", "/admin/accounts"):
            response = support.client_for("admin_unlocked").get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertRegex(response.get_data(as_text=True), r'buildId:\s*"build-A"', path)


if __name__ == "__main__":
    unittest.main()
