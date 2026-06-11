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
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------- config
# Monitored domains. vixex.vn is not live yet (no DNS) — armed so the monitor
# catches its pages/links the moment it goes online.
SITES = ["https://vixx.vn", "https://vixex.vn"]
HOSTS = {"vixx.vn", "www.vixx.vn", "vixex.vn", "www.vixex.vn"}
SITE = SITES[0]            # base used by normalize() for relative seeds
SEEDS = [s + p for s in SITES for p in ("/", "/vi", "/en")]
MAX_PAGES = 150
CRAWL_DELAY = 1.0          # seconds between page fetches (polite)
FETCH_TIMEOUT = 30
WAYBACK_TIMEOUT = 180      # Save-Page-Now blocks until capture completes
WAYBACK_DELAY = 6.0        # seconds between Save-Page-Now calls (rate limit)
ARCHIVE_BATCH = 1          # pages archived per --archive run (spaced by scheduler)

# ---- news / mentions monitoring ----
NEWS_KEEP = 80             # articles kept in the dashboard feed
# Vietnamese crypto outlets spotlighted via site:-scoped queries (edit freely).
VN_CRYPTO_SITES = ["coin68.com", "tapchibitcoin.io", "coinnews.vn", "blogtienso.net"]
# Crypto/exchange context used to scope the big corporates so their general
# news doesn't flood the feed. To track ALL of an entity's news, drop the ctx.
_CTX = ('(Vixex OR crypto OR "tài sản mã hóa" OR "tài sản số" OR '
        '"sàn giao dịch tài sản" OR "tiền số" OR blockchain OR "tài sản số")')
NEWS_QUERIES = [
    {"label": "Vixex", "q": '"Vixex" OR "vixx.vn" OR "VIX Crypto Assets Exchange"'},
    {"label": "FPT×crypto", "q": '"FPT" ' + _CTX + ' (Vixex OR "VIX Crypto" OR "sàn giao dịch tài sản mã hóa")'},
    {"label": "FPT IS×crypto", "q": '("FPT IS" OR "FPT Information System") ' + _CTX},
    {"label": "GELEX×crypto", "q": '"GELEX" ' + _CTX},
]
NEWS_LANGS = [
    {"code": "vi", "params": "hl=vi&gl=VN&ceid=VN:vi"},
    {"code": "en", "params": "hl=en-US&gl=US&ceid=US:en"},
]
# Drop obvious VIX-index / VIX-Securities noise unless crypto/Vixex is present.
NEWS_EXCLUDE = re.compile(
    r"(VN-?Index|chỉ số VIX|VIX Securities|Chứng khoán VIX|volatility index)", re.I)
NEWS_KEEP_IF = re.compile(
    r"(vixex|vixx\.vn|crypto|mã hóa|tài sản số|blockchain|tiền số)", re.I)

# ---- app-store watch (Apple iTunes Search API + Google Play scrape) ----
APPS_KEEP = 60
APP_TERMS = ["Vixex", "VIX exchange", "VIX crypto", "VIX trading", "VIX wallet"]
PLAY_DETAIL_CAP = 25       # max Google Play app-detail fetches per run (bounds cost)
# Precision filters: exact brand "vixex" anywhere is always kept; a standalone
# "VIX" token is kept only with a crypto/trading context; securities/index never.
APP_BRAND_RE = re.compile(r"vixex", re.I)
APP_VIXTOKEN_RE = re.compile(r"\bvix\b", re.I)
APP_CONTEXT_RE = re.compile(
    r"(crypto|exchange|trading|wallet|coin|blockchain|defi|web3|"
    r"tài sản|tiền số|sàn|giao dịch)", re.I)
APP_EXCLUDE_RE = re.compile(
    r"(securit|chứng khoán|fear\s*&?\s*greed|volatilit|vn-?index|\bindex\b)", re.I)


