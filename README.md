# vixx-watch

Daily change monitor for **vixx.vn** (Vixex). Runs at 00:00 UTC, records what
changed, and snapshots every page to the Wayback Machine.

## What it tracks
1. **New / removed pages** — crawled from `/vi`, `/en` and all internal links.
2. **New / removed links** — every internal + external page link (assets/`_next`
   chunks excluded so deploys don't create noise). Catches footer `#`
   placeholders turning into real URLs.
3. **Content updates** — per-page SHA-256 with Next.js build-id and chunk hashes
   stripped, so a no-op redeploy is *not* flagged; a real text edit is.
4. **Sitemap changes** — `sitemap.xml` is **absent today**; the monitor reports
   if/when it appears, changes, or disappears (with added/removed `<loc>` URLs).

## Files written (under `data/`)
| File | Contents |
|------|----------|
| `changelog.md` | One dated section **per day that something changed** (the main log). |
| `run.log` | One line every run (heartbeat + counts), changed or not. |
| `wayback.log` | Archive result + archived URL per page, per run. |
| `state.json` | Previous-run fingerprint (used for diffing). |
| `snapshots/YYYY-MM-DD/*.html` | Raw HTML of every page that day, for manual diffing. |
| `cron.log` | stdout/stderr from cron. |

## Install (on the Linode)
```bash
# scp the folder up first (run from your Windows PC), then on the server:
cd /path/to/vixx-watch
bash install.sh          # adds CRON_TZ=UTC + 00:00 cron entry, runs one baseline
crontab -l               # verify
```
Requires only `python3` (standard library — no pip).

## Notes
- vixx.vn currently serves an **expired TLS cert**; the crawler skips cert
  verification for vixx.vn only (Wayback uses normal verification).
- Wayback Save-Page-Now is anonymous + rate-limited; a slow/timed-out save is
  logged as a warning and the run still succeeds (the capture is usually queued).
- First run establishes the baseline; diffs start the next day.
