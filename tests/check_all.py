#!/usr/bin/env python3
"""ONE command to check that nothing broke:

    python tests/check_all.py            run everything
    python tests/check_all.py --update   re-create the "golden" snapshots (only when a change is intended)
    python tests/check_all.py -k backup  run only test modules whose name contains "backup"

It first copies the code (NOT data/, logs/, .env, .git, .venv) into a scratch folder and runs every test
there, so your real data folder, database, Drive credentials and .env are never read or changed. Real Drive,
real Tally and the real site are never contacted (fakes only). Exit code 0 = all passed.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {".git", "data", "logs", "__pycache__", ".venv", "venv", ".state_snapshot", "node_modules", "brag-output", "ai_tryon_test", "archived_unneeded"}
NEVER_COPY = (".env",)           # any file whose name is .env or starts with .env.

NOT_COVERED = """
What this does NOT cover (be honest about the gaps):
  * a real browser or phone (no JavaScript is executed here; templates/JS are only scanned for the URLs they call)
  * real Google Drive, real Tally, real Render SSH, the installed PWA / service worker in a real browser
  * Linux-only paths (/proc descriptor breakdown) -- the helper is unit-tested with fake data only
  * the real production data size/timing (709 cars, ~15,000 rows): memory behaviour is checked with small data
  * the visual look of any page (snapshots compare structure and text hashes, not pixels)
"""


def copy_code(dest):
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(folder, ROOT)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for name in files:
            if name == ".env" or name.startswith(".env.") or name.endswith((".pyc", ".db", ".log", ".pid")):
                continue
            shutil.copy2(os.path.join(folder, name), os.path.join(dest, rel, name))
    os.makedirs(os.path.join(dest, "data"), exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="rewrite tests/golden/*.json from the current behaviour")
    parser.add_argument("-k", dest="pattern", default="", help="only run test modules whose file name contains this text")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the scratch folder (to inspect it)")
    args = parser.parse_args()

    scratch = tempfile.mkdtemp(prefix="check_all_")
    print(f"Working in a scratch copy: {scratch}\n(your data/, logs/ and .env are not copied)\n", flush=True)
    try:
        copy_code(scratch)
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("MACHINE_ROLE", "BACKUP_MAX_DELETIONS", "IMAGE_SCAN_ROOT", "FEEDER_MODE")
               and not k.startswith(("GDRIVE", "CLOUD_SYNC"))}
        env.update(CHECK_ALL_SCRATCH="1", PYTHONUNBUFFERED="1", PYTHONWARNINGS="ignore::ResourceWarning")
        if args.update:
            env["CHECK_ALL_UPDATE"] = "1"
        modules = sorted(f[:-3] for f in os.listdir(os.path.join(scratch, "tests")) if f.startswith("test_") and f.endswith(".py") and args.pattern in f)
        if not modules:
            print("No test modules matched."); return 2
        command = [sys.executable, "-m", "unittest"] + (["-v"] if args.verbose else []) + [f"tests.{m}" for m in modules]
        # On Windows the runner is started without a console window (so no window opens for it or for the processes the tests
        # start); its output is piped and printed here, because a console-less child has no console to write to.
        proc = subprocess.Popen(command, cwd=scratch, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for raw_line in proc.stdout:
            sys.stdout.write(raw_line.decode("utf-8", "replace"))
            sys.stdout.flush()
        result = argparse.Namespace(returncode=proc.wait())
        if args.update:
            golden_src = os.path.join(scratch, "tests", "golden")
            golden_dst = os.path.join(ROOT, "tests", "golden")
            os.makedirs(golden_dst, exist_ok=True)
            for name in os.listdir(golden_src):
                shutil.copy2(os.path.join(golden_src, name), os.path.join(golden_dst, name))
            print(f"\nGoldens rewritten in {golden_dst}. REVIEW THE DIFF (git diff tests/golden) before committing.")
        print("\n" + ("CHECK_ALL: PASS" if result.returncode == 0 else "CHECK_ALL: FAIL") + f"  ({len(modules)} test modules)")
        print(NOT_COVERED)
        return result.returncode
    finally:
        if args.keep:
            print(f"(scratch kept at {scratch})")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