def _app_relevant(name, genre="", extra=""):
    """True only for a VIXEX-brand app or a VIX-token crypto/trading app."""
    blob = f"{name} {genre} {extra}"
    if APP_EXCLUDE_RE.search(blob):
        return False                       # VIX Securities / VIX index, etc.
    if APP_BRAND_RE.search(blob):
        return True                        # exact "vixex" brand (name or package)
    if APP_VIXTOKEN_RE.search(name) and (
        (genre or "").lower() in ("finance", "business")
        or APP_CONTEXT_RE.search(blob)):
        return True
    return False

USER_AGENT = "vixx-watch/1.0 (+site change monitor)"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PENDING_FILE = os.path.join(DATA_DIR, "wayback_pending.json")
NEWS_SEEN = os.path.join(DATA_DIR, "news_seen.json")
NEWS_LOG = os.path.join(DATA_DIR, "news.jsonl")
NEWS_LATEST = os.path.join(DATA_DIR, "news_latest.json")
NEWS_TR = os.path.join(DATA_DIR, "news_tr.json")   # id -> English title cache
TRANSLATE_PER_RUN = 60     # cap VN->EN translations per run (backfills over time)
APPS_SEEN = os.path.join(DATA_DIR, "apps_seen.json")
APPS_LOG = os.path.join(DATA_DIR, "apps.jsonl")
APPS_LATEST = os.path.join(DATA_DIR, "apps_latest.json")
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
    if url.endswith("/"):
        p = urllib.parse.urlparse(url)
        if p.path not in ("", "/"):       # keep bare host roots, trim others
            url = url.rstrip("/")
    return url or None


def is_internal(url):
    try:
        return urllib.parse.urlparse(url).netloc in HOSTS
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
        if status != 200 or text.startswith("__ERROR__"):
            continue  # unreachable/404 (e.g. vixex.vn not live yet) -> not a live page

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
    """Check sitemap.xml on every monitored domain; combine into one record."""
    per, all_urls, hashes, any_present = {}, [], [], False
    for site in SITES:
        _, status, text = fetch(f"{site}/sitemap.xml")
        # Next.js serves a 200-looking SPA error page for unknown routes; treat
        # only real XML as a present sitemap.
        present = status == 200 and "<urlset" in text.lower()
        urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text) if present else []
        per[site] = {"present": present, "status": status,
                     "hash": sha(clean_for_hash(text)) if present else "",
                     "urls": sorted(set(urls))}
        any_present = any_present or present
        all_urls += urls
        hashes.append(site + ":" + per[site]["hash"])
    return {"present": any_present, "status": 0, "hash": sha("|".join(hashes)),
            "urls": sorted(set(all_urls)), "per_site": per}


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


# ---------------------------------------------------------------- news
def parse_feed(xml_text, lang, label, source_hint):
    """Parse a Google News (RSS2) or Reddit (Atom) feed into article dicts."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):  # RSS 2.0 (Google News)
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        s = it.find("source")
        src = (s.text or "").strip() if s is not None and s.text else source_hint
        items.append({
            "title": title, "link": link, "id": (guid or link).strip(),
            "published": (it.findtext("pubDate") or "").strip(),
            "source": src, "lang": lang, "query": label,
        })
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(ns + "entry"):  # Atom (Reddit)
        title = (e.findtext(ns + "title") or "").strip()
        if not title:
            continue
        le = e.find(ns + "link")
        link = le.get("href") if le is not None else ""
        gid = (e.findtext(ns + "id") or "").strip()
        items.append({
            "title": title, "link": link, "id": (gid or link).strip(),
            "published": (e.findtext(ns + "updated") or "").strip(),
            "source": source_hint, "lang": lang, "query": label,
        })
    return items


def _pub_ts(item):
    p = item.get("published", "")
    try:
        return parsedate_to_datetime(p).timestamp()
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(p.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0


def _gnews_url(query, params):
    return ("https://news.google.com/rss/search?q="
            + urllib.parse.quote(query) + "&" + params)


def translate_vi_en(text):
    """Best-effort VN->EN via Google's public translate endpoint. '' on failure."""
    if not text.strip():
        return ""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           "&sl=vi&tl=en&dt=t&q=" + urllib.parse.quote(text))
    _, status, body = fetch(url, verify=True, timeout=15)
    if status != 200 or body.startswith("__ERROR__"):
        return ""
    try:
        data = json.loads(body)
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        return out if out.lower() != text.strip().lower() else ""
    except (ValueError, IndexError, TypeError):
        return ""


