#!/usr/bin/env python3
"""
vixx_watch.py — daily change monitor for vixx.vn (Vixex).

Crawls the site, diffs against the previous run, and records what changed:
  1) new / removed pages
  2) new / removed links (internal + external, incl. placeholder "#" -> real URL)
  3) content updates (per-page hash, with Next.js deploy-noise stripped)
  4) sitemap.xml appearing / changing / disappearing

Then asks the Wayback Machine to snapshot each discovered page.

Stdlib only — no pip install needed. Designed to run under cron at 00:00 UTC.
vixx.vn currently serves an EXPIRED TLS cert, so crawl requests skip cert
verification (Wayback uses normal verification).
"""

import gzip
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

# ---------------------------------------------------------------- config
SITE = "https://vixx.vn"
HOST = "vixx.vn"
SEEDS = [f"{SITE}/", f"{SITE}/vi", f"{SITE}/en"]
MAX_PAGES = 100
CRAWL_DELAY = 1.0          # seconds between page fetches (polite)
FETCH_TIMEOUT = 30
WAYBACK_TIMEOUT = 180      # Save-Page-Now blocks until capture completes
WAYBACK_DELAY = 6.0        # seconds between Save-Page-Now calls (rate limit)
ARCHIVE_BATCH = 1          # pages archived per --archive run (spaced by scheduler)
USER_AGENT = "vixx-watch/1.0 (+site change monitor)"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PENDING_FILE = os.path.join(DATA_DIR, "wayback_pending.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")          # GitHub Pages source
DASHBOARD = os.path.join(DOCS_DIR, "index.html")
WB_LATEST = "https://web.archive.org/web/29991231235959/"  # redirects to newest capture
WB_HISTORY = "https://web.archive.org/web/*/"               # capture calendar
CHANGELOG = os.path.join(DATA_DIR, "changelog.md")
RUN_LOG = os.path.join(DATA_DIR, "run.log")
WAYBACK_LOG = os.path.join(DATA_DIR, "wayback.log")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")

# Don't crawl these (assets / framework internals); still recorded as links.
SKIP_CRAWL_RE = re.compile(
    r"(/_next/|/images/|/logos/|/icons/|/fonts/|/favicon)"
    r"|\.(png|jpe?g|gif|svg|ico|css|js|woff2?|ttf|webp|mp4|pdf)(\?|$)",
    re.I,
)
HREF_RE = re.compile(r'href="([^"]+)"')

# Volatile per-deploy fingerprints to strip before hashing page content,
# so a no-op redeploy doesn't look like a content change.
NOISE_RES = [
    re.compile(r'"b":"[^"]+"'),                       # Next.js build id
    re.compile(r'/_next/static/[^"\\)\s]+'),          # chunk/css filenames (content-hashed)
    re.compile(r'\?v=[0-9a-f]+', re.I),               # asset cache-busters
    re.compile(r'"buildId":"[^"]+"'),
    re.compile(r'nonce="[^"]*"'),
]


# ---------------------------------------------------------------- helpers
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_dirs():
    for d in (DATA_DIR, SNAP_DIR):
        os.makedirs(d, exist_ok=True)


_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE  # vixx.vn cert is expired


