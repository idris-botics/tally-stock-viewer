#!/usr/bin/env python3
"""Read the FULL usage history: the archived months (YYYY-MM.jsonl.gz) plus the live user_activity table.

    python tools/activity_archive_read.py --archive-dir <folder with the .jsonl.gz files> --db <mappings.db copy> --summary
    python tools/activity_archive_read.py --archive-dir ... --db ... --csv all_activity.csv

Use a COPY of mappings.db (download last night's backup from Drive) and the archive files downloaded from the
Drive folder "SuperSeatings Activity Archive" (or data/activity_archive/ on the server disk). The database is opened
read-only and only user_activity and users.username are read -- never any access-code column.

In code:   from activity_archive_read import iter_all_rows
Each row is a dict: id, user_id, username, at_utc (naive UTC ISO), action, detail, source ('archive' | 'live').
A row present in both places (e.g. after a dry run) is returned once, from the archive.
"""
import argparse
import csv
import glob
import gzip
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

IST = timedelta(minutes=330)


def iter_archive_rows(archive_dir):
    for path in sorted(glob.glob(os.path.join(archive_dir, "*.jsonl.gz"))):
        with gzip.open(path, "rb") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    row["source"] = "archive"
                    yield row


def iter_live_rows(db_path, skip_ids=()):
    conn = sqlite3.connect("file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro", uri=True)
    try:
        cursor = conn.execute(
            "SELECT a.id, a.user_id, u.username, a.at_utc, a.action, a.detail FROM user_activity a "
            "LEFT JOIN users u ON u.id = a.user_id ORDER BY a.id")
        while True:
            chunk = cursor.fetchmany(2000)
            if not chunk:
                return
            for r in chunk:
                if r[0] in skip_ids:
                    continue
                yield {"id": r[0], "user_id": r[1], "username": r[2], "at_utc": r[3], "action": r[4], "detail": r[5], "source": "live"}
    finally:
        conn.close()


def iter_all_rows(archive_dir=None, db_path=None):
    """Archive first (id order per month), then the live rows not already seen."""
    seen = set()
    if archive_dir:
        for row in iter_archive_rows(archive_dir):
            seen.add(row["id"])
            yield row
    if db_path:
        yield from iter_live_rows(db_path, seen)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive-dir")
    ap.add_argument("--db")
    ap.add_argument("--csv", help="write every row to this CSV file")
    ap.add_argument("--summary", action="store_true", help="print row counts per month and per action")
    args = ap.parse_args(argv)
    if not args.archive_dir and not args.db:
        ap.error("give --archive-dir and/or --db")
    per_month, per_action, total = Counter(), Counter(), 0
    out = open(args.csv, "w", newline="", encoding="utf-8") if args.csv else None
    writer = csv.writer(out) if out else None
    if writer:
        writer.writerow(["id", "user_id", "username", "at_utc", "at_ist", "action", "detail", "source"])
    try:
        for row in iter_all_rows(args.archive_dir, args.db):
            total += 1
            ist = datetime.fromisoformat(row["at_utc"]) + IST
            per_month[ist.strftime("%Y-%m")] += 1
            per_action[row["action"]] += 1
            if writer:
                detail = row["detail"] or ""
                if detail[:1] in ("=", "+", "-", "@"):
                    detail = "'" + detail          # keeps spreadsheets from treating text as a formula
                writer.writerow([row["id"], row["user_id"], row["username"], row["at_utc"], ist.isoformat(timespec="seconds"),
                                 row["action"], detail, row["source"]])
    finally:
        if out:
            out.close()
    if args.summary or not args.csv:
        print(f"{total} rows")
        for month in sorted(per_month):
            print(f"  {month}: {per_month[month]}")
        print("by action:", dict(per_action.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