def fetch_news():
    """Query news/forum feeds for Vixex + backers; record new mentions."""
    seen = {}
    if os.path.exists(NEWS_SEEN):
        try:
            with open(NEWS_SEEN, encoding="utf-8") as f:
                seen = json.load(f)
        except (OSError, ValueError):
            seen = {}

    collected = []
    for q in NEWS_QUERIES:                      # Google News, VN + EN
        for lang in NEWS_LANGS:
            _, status, text = fetch(_gnews_url(q["q"], lang["params"]), verify=True)
            if status == 200 and not text.startswith("__ERROR__"):
                collected += parse_feed(text, lang["code"], q["label"], "Google News")
            time.sleep(1.0)
    for site in VN_CRYPTO_SITES:                # site-scoped VN crypto outlets
        q = f'"Vixex" OR "VIX Crypto Assets Exchange" site:{site}'
        _, status, text = fetch(_gnews_url(q, "hl=vi&gl=VN&ceid=VN:vi"), verify=True)
        if status == 200 and not text.startswith("__ERROR__"):
            collected += parse_feed(text, "vi", f"site:{site}", site)
        time.sleep(1.0)
    for term in ['"Vixex"', "vixx.vn"]:         # Reddit / forums
        url = "https://www.reddit.com/search.rss?sort=new&q=" + urllib.parse.quote(term)
        _, status, text = fetch(url, verify=True)
        if status == 200 and not text.startswith("__ERROR__"):
            collected += parse_feed(text, "en", "Reddit", "Reddit")
        time.sleep(1.0)

    new = []
    for it in collected:
        if not it["id"]:
            continue
        t = it["title"]
        if NEWS_EXCLUDE.search(t) and not NEWS_KEEP_IF.search(t):
            continue  # VIX-index / VIX-Securities noise
        if it["query"] == "Reddit" and not re.search(r"vix", t, re.I):
            continue  # Reddit fuzzy-matches obscure terms; require a vix mention
        if it["id"] in seen:
            continue
        seen[it["id"]] = now_iso()
        it["first_seen"] = seen[it["id"]]
        new.append(it)

    if new:
        with open(NEWS_LOG, "a", encoding="utf-8") as f:
            for it in new:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    with open(NEWS_SEEN, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False)

    latest = []
    if os.path.exists(NEWS_LOG):
        with open(NEWS_LOG, encoding="utf-8") as f:
            for ln in f.read().splitlines()[-400:]:
                try:
                    latest.append(json.loads(ln))
                except ValueError:
                    pass
    latest.sort(key=_pub_ts, reverse=True)
    latest = latest[:NEWS_KEEP]

    # Attach English titles to Vietnamese items (cached; backfills over runs).
    cache = {}
    if os.path.exists(NEWS_TR):
        try:
            with open(NEWS_TR, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    budget = TRANSLATE_PER_RUN
    for a in latest:
        if a.get("lang") != "vi" or not a.get("title"):
            continue
        tid = a.get("id") or a.get("link") or a["title"]
        if tid in cache:
            a["title_en"] = cache[tid]
        elif budget > 0:
            en = translate_vi_en(a["title"])
            cache[tid] = en
            a["title_en"] = en
            budget -= 1
            time.sleep(0.3)
    with open(NEWS_TR, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    with open(NEWS_LATEST, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    msg = f"{now_iso()} news: +{len(new)} new (scanned {len(collected)})"
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)
    return new


# ---------------------------------------------------------------- app stores
def _apple_search(term, country):
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "country": country, "entity": "software", "limit": 25})
    _, status, text = fetch(url, verify=True)
    out = []
    if status == 200 and not text.startswith("__ERROR__"):
        try:
            data = json.loads(text)
        except ValueError:
            return out
        for r in data.get("results", []):
            name = r.get("trackName", "") or ""
            desc = r.get("description", "") or ""
            if not _app_relevant(name, r.get("primaryGenreName", "") or "", desc[:400]):
                continue
            out.append({
                "store": "iOS", "id": "ios:" + str(r.get("bundleId") or r.get("trackId")),
                "name": name, "developer": r.get("sellerName", "") or "",
                "genre": r.get("primaryGenreName", "") or "",
                "url": r.get("trackViewUrl", "") or "",
                "updated": (r.get("currentVersionReleaseDate") or "")[:10],
                "country": country, "term": term,
            })
    return out