def fetch(url, verify=False, timeout=FETCH_TIMEOUT):
    """Return (final_url, status, text). status 0 on transport error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, identity"},
    )
    ctx = _ctx if not verify else ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            text = raw.decode("utf-8", "replace")
            return r.geturl(), r.status, text
    except urllib.error.HTTPError as e:
        return url, e.code, ""
    except Exception as e:  # noqa: BLE001  (transport/timeout/ssl)
        return url, 0, f"__ERROR__ {e}"


def decode_payload(text):
    """Unescape Next.js RSC string blobs so hrefs/text are readable."""
    return (
        text.replace('\\"', '"')
        .replace("\\u0026", "&")
        .replace("\\/", "/")
    )


def normalize(url, base):
    """Absolute, fragment-stripped, trailing-slash-normalized. None if not a link."""
    url = url.strip()
    if not url or url.startswith(("mailto:", "tel:", "javascript:")) or url == "#":
        return None
    url = urllib.parse.urljoin(base, url)
    url, _frag = urllib.parse.urldefrag(url)
    if url.endswith("/") and url.rstrip("/") != f"{SITE}":
        url = url.rstrip("/")
    return url or None


def is_internal(url):
    try:
        return urllib.parse.urlparse(url).netloc in (HOST, f"www.{HOST}")
    except ValueError:
        return False


def clean_for_hash(text):
    for rx in NOISE_RES:
        text = rx.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def sha(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def snap_name(url):
    return re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")[:150] + ".html"


# ---------------------------------------------------------------- crawl
def crawl():
    """Return (pages, all_links). pages: {url: {hash, status, len}}."""
    pages = {}
    all_links = set()
    seen = set()
    queue = deque(SEEDS)
    snap_day = os.path.join(SNAP_DIR, today())
    os.makedirs(snap_day, exist_ok=True)

    while queue and len(pages) < MAX_PAGES:
        url = queue.popleft()
        norm = normalize(url, SITE)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if not is_internal(norm) or SKIP_CRAWL_RE.search(norm):
            if not SKIP_CRAWL_RE.search(norm):
                all_links.add(norm)  # external page link, worth tracking
            continue

        final, status, text = fetch(norm)
        final = normalize(final, SITE) or norm
        if final in pages:
            continue
        if text.startswith("__ERROR__") or status == 0:
            pages[final] = {"hash": "", "status": 0, "len": 0, "note": text[:200]}
            continue

        decoded = decode_payload(text)
        # save raw snapshot
        try:
            with open(os.path.join(snap_day, snap_name(final)), "w",
                      encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

        pages[final] = {
            "hash": sha(clean_for_hash(text)),
            "status": status,
            "len": len(text),
        }

        # discover links from anchors AND RSC payload
        for raw_href in HREF_RE.findall(decoded):
            link = normalize(raw_href, final)
            if not link:
                continue
            if not SKIP_CRAWL_RE.search(link):
                all_links.add(link)  # track page links, not rotating assets
            if (
                is_internal(link)
                and not SKIP_CRAWL_RE.search(link)
                and link not in seen
                and status == 200
            ):
                queue.append(link)

        time.sleep(CRAWL_DELAY)

    return pages, sorted(all_links)


def check_sitemap():
    final, status, text = fetch(f"{SITE}/sitemap.xml")
    # Next.js serves a 200-looking SPA error page for unknown routes; treat
    # only real XML as a present sitemap.
    present = status == 200 and "<urlset" in text.lower()
    h = sha(clean_for_hash(text)) if present else ""
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text) if present else []
    return {"present": present, "status": status, "hash": h, "urls": sorted(set(urls))}


# ---------------------------------------------------------------- diff
def diff_state(old, new):
    """Return a dict of change lists (empty lists when nothing changed)."""
    op, np_ = old.get("pages", {}), new["pages"]
    ol = set(old.get("links", []))
    nl = set(new["links"])
    osm = old.get("sitemap", {})
    nsm = new["sitemap"]

    changed = []
    for u in sorted(set(op) & set(np_)):
        if op[u].get("hash") and np_[u].get("hash") and op[u]["hash"] != np_[u]["hash"]:
            changed.append(u)

    sm_notes = []
    if osm:
        if nsm["present"] and not osm.get("present"):
            sm_notes.append("sitemap.xml NOW PRESENT (was absent)")
        elif not nsm["present"] and osm.get("present"):
            sm_notes.append("sitemap.xml DISAPPEARED (was present)")
        elif nsm["present"] and osm.get("present") and nsm["hash"] != osm.get("hash"):
            added = set(nsm["urls"]) - set(osm.get("urls", []))
            removed = set(osm.get("urls", [])) - set(nsm["urls"])
            sm_notes.append(
                f"sitemap.xml CHANGED (+{len(added)} / -{len(removed)} URLs)"
            )
            sm_notes += [f"  + {u}" for u in sorted(added)]
            sm_notes += [f"  - {u}" for u in sorted(removed)]

    return {
        "new_pages": sorted(set(np_) - set(op)),
        "removed_pages": sorted(set(op) - set(np_)),
        "content_changed": changed,
        "new_links": sorted(nl - ol),
        "removed_links": sorted(ol - nl),
        "sitemap": sm_notes,
        "first_run": not op and not osm,
    }


def has_changes(d):
    return any(
        d[k]
        for k in (
            "new_pages",
            "removed_pages",
            "content_changed",
            "new_links",
            "removed_links",
            "sitemap",
        )
    )


def write_changelog(d, new):
    lines = [f"\n## {now_iso()}"]
    if d["first_run"]:
        lines.append(
            f"_Baseline established: {len(new['pages'])} pages, "
            f"{len(new['links'])} links, sitemap "
            f"{'present' if new['sitemap']['present'] else 'absent'}._"
        )
    sections = [
        ("New pages", d["new_pages"]),
        ("Removed pages", d["removed_pages"]),
        ("Content changed", d["content_changed"]),
        ("New links", d["new_links"]),
        ("Removed links", d["removed_links"]),
    ]
    for title, items in sections:
        if items:
            lines.append(f"\n### {title} ({len(items)})")
            lines += [f"- {u}" for u in items]
    if d["sitemap"]:
        lines.append("\n### Sitemap")
        lines += [f"- {n}" for n in d["sitemap"]]
    with open(CHANGELOG, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- wayback
def wayback_one(url, attempts=1):
    """Try to archive one URL. Returns (status, archived).

    attempts=1 (default) does a single gentle try — the scheduler spaces calls
    over the day, so failures are simply retried on the next fire.
    """
    save_url = "https://web.archive.org/save/" + url
    backoffs = [0, 30, 60][:max(1, attempts)]  # only back off if multiple attempts
    last = ("ERR none", "")
    for i, wait in enumerate(backoffs):
        if wait:
            time.sleep(wait)
        req = urllib.request.Request(save_url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=WAYBACK_TIMEOUT) as r:
                cl = r.headers.get("Content-Location")
                archived = "https://web.archive.org" + cl if cl else r.geturl()
                return ("OK", archived)
        except urllib.error.HTTPError as e:
            last = (f"HTTP {e.code}", "")
            if e.code not in (429, 502, 503, 520, 523, 525):
                return last  # not a throttle -> don't retry
        except Exception as e:  # noqa: BLE001  (timeouts: capture often still queued)
            last = (f"ERR {e}", "")
    return last


def load_pending():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_pending(q):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(q, f, indent=2, ensure_ascii=False)


def reset_pending(pages):
    """Queue today's pages for archiving. Preserves any already done today."""
    day = today()
    q = {"date": day, "pages": {u: {"status": "pending", "archived": ""} for u in sorted(pages)}}
    old = load_pending()
    if old.get("date") == day:
        for u, info in old.get("pages", {}).items():
            if u in q["pages"] and info.get("status") == "OK":
                q["pages"][u] = info  # keep already-archived
    save_pending(q)
    return q


