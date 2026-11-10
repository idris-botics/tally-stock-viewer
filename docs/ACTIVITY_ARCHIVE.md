# Usage activity: tiers, summaries and the Drive archive

Dealer/admin actions are recorded in the `user_activity` table (who, when, what). It is kept in three tiers so the
live database and the nightly backup stay small while the Usage Charts can still look back as far as you like.

| Tier | Where | How long | Used by |
|---|---|---|---|
| Raw rows | `user_activity` | `ACTIVITY_RETENTION_DAYS` (default **90**) | charts for 1 - 90 days, with full detail |
| Summaries | `activity_daily`, `activity_hourly`, `activity_top_monthly` (+ `activity_summary_done` markers) | forever (about 1 MB a year) | charts for 6 months / 1 year / All time |
| Archive | Google Drive folder **SuperSeatings Activity Archive** (and `data/activity_archive/` on the disk) | forever | analysis later (`tools/activity_archive_read.py`) |

* A day is summarised once it is over (IST). Nothing is ever recomputed once summarised, and a raw row is never
  removed before its day (and its month) has a summary.
* Once a whole IST month is older than the raw window, its rows are written to `YYYY-MM.jsonl.gz`, uploaded to the
  archive folder (a **separate** folder in the Drive root, never inside the backup folder), checked (size + md5
  re-read from Drive, row count + checksum), and only then deleted from the table, in one transaction.
  Any failed step deletes nothing, logs one `ERROR` with the exception class name, and is retried the next night.
* It runs only after a successful scheduled backup, inside the same lock, so it never overlaps a backup.
* `ACTIVITY_ARCHIVE_DRY_RUN=1` uploads and verifies but never deletes.
* The first real archive can only happen once October is wholly older than the raw window: about **30 January 2027**
  with the 90-day window.

What to look at the first time it really runs (cloud logs, around 02:00 IST on the first night after that date):
`Activity archive: 2026-10 uploaded (N rows in the file, verified on Drive)` and
`Activity archive run: months archived=1, rows pruned=N`. In Drive: the folder exists in "My Drive" with
`2026-10.jsonl.gz`; the backup folder is unchanged and the backup still reports `success`.
If you see `Activity archive failed: <ClassName>`, nothing was deleted; the rows are safe in the table.