def _play_search_pkgs(term, gl, hl):
    url = "https://play.google.com/store/search?" + urllib.parse.urlencode(
        {"q": term, "c": "apps", "gl": gl, "hl": hl})
    _, status, text = fetch(url, verify=True)
    pkgs, seen = [], set()
    if status == 200 and not text.startswith("__ERROR__"):
        for m in re.finditer(r"/store/apps/details\?id=([A-Za-z0-9_.]+)", text):
            p = m.group(1)
            if p not in seen:
                seen.add(p)
                pkgs.append(p)
    return pkgs[:10]


def _play_detail(pkg):
    url = "https://play.google.com/store/apps/details?" + urllib.parse.urlencode(
        {"id": pkg, "hl": "en"})
    _, status, text = fetch(url, verify=True)
    name, dev, desc = "", "", ""
    if status == 200 and not text.startswith("__ERROR__"):
        m = re.search(r'<meta property="og:title" content="([^"]+)"', text)
        if m:
            name = html.unescape(m.group(1)).replace(" - Apps on Google Play", "").strip()
        m = re.search(r'<meta property="og:description" content="([^"]+)"', text)
        if m:
            desc = html.unescape(m.group(1)).strip()
        m = re.search(r'href="/store/apps/dev(?:eloper)?\?id=[^"]*"[^>]*>([^<]+)<', text)
        if m:
            dev = html.unescape(m.group(1)).strip()
    return name, dev, desc, url


def fetch_apps():
    """Scan Apple App Store + Google Play for VIX-named crypto/trading apps."""
    seen = {}
    if os.path.exists(APPS_SEEN):
        try:
            with open(APPS_SEEN, encoding="utf-8") as f:
                seen = json.load(f)
        except (OSError, ValueError):
            seen = {}

    found = []
    for term in APP_TERMS:                      # Apple iTunes Search API (official)
        for c in ("us", "vn"):
            found += _apple_search(term, c)
            time.sleep(0.5)

    pkgs = {}                                   # Google Play (scrape search)
    for term in APP_TERMS:
        for gl, hl in (("US", "en"), ("VN", "vi")):
            for p in _play_search_pkgs(term, gl, hl):
                pkgs.setdefault(p, term)
            time.sleep(1.0)
    budget = PLAY_DETAIL_CAP
    for pkg, term in pkgs.items():
        appid = "play:" + pkg
        if appid in seen:
            continue
        playurl = "https://play.google.com/store/apps/details?id=" + pkg
        name, dev, desc, url = pkg, "", "", playurl
        if budget > 0:
            name, dev, desc, url = _play_detail(pkg)
            name = name or pkg
            budget -= 1
            time.sleep(0.7)
        if not _app_relevant(name, "", f"{pkg} {desc}"):
            continue
        found.append({
            "store": "Play", "id": appid, "name": name, "developer": dev,
            "genre": "", "url": url, "updated": "", "country": "", "term": term,
        })

    new = []
    for a in found:
        if a["id"] in seen:
            continue
        seen[a["id"]] = now_iso()
        a["first_seen"] = seen[a["id"]]
        new.append(a)

    if new:
        with open(APPS_LOG, "a", encoding="utf-8") as f:
            for a in new:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
    with open(APPS_SEEN, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False)

    latest = []
    if os.path.exists(APPS_LOG):
        with open(APPS_LOG, encoding="utf-8") as f:
            for ln in f.read().splitlines()[-300:]:
                try:
                    latest.append(json.loads(ln))
                except ValueError:
                    pass
    latest.sort(key=lambda a: a.get("first_seen", ""), reverse=True)
    with open(APPS_LATEST, "w", encoding="utf-8") as f:
        json.dump(latest[:APPS_KEEP], f, ensure_ascii=False, indent=2)

    msg = f"{now_iso()} apps: +{len(new)} new (scanned {len(found)})"
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)
    return new


