# vixx-watch

Change monitor for **Vixex** — the website(s), news/social coverage, app
stores, and LinkedIn. Runs on a Windows PC via scheduled tasks and publishes a
shareable dashboard to GitHub Pages.

**Live dashboard:** https://harmonious-bublanina-d85bf4.netlify.app (hosted on Netlify; GitHub Pages disabled)
**Build-progress chart:** https://harmonious-bublanina-d85bf4.netlify.app/history.html

## What it tracks
1. **Website changes** on BOTH **vixx.vn** and **vixex.vn** (the latter is not
   live yet — armed to catch it the moment it appears): new/removed pages,
   new/removed links, per-page content edits (Next.js build-id + chunk hashes
   stripped so redeploys aren't false positives), and sitemap.xml appear/change.
2. **News & mentions** of Vixex + backers **FPT / FPT IS / GELEX** across
   Vietnamese + English (Google News), Reddit, and VN crypto sites — each VN
   headline shown with an **English translation**.
3. **App stores** — Apple App Store (iTunes Search API) + Google Play for any
   **VIX-named** crypto/trading app; flags newly-appeared apps.
4. **LinkedIn** — vn.linkedin.com/company/vixex: name, tagline, **follower
   count** (change-tracked).
5. **Wayback snapshots** of every live page (spaced over the day).
6. **History chart** — daily counts of live pages/links/news/apps over time.
7. **Forensic evidence capture** (court-grade) — see below.

## Forensic evidence (court-grade, tamper-evident)
Each crawl seals a complete, tamper-evident record into a **separate PRIVATE
repo** (`Power-Trade/vixx-watch-evidence`, pushed every run — off-machine,
GitHub-timestamped, access-restricted):
- **Verbatim captures** per page/asset: raw response **bytes**, full HTTP
  **headers** (Date/ETag/Last-Modified…), **redirect chain**, HTTP status.
- **TLS certificate** per host (DER + fingerprint + validity dates) — documents
  the site's cert state (vixx.vn's is expired).
- **Full-page rendered screenshots** (headless Chromium) — what a human saw.
- **Provenance** per run: tracker git commit, machine, OS/Python, config, and
  that crawl TLS verification was disabled.
- **Hash-chained manifest** (`manifest.jsonl` + `ledger.txt`): every artifact
  SHA-256'd; each run's entry chains to the previous, so any later edit is
  detectable. Verify: `python vixx_watch.py --verify`.
- **Trusted timestamp**: each run's manifest is RFC-3161 timestamped by a free
  TSA (`.tsr`), independently of the local clock (`ots`/OpenTimestamps is
  attempted first but unavailable on this Windows/OpenSSL-3 setup).
- **Wayback + GitHub** push times provide two further independent timestamp
  witnesses.

*Not legal advice — admissibility varies by jurisdiction; involve counsel and
consider a neutral eDiscovery custodian for high-stakes use.*

## Modules
- `vixx_watch.py` — crawler, forensic capture, diff, news+translation, app
  stores, history, Wayback queue/archiver, orchestration. Modes: *(none)*=daily
  crawl, `--archive`, `--news`, `--apps`, `--verify`.
- `evidence.py` — provenance + hash-chained manifest/ledger + `verify()` +
  RFC-3161/OTS timestamping.
- `screenshot.py` — Playwright/Chromium full-page screenshots.
- `dashboard.py` — renders `docs/index.html` (the console).
- `chart.py` — renders `docs/history.html` (build-progress chart).
- `linkedin.py` — public LinkedIn company-page monitor → `data/linkedin.json`.

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

## News & mentions scanner
Scans for coverage of **Vixex** and its backers **FPT Corporation, FPT IS,
GELEX Group** across Vietnamese + English news (Google News RSS), Reddit, and
named VN crypto outlets. New articles are deduped, logged, and shown on the
dashboard newest-first with source + language.

- Config at the top of `vixx_watch.py`: `NEWS_QUERIES`, `VN_CRYPTO_SITES`,
  `NEWS_LANGS`. Big corporates (FPT/GELEX) are **scoped to a crypto/exchange
  context** so their general business news doesn't flood the feed — to track
  ALL of an entity's news, remove the `_CTX` part of its query.
- Noise guard: drops VIX-index / VIX-Securities (Chứng khoán VIX) unless the
  item also mentions crypto/Vixex.
- Runs every 6h (`VixxWatchNews` task) + once in the daily crawl. Run manually:
  `python vixx_watch.py --news`.
- Data: `data/news.jsonl` (all-time), `data/news_seen.json` (dedup),
  `data/news_latest.json` (feed). **Note:** social platforms beyond Reddit
  (X/Twitter, Facebook, TikTok) need paid APIs / handles and are not scraped.

## Web dashboard (Netlify)
Public URL: **https://harmonious-bublanina-d85bf4.netlify.app** — share with anyone.
(Moved off GitHub Pages to avoid Pages-build Actions billing; rename the site in
the Netlify UI if you want a tidier URL.)

Each daily-crawl / news run regenerates `docs/index.html` and `netlify deploy`s it
(the 2-hourly archive runs don't redeploy). It shows:
- a **banner** (green = no changes / red = changes detected) — the "alert",
- a **Recent changes** feed (new/removed pages, new/removed links, content
  edits, sitemap changes) with clickable links,
- a **Pages** table: every live URL + its latest Wayback snapshot + history,
- the full **tracked-links** list.

Publishing is done by `run_and_publish.ps1` (called by the scheduled tasks),
which runs the monitor then commits+pushes `docs/` only when it changed.

## Notes
- vixx.vn currently serves an **expired TLS cert**; the crawler skips cert
  verification for vixx.vn only (Wayback uses normal verification).
- Wayback Save-Page-Now is anonymous + rate-limited; a slow/timed-out save is
  logged as a warning and the run still succeeds (the capture is usually queued).
- First run establishes the baseline; diffs start the next day.