def archive_step(batch=ARCHIVE_BATCH):
    """Archive up to `batch` still-pending pages for today; log results."""
    q = load_pending()
    if q.get("date") != today():
        msg = f"{now_iso()} archive: no queue for today (crawl runs at 09:00)"
        print(msg)
        return msg
    pending = [u for u, i in q["pages"].items() if i.get("status") != "OK"]
    done = []
    for u in pending[:batch]:
        st, arch = wayback_one(u, attempts=1)
        q["pages"][u] = {"status": "OK" if st == "OK" else st, "archived": arch}
        done.append((u, st, arch))
        time.sleep(WAYBACK_DELAY)
    save_pending(q)
    if done:
        with open(WAYBACK_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n# {now_iso()} (archive step)\n")
            for u, st, arch in done:
                f.write(f"{st}\t{u}\t{arch}\n")
    remaining = sum(1 for i in q["pages"].values() if i.get("status") != "OK")
    ok = sum(1 for i in q["pages"].values() if i.get("status") == "OK")
    msg = (
        f"{now_iso()} archive: did {len(done)} "
        f"({', '.join(st for _, st, _ in done) or '-'}); "
        f"{ok}/{len(q['pages'])} archived today, {remaining} pending"
    )
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    build_dashboard()  # refresh Wayback links/status on the page
    print(msg)
    return msg


# ---------------------------------------------------------------- dashboard
def _changelog_entries(limit=40):
    """Parse changelog.md into [(timestamp, body_lines), ...] newest first."""
    if not os.path.exists(CHANGELOG):
        return []
    with open(CHANGELOG, encoding="utf-8") as f:
        text = f.read()
    entries = []
    for block in text.split("\n## ")[1:]:
        lines = block.strip("\n").split("\n")
        ts = lines[0].strip()
        entries.append((ts, lines[1:]))
    entries.reverse()
    return entries[:limit]


def _render_change_body(lines):
    out, in_list = [], False
    for ln in lines:
        ln = ln.rstrip()
        if not ln:
            continue
        if ln.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f'<div class="ch-sec">{html.escape(ln[4:])}</div>')
        elif ln.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = ln[2:].strip()
            if item.startswith("http"):
                out.append(
                    f'<li><a href="{html.escape(item)}" target="_blank" '
                    f'rel="noopener">{html.escape(item)}</a></li>'
                )
            else:
                out.append(f"<li>{html.escape(item)}</li>")
        elif ln.startswith("_") and ln.endswith("_"):
            out.append(f'<div class="ch-note">{html.escape(ln.strip("_"))}</div>')
        else:
            out.append(f"<div>{html.escape(ln)}</div>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def build_dashboard():
    """Render docs/index.html from current state — self-contained, colleague-shareable."""
    os.makedirs(DOCS_DIR, exist_ok=True)
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
    pending = load_pending()
    wb = pending.get("pages", {}) if pending.get("date") == today() else {}

    pages = sorted(state.get("pages", {}))
    links = state.get("links", [])
    sm = state.get("sitemap", {})
    updated = state.get("crawled_at", "—")

    entries = _changelog_entries()
    # Has a real (non-baseline) change happened recently?
    recent_change = ""
    for ts, body in entries:
        if not any("Baseline established" in l for l in body):
            recent_change = ts
            break
    if recent_change:
        banner = (
            f'<div class="banner alert">&#9888; Changes detected &mdash; '
            f'most recent: <b>{html.escape(recent_change)}</b>. See the feed below.</div>'
        )
    else:
        banner = '<div class="banner ok">&#10003; No structural changes recorded yet.</div>'

    # pages table
    rows = []
    for u in pages:
        info = wb.get(u, {})
        st = info.get("status", "—")
        arch = info.get("archived", "")
        snap = arch if (st == "OK" and arch) else WB_LATEST + u
        badge = "ok" if st == "OK" else ("pend" if st in ("—", "pending") else "warn")
        rows.append(
            f"<tr><td><a href='{html.escape(u)}' target='_blank' rel='noopener'>"
            f"{html.escape(u)}</a></td>"
            f"<td><a href='{html.escape(snap)}' target='_blank' rel='noopener'>snapshot</a> "
            f"&middot; <a href='{html.escape(WB_HISTORY + u)}' target='_blank' "
            f"rel='noopener'>history</a></td>"
            f"<td><span class='b {badge}'>{html.escape(str(st))}</span></td></tr>"
        )

    link_items = "\n".join(
        f"<li><a href='{html.escape(l)}' target='_blank' rel='noopener'>{html.escape(l)}</a></li>"
        for l in links
    )

    feed = []
    for ts, body in entries:
        feed.append(
            f'<div class="entry"><div class="ts">{html.escape(ts)}</div>'
            f"{_render_change_body(body)}</div>"
        )
    feed_html = "\n".join(feed) or "<p class='muted'>No changes recorded yet.</p>"

    sm_txt = "present" if sm.get("present") else "absent"
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vixx.vn change monitor</title>
<style>
:root{{--bg:#0f1115;--card:#1a1d24;--mut:#8b93a7;--fg:#e6e9ef;--ac:#5b9dff;--ok:#2ea043;--warn:#d29922;--alert:#f85149}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,Arial}}
a{{color:var(--ac);text-decoration:none}}a:hover{{text-decoration:underline}}
.wrap{{max-width:1000px;margin:0 auto;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px;margin-bottom:18px}}
.banner{{padding:12px 16px;border-radius:8px;margin:14px 0;font-weight:600}}
.banner.alert{{background:rgba(248,81,73,.15);border:1px solid var(--alert);color:#ffb4ae}}
.banner.ok{{background:rgba(46,160,67,.12);border:1px solid var(--ok);color:#85e89d}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 20px}}
.stat{{background:var(--card);border-radius:8px;padding:10px 14px;min-width:90px}}
.stat .n{{font-size:20px;font-weight:700}}.stat .l{{color:var(--mut);font-size:12px}}
.card{{background:var(--card);border-radius:10px;padding:16px 18px;margin:16px 0}}
h2{{font-size:16px;margin:0 0 12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td,th{{text-align:left;padding:7px 8px;border-bottom:1px solid #262a33;vertical-align:top}}
th{{color:var(--mut);font-weight:600}}
.b{{font-size:11px;padding:2px 7px;border-radius:10px}}
.b.ok{{background:rgba(46,160,67,.2);color:#85e89d}}.b.pend{{background:#2a2f3a;color:var(--mut)}}
.b.warn{{background:rgba(210,153,34,.2);color:#e3b341}}
.entry{{border-left:3px solid var(--ac);padding:6px 0 6px 14px;margin:14px 0}}
.entry .ts{{color:var(--mut);font-size:12px;margin-bottom:4px}}
.ch-sec{{font-weight:600;margin:8px 0 2px}}.ch-note{{color:var(--mut);font-style:italic}}
.entry ul{{margin:4px 0 4px 18px;padding:0}}.entry li{{word-break:break-all}}
ul.links{{columns:2;font-size:13px;list-style:none;padding:0}}ul.links li{{margin:3px 0;word-break:break-all}}
.muted{{color:var(--mut)}}footer{{color:var(--mut);font-size:12px;margin-top:24px}}
@media(max-width:640px){{ul.links{{columns:1}}}}
</style></head><body><div class="wrap">
<h1>vixx.vn &mdash; website change monitor</h1>
<div class="sub">Last crawl: <b>{html.escape(updated)}</b> (UTC) &middot; auto-updates when the monitor runs</div>
{banner}
<div class="stats">
<div class="stat"><div class="n">{len(pages)}</div><div class="l">pages</div></div>
<div class="stat"><div class="n">{len(links)}</div><div class="l">links</div></div>
<div class="stat"><div class="n">{sm_txt}</div><div class="l">sitemap.xml</div></div>
</div>

<div class="card"><h2>&#9888; Recent changes</h2>{feed_html}</div>

<div class="card"><h2>Pages ({len(pages)})</h2>
<table><tr><th>Live URL</th><th>Wayback</th><th>Today</th></tr>
{''.join(rows)}
</table></div>

<div class="card"><h2>All tracked links ({len(links)})</h2>
<ul class="links">{link_items}</ul></div>

<footer>Generated by vixx-watch. Snapshot = latest Wayback capture; history = all captures.</footer>
</div></body></html>"""
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(doc)


# ---------------------------------------------------------------- main
def main():
    ensure_dirs()
    start = now_iso()

    old = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}

    pages, links = crawl()
    sitemap = check_sitemap()
    new = {"pages": pages, "links": links, "sitemap": sitemap, "crawled_at": start}

    d = diff_state(old, new)
    changed = has_changes(d)
    if changed or d["first_run"]:
        write_changelog(d, new)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)

    # Queue today's pages for the spaced-out archiver (does NOT archive here).
    reset_pending(pages)
    build_dashboard()

    summary = (
        f"{now_iso()} pages={len(pages)} links={len(links)} "
        f"sitemap={'Y' if sitemap['present'] else 'N'} "
        f"changed={'YES' if changed else 'no'} "
        f"(+{len(d['new_pages'])}p/{len(d['content_changed'])}c/"
        f"{len(d['new_links'])}l) queued {len(pages)} for archive"
    )
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(summary + "\n")
    print(summary)


if __name__ == "__main__":
    ensure_dirs()
    if "--archive" in sys.argv:
        archive_step()
    else:
        main()