# ---------------------------------------------------------------- dashboard
# Rendering lives in separate modules (redesigned): dashboard.py builds
# docs/index.html, chart.py builds docs/history.html. Imported lazily so a
# render-module error never blocks data collection.
def build_dashboard():
    """Regenerate the dashboard + history pages from current data."""
    try:
        import dashboard
        dashboard.build_dashboard()
    except Exception as e:  # noqa: BLE001
        print(f"{now_iso()} dashboard render failed: {e}")
    try:
        import chart
        chart.build_chart()
    except Exception as e:  # noqa: BLE001
        print(f"{now_iso()} chart render failed: {e}")


# ---------------------------------------------------------------- history / linkedin
HISTORY = os.path.join(DATA_DIR, "history.jsonl")


def _count_json_list(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return len(json.load(f))
        except (OSError, ValueError):
            return 0
    return 0


def record_history(pages, links, sitemap_present):
    """Append today's metrics to history.jsonl (one record per date, latest wins)."""
    rec = {"date": today(), "pages": len(pages), "links": len(links),
           "news_total": _count_json_list(NEWS_LATEST),
           "apps_total": _count_json_list(APPS_LATEST),
           "sitemap": bool(sitemap_present), "ts": now_iso()}
    records = {}
    if os.path.exists(HISTORY):
        with open(HISTORY, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                try:
                    r = json.loads(ln)
                    records[r["date"]] = r
                except (ValueError, KeyError):
                    pass
    records[rec["date"]] = rec
    with open(HISTORY, "w", encoding="utf-8") as f:
        for dkey in sorted(records):
            f.write(json.dumps(records[dkey], ensure_ascii=False) + "\n")
    return rec


def run_linkedin():
    """Call the LinkedIn monitor module defensively; return change strings."""
    try:
        import linkedin
        return linkedin.fetch_linkedin() or []
    except Exception as e:  # noqa: BLE001
        print(f"{now_iso()} linkedin fetch failed: {e}")
        return []


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
    news_new = fetch_news()
    apps_new = fetch_apps()
    li_changes = run_linkedin()
    record_history(pages, links, sitemap["present"])
    build_dashboard()

    summary = (
        f"{now_iso()} pages={len(pages)} links={len(links)} "
        f"sitemap={'Y' if sitemap['present'] else 'N'} "
        f"changed={'YES' if changed else 'no'} "
        f"(+{len(d['new_pages'])}p/{len(d['content_changed'])}c/"
        f"{len(d['new_links'])}l) queued {len(pages)} for archive, "
        f"+{len(news_new)} news, +{len(apps_new)} apps, "
        f"linkedin{'(' + '; '.join(li_changes) + ')' if li_changes else '=ok'}"
    )
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(summary + "\n")
    print(summary)


if __name__ == "__main__":
    ensure_dirs()
    if "--archive" in sys.argv:
        archive_step()
    elif "--news" in sys.argv:        # periodic: news + apps + linkedin + republish
        fetch_news()
        fetch_apps()
        run_linkedin()
        build_dashboard()
    elif "--apps" in sys.argv:
        fetch_apps()
        build_dashboard()
    else:
        main()
