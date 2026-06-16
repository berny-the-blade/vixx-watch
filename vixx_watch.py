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
ARCHIVE_BATCH = 8          # attempt ALL pending pages each fire (authenticated SPN2;
                           # failures stay pending and retry next fire, no starvation)

# ---- news / mentions monitoring ----
NEWS_KEEP = 80             # articles kept in the dashboard feed
# Vietnamese crypto outlets — Vixex-scoped site: queries (edit freely).
VN_CRYPTO_SITES = ["coin68.com", "tapchibitcoin.io", "coinnews.vn", "blogtienso.net"]
# Crypto-exchange / regulation focus — keeps Vixex + licensing/market-structure
# articles, drops generic bitcoin/altcoin price chatter.
VN_THEME_Q = (
    '("sàn giao dịch tài sản mã hóa" OR "sàn tài sản mã hóa" OR '
    '"sàn giao dịch tài sản số" OR "sàn giao dịch crypto" OR Vixex OR '
    '"VIX Crypto Assets Exchange" OR ("tài sản mã hóa" AND '
    '(sàn OR "cấp phép" OR "giấy phép" OR "thí điểm" OR "khung pháp lý" OR '
    '"Nghị định" OR UBCKNN OR "sàn nội địa" OR "vi phạm" OR "an toàn thị trường")))')
# Title gate for site-theme articles: must actually be about a crypto exchange /
# regulation / Vixex — not just any article that mentions crypto in passing.
NEWS_FOCUS_RE = re.compile(
    r"(vixex|sàn giao dịch|sàn tài sản|cấp phép|giấy phép|thí điểm|khung pháp lý|"
    r"nghị định|ubcknn|tài sản mã hóa|tài sản số|sàn nội địa|fpt|gelex|"
    r"trung tâm tài chính)", re.I)
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


# ---- VIX Securities app watch (separate: incumbent broker; alert if it adds crypto) ----
SEC_TERMS = ["VIX Securities", "Chứng khoán VIX", "XPower VIX", "Chung khoan VIX"]
SEC_DEV_RE = re.compile(r"(vix\s*securit|chứng khoán\s*vix|chung khoan\s*vix)", re.I)
# Crypto signal — deliberately NOT bare "mã hóa" (that means encryption in VN).
SEC_CRYPTO_RE = re.compile(
    r"(crypto|tài sản mã hóa|tiền mã hóa|tài sản số|tiền số|tiền điện tử|"
    r"bitcoin|blockchain|web3|defi)", re.I)

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
SEC_FILE = os.path.join(DATA_DIR, "securities.json")   # VIX Securities app watch
DOCS_DIR = os.path.join(BASE_DIR, "docs")          # GitHub Pages source
DASHBOARD = os.path.join(DOCS_DIR, "index.html")
# Optional archive.org S3 keys ("accesskey:secret") for authenticated SPN2 —
# forces a FRESH capture each day (anonymous SPN dedups stale ones). Gitignored.
WAYBACK_KEYS_FILE = os.path.join(BASE_DIR, "wayback_keys.txt")
WB_LATEST = "https://web.archive.org/web/29991231235959/"  # redirects to newest capture
WB_HISTORY = "https://web.archive.org/web/*/"               # capture calendar
CHANGELOG = os.path.join(DATA_DIR, "changelog.md")
RUN_LOG = os.path.join(DATA_DIR, "run.log")
WAYBACK_LOG = os.path.join(DATA_DIR, "wayback.log")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")          # separate private repo
CAPTURES_DIR = os.path.join(EVIDENCE_DIR, "captures")      # per-run raw captures

# Don't crawl these (assets / framework internals); still recorded as links.
SKIP_CRAWL_RE = re.compile(
    r"(/_next/|/images/|/logos/|/icons/|/fonts/|/favicon)"
    r"|\.(png|jpe?g|gif|svg|ico|css|js|woff2?|ttf|webp|mp4|pdf)(\?|$)",
    re.I,
)
HREF_RE = re.compile(r'href="([^"]+)"')

# Graphics/asset detection: hash the BYTES of each image so a same-URL change
# (new pixels under the same filename) is caught, not just src changes.
ASSET_CAP = 80                         # max assets fetched+hashed per run
ASSET_EXT_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|avif|bmp)(\?|$)", re.I)
SRC_RE = re.compile(r'src="([^"]+)"')
CSS_URL_RE = re.compile(r"url\((['\"]?)([^)'\"]+)\1\)")
OG_IMG_RE = re.compile(r'(?:property|name)="og:image"[^>]*content="([^"]+)"')

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


def sha_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def fetch_bytes(url, timeout=FETCH_TIMEOUT):
    """Fetch raw bytes (for hashing images). Returns (status, bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    pass
            return r.status, data
    except Exception:  # noqa: BLE001
        return 0, b""


def extract_assets(decoded, base):
    """Internal image/graphic URLs referenced by a page (src, CSS url(), og:image)."""
    found = set()
    cands = (SRC_RE.findall(decoded)
             + [m[1] for m in CSS_URL_RE.findall(decoded)]
             + OG_IMG_RE.findall(decoded))
    for raw in cands:
        u = normalize(raw, base)
        if not u or not is_internal(u):
            continue
        if "/_next/" in u:                 # skip Next.js image-optimizer wrappers
            continue
        if ASSET_EXT_RE.search(u):
            found.add(u)
    return found


def capture_assets(urls, run_id):
    """Fetch each graphic and store VERBATIM bytes + meta under
    captures/<run_id>/assets/. Returns hashes {url: raw_sha256}."""
    hashes = {}
    adir = os.path.join(CAPTURES_DIR, run_id, "assets")
    os.makedirs(adir, exist_ok=True)
    for u in sorted(urls)[:ASSET_CAP]:
        cap = fetch_capture(u)
        if cap["status"] != 200 or not cap["raw"]:
            continue
        raw_sha = sha_bytes(cap["raw"])
        hashes[u] = raw_sha
        name = cap_name(u)
        m = ASSET_EXT_RE.search(u)
        ext = m.group(0).split("?")[0] if m else ""
        try:
            with open(os.path.join(adir, name + ext), "wb") as f:
                f.write(cap["raw"])
            with open(os.path.join(adir, name + ".meta.json"), "w", encoding="utf-8") as f:
                json.dump({"url": u, "final_url": cap["final_url"],
                           "status": cap["status"], "headers": cap["headers"],
                           "raw_sha256": raw_sha, "bytes": len(cap["raw"]),
                           "fetched_at": now_iso()}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        time.sleep(0.3)
    return hashes


# ---------------------------------------------------------------- forensic capture
def run_stamp():
    """Compact UTC run id, e.g. 20260615T120003Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def cap_name(url):
    return re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")[:150]


class _RedirectRec(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append({"code": code, "to": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_capture(url, timeout=FETCH_TIMEOUT):
    """Forensic fetch: verbatim bytes + full headers + redirect chain + status."""
    rec = _RedirectRec()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ctx), rec)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, identity"})
    info = {"url": url, "final_url": url, "status": 0, "headers": {},
            "redirects": [], "raw": b"", "body": b"", "text": "", "error": ""}
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            body = raw
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    body = gzip.decompress(raw)
                except OSError:
                    body = raw
            info.update(final_url=r.geturl(), status=getattr(r, "status", 200),
                        headers=dict(r.headers.items()), redirects=rec.hops,
                        raw=raw, body=body, text=body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        info.update(status=e.code, error=f"HTTP {e.code}",
                    headers=dict((e.headers or {}).items()))
    except Exception as e:  # noqa: BLE001
        info.update(error=str(e))
    return info


def capture_tls(host, port=443):
    """Capture the server's TLS certificate (DER bytes + parsed fields). The
    expired vixx.vn cert is itself evidence. Returns (meta, der_bytes)."""
    import socket
    meta = {"host": host, "port": port, "error": ""}
    try:
        ctx = ssl._create_unverified_context()
        with socket.create_connection((host, port), timeout=20) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True) or b""
                meta["tls_version"] = ss.version()
                meta["cipher"] = ss.cipher()
        meta["der_sha256"] = sha_bytes(der)
        meta["der_len"] = len(der)
        # Parse human-readable fields from the DER via openssl if available.
        try:
            pem = ssl.DER_cert_to_PEM_cert(der)
            import subprocess
            out = subprocess.run(
                ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates",
                 "-fingerprint", "-sha256"],
                input=pem, capture_output=True, text=True, timeout=15)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        meta[k.strip().lower().replace(" ", "_")] = v.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return meta, der
    except Exception as e:  # noqa: BLE001
        meta["error"] = str(e)
        return meta, b""


# ---------------------------------------------------------------- crawl
def crawl(run_id):
    """Crawl all reachable pages, storing VERBATIM captures (raw bytes + full
    headers + redirect chain) under evidence/captures/<run_id>/pages/.
    Returns (pages, all_links, asset_urls, hosts)."""
    pages = {}
    all_links = set()
    asset_urls = set()
    hosts = set()
    seen = set()
    queue = deque(SEEDS)
    pdir = os.path.join(CAPTURES_DIR, run_id, "pages")
    os.makedirs(pdir, exist_ok=True)

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

        cap = fetch_capture(norm)
        final = normalize(cap["final_url"], SITE) or norm
        if final in pages:
            continue
        if cap["status"] != 200 or not cap["raw"]:
            continue  # unreachable/404 (e.g. vixex.vn not live yet) -> not a live page

        hosts.add(urllib.parse.urlparse(final).netloc)
        decoded = decode_payload(cap["text"])
        name = cap_name(final)
        raw_sha = sha_bytes(cap["raw"])
        try:
            with open(os.path.join(pdir, name + ".raw"), "wb") as f:
                f.write(cap["raw"])
            with open(os.path.join(pdir, name + ".meta.json"), "w",
                      encoding="utf-8") as f:
                json.dump({
                    "url": norm, "final_url": final, "status": cap["status"],
                    "redirects": cap["redirects"], "headers": cap["headers"],
                    "raw_file": name + ".raw", "raw_sha256": raw_sha,
                    "content_hash": sha(clean_for_hash(cap["text"])),
                    "bytes": len(cap["raw"]), "fetched_at": now_iso(),
                }, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

        pages[final] = {
            "hash": sha(clean_for_hash(cap["text"])),  # change-detection hash
            "raw_sha256": raw_sha,                       # integrity over raw bytes
            "status": cap["status"], "len": len(cap["raw"]),
            "capture": f"captures/{run_id}/pages/{name}.raw",
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
                and cap["status"] == 200
            ):
                queue.append(link)

        asset_urls |= extract_assets(decoded, final)  # images/graphics on this page
        time.sleep(CRAWL_DELAY)

    return pages, sorted(all_links), asset_urls, hosts


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

    oa, na = old.get("assets", {}), new.get("assets", {})
    changed_assets = sorted(u for u in set(oa) & set(na) if oa[u] != na[u])

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
        "new_graphics": sorted(set(na) - set(oa)),
        "removed_graphics": sorted(set(oa) - set(na)),
        "changed_graphics": changed_assets,
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
            "new_graphics",
            "removed_graphics",
            "changed_graphics",
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
        ("New graphics", d["new_graphics"]),
        ("Changed graphics", d["changed_graphics"]),
        ("Removed graphics", d["removed_graphics"]),
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
def _load_wayback_keys():
    """Return (access, secret) from wayback_keys.txt, or None for anonymous."""
    try:
        with open(WAYBACK_KEYS_FILE, encoding="utf-8") as f:
            line = f.read().strip()
        if ":" in line:
            a, s = line.split(":", 1)
            if a.strip() and s.strip():
                return a.strip(), s.strip()
    except OSError:
        pass
    return None


def _spn2_save(url, access, secret):
    """Authenticated Save-Page-Now: forces a fresh capture, polls for the result.
    Returns (status, archived_url)."""
    hdr = {"Authorization": f"LOW {access}:{secret}", "Accept": "application/json",
           "User-Agent": USER_AGENT}
    body = urllib.parse.urlencode({
        "url": url, "capture_all": "1", "skip_first_archive": "1",
        "if_not_archived_within": "0",   # always make a new capture
    }).encode()
    req = urllib.request.Request("https://web.archive.org/save", data=body, method="POST",
                                 headers={**hdr, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return (f"ERR spn2 HTTP {e.code}", "")
    except Exception as e:  # noqa: BLE001
        return (f"ERR spn2 {e}", "")
    job = resp.get("job_id")
    if not job:
        return (f"ERR spn2 {resp.get('status_ext') or resp.get('message') or resp}"[:90], "")
    for _ in range(12):                  # poll up to ~12 * 8s
        time.sleep(8)
        sreq = urllib.request.Request("https://web.archive.org/save/status/" + job, headers=hdr)
        try:
            with urllib.request.urlopen(sreq, timeout=30) as r:
                st = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        if st.get("status") == "success":
            ts = st.get("timestamp", "")
            ou = st.get("original_url") or url
            return ("OK", f"https://web.archive.org/web/{ts}/{ou}")
        if st.get("status") == "error":
            return (f"ERR spn2 {st.get('status_ext') or st.get('message')}"[:90], "")
    return ("ERR spn2 timeout", "")


def wayback_one(url, attempts=1):
    """Try to archive one URL. Returns (status, archived).

    Uses authenticated SPN2 (fresh daily capture) when wayback_keys.txt exists;
    otherwise anonymous Save-Page-Now (which may return a stale cached capture).
    attempts=1 spaces retries across scheduler fires.
    """
    keys = _load_wayback_keys()
    if keys:
        # Authenticated SPN2 forces a FRESH capture. Do NOT fall back to anonymous
        # on failure — anonymous returns a stale cached capture and would record an
        # old date as "OK". Return the error so the archiver keeps it pending and
        # retries next fire until a genuinely fresh capture succeeds.
        st, arch = _spn2_save(url, keys[0], keys[1])
        if st != "OK":
            print(f"{now_iso()} SPN2 not fresh for {url} ({st}); keeping pending")
        return (st, arch)
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
    """Archive up to `batch` still-pending pages; log results.

    Drains whatever is pending regardless of the queue's date — so a page left
    pending when the UTC day rolls over (e.g. an SPN 520) still gets retried by
    the next archiver fire instead of being abandoned until the next crawl.
    """
    q = load_pending()
    if not q.get("pages"):
        msg = f"{now_iso()} archive: no queue yet (first crawl pending)"
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
        f"{ok}/{len(q['pages'])} archived (queue {q.get('date','?')}), {remaining} pending"
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
    for site in VN_CRYPTO_SITES:                # Vixex-scoped on crypto outlets
        q = f'"Vixex" OR "VIX Crypto Assets Exchange" site:{site}'
        _, status, text = fetch(_gnews_url(q, "hl=vi&gl=VN&ceid=VN:vi"), verify=True)
        if status == 200 and not text.startswith("__ERROR__"):
            collected += parse_feed(text, "vi", f"site:{site}", site)
        time.sleep(1.0)
    # Focused VN crypto-exchange/regulation theme. Google News RSS ignores the
    # site: operator, so one theme query covers all outlets; the NEWS_FOCUS_RE
    # title gate below keeps only genuine exchange/regulation/Vixex articles.
    _, status, text = fetch(_gnews_url(VN_THEME_Q, "hl=vi&gl=VN&ceid=VN:vi"), verify=True)
    if status == 200 and not text.startswith("__ERROR__"):
        collected += parse_feed(text, "vi", "vn:exchange", "Google News")
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
        if it["query"].startswith("vn:") and not NEWS_FOCUS_RE.search(t):
            continue  # mainstream-outlet article not actually about a crypto exchange
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
def _itunes_search(term, country, limit=25):
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "country": country, "entity": "software", "limit": limit})
    _, status, text = fetch(url, verify=True)
    if status == 200 and not text.startswith("__ERROR__"):
        try:
            return json.loads(text).get("results", [])
        except ValueError:
            return []
    return []


def _apple_search(term, country):
    out = []
    for r in _itunes_search(term, country):
        name = r.get("trackName", "") or ""
        desc = r.get("description", "") or ""
        if _app_relevant(name, r.get("primaryGenreName", "") or "", desc[:400]):
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


def fetch_securities():
    """Track VIX Securities' app(s); alert if a listing starts mentioning crypto."""
    prev = {}
    if os.path.exists(SEC_FILE):
        try:
            with open(SEC_FILE, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = {}

    cur = {}
    for term in SEC_TERMS:                       # Apple
        for c in ("vn", "us"):
            for r in _itunes_search(term, c):
                name = r.get("trackName", "") or ""
                dev = r.get("sellerName", "") or ""
                desc = r.get("description", "") or ""
                if not (SEC_DEV_RE.search(dev) or SEC_DEV_RE.search(name)):
                    continue
                appid = "ios:" + str(r.get("bundleId") or r.get("trackId"))
                cur[appid] = {
                    "store": "iOS", "name": name, "developer": dev,
                    "url": r.get("trackViewUrl", "") or "",
                    "genre": r.get("primaryGenreName", "") or "",
                    "crypto": bool(SEC_CRYPTO_RE.search(name + " " + desc)),
                }
            time.sleep(0.5)

    pkgs = {}                                    # Google Play
    for term in SEC_TERMS:
        for gl, hl in (("VN", "vi"), ("US", "en")):
            for p in _play_search_pkgs(term, gl, hl):
                pkgs.setdefault(p, term)
            time.sleep(1.0)
    budget = 15
    for pkg in pkgs:
        if budget <= 0:
            break
        name, dev, desc, url = _play_detail(pkg)
        budget -= 1
        time.sleep(0.7)
        if not SEC_DEV_RE.search(f"{pkg} {name} {dev} {desc}"):
            continue
        cur["play:" + pkg] = {
            "store": "Play", "name": name or pkg, "developer": dev, "url": url,
            "genre": "", "crypto": bool(SEC_CRYPTO_RE.search(name + " " + desc)),
        }

    # Diff vs previous: new apps and crypto-signal flips are the alerts.
    now = now_iso()
    alerts = []
    for appid, rec in cur.items():
        old = prev.get(appid)
        rec["first_seen"] = (old or {}).get("first_seen", now)
        rec["last_checked"] = now
        if not old:
            tag = " [CRYPTO!]" if rec["crypto"] else ""
            alerts.append(f"VIX Securities app found: {rec['name']} ({rec['store']}){tag}")
        elif rec["crypto"] and not old.get("crypto"):
            alerts.append(f"VIX Securities app '{rec['name']}' NOW mentions crypto")
    with open(SEC_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    msg = (f"{now_iso()} securities: {len(cur)} app(s), "
           f"crypto={sum(1 for r in cur.values() if r['crypto'])}, +{len(alerts)} alert(s)")
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)
    return alerts


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
STATUS_FILE = os.path.join(DATA_DIR, "status.json")  # per-run deltas for the banner


def update_status(**cats):
    """Record what each run actually found (deltas), so the dashboard banner can
    reflect day-to-day change instead of cumulative counts. Each category gets a
    fresh timestamp; categories a run didn't compute are left untouched."""
    st = {}
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
    now = now_iso()
    for key, val in cats.items():
        st[key] = {"ts": now, **val}
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


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


def seal_evidence(run_id, pages, hosts):
    """Capture TLS + screenshots, seal copies of mutable state/logs, then write a
    hash-chained manifest entry over EVERY artifact and OpenTimestamp it."""
    import shutil
    cap_dir = os.path.join(CAPTURES_DIR, run_id)

    tdir = os.path.join(cap_dir, "tls")
    os.makedirs(tdir, exist_ok=True)
    for h in sorted(hosts):
        meta, der = capture_tls(h)
        try:
            if der:
                with open(os.path.join(tdir, h + ".der"), "wb") as f:
                    f.write(der)
            with open(os.path.join(tdir, h + ".json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    try:
        import screenshot
        if screenshot.available():
            screenshot.capture(sorted(pages), os.path.join(cap_dir, "screenshots"))
    except Exception as e:  # noqa: BLE001
        print(f"{now_iso()} screenshot failed: {e}")

    # Seal copies of the mutable state/logs so this run's manifest covers them.
    sdir = os.path.join(cap_dir, "state")
    os.makedirs(sdir, exist_ok=True)
    for p in (STATE_FILE, CHANGELOG, RUN_LOG, WAYBACK_LOG, NEWS_LOG, APPS_LOG,
              SEC_FILE, NEWS_SEEN, APPS_SEEN, HISTORY, PENDING_FILE, STATUS_FILE,
              os.path.join(DATA_DIR, "linkedin.json"),
              os.path.join(DATA_DIR, "linkedin_changes.md"),
              os.path.join(DATA_DIR, "news_latest.json"),
              os.path.join(DATA_DIR, "apps_latest.json")):
        if os.path.exists(p):
            try:
                shutil.copy2(p, os.path.join(sdir, os.path.basename(p)))
            except OSError:
                pass

    try:
        import evidence
        artifacts = [os.path.join(root, fn)
                     for root, _, files in os.walk(cap_dir) for fn in files]
        wb = load_pending()
        entry = evidence.record_run("crawl", artifacts, extra={
            "run_id": run_id, "ts": now_iso(), "page_count": len(pages),
            "wayback_captures": {u: i.get("archived", "")
                                 for u, i in wb.get("pages", {}).items()},
        })
        runs_file = os.path.join(EVIDENCE_DIR, "runs", f"{entry['seq']}-{run_id}.json")
        stamps = []
        if evidence.ots_available() and evidence.ots_stamp(runs_file):
            stamps.append("ots")
        stamps += [name for name, ok in evidence.tsa_stamp_all(runs_file) if ok]
        msg = (f"{now_iso()} evidence: run {entry['seq']} sealed, "
               f"{len(entry.get('artifacts', []))} artifacts, "
               f"timestamps={'+'.join(stamps) or 'none'}")
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg)
        return entry
    except Exception as e:  # noqa: BLE001
        print(f"{now_iso()} evidence sealing failed: {e}")
        return None


# ---------------------------------------------------------------- main
def main():
    ensure_dirs()
    run_id = run_stamp()
    start = now_iso()

    old = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}

    pages, links, asset_urls, hosts = crawl(run_id)
    assets = capture_assets(asset_urls, run_id)
    sitemap = check_sitemap()
    new = {"pages": pages, "links": links, "assets": assets,
           "sitemap": sitemap, "crawled_at": start, "run_id": run_id}

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
    sec_alerts = fetch_securities()
    li_changes = run_linkedin()
    record_history(pages, links, sitemap["present"])
    update_status(
        site={"changed": bool(changed and not d["first_run"])},
        news={"n": len(news_new)}, apps={"n": len(apps_new)},
        securities={"n": len(sec_alerts)}, linkedin={"n": len(li_changes)},
    )
    build_dashboard()
    seal_evidence(run_id, pages, hosts)  # TLS + screenshots + hash-chained manifest + OTS

    summary = (
        f"{now_iso()} pages={len(pages)} links={len(links)} "
        f"sitemap={'Y' if sitemap['present'] else 'N'} "
        f"changed={'YES' if changed else 'no'} "
        f"(+{len(d['new_pages'])}p/{len(d['content_changed'])}c/"
        f"{len(d['new_links'])}l/{len(d['changed_graphics'])}g) "
        f"{len(assets)} graphics, queued {len(pages)} for archive, "
        f"+{len(news_new)} news, +{len(apps_new)} apps, "
        f"+{len(sec_alerts)} sec-alerts, "
        f"linkedin{'(' + '; '.join(li_changes) + ')' if li_changes else '=ok'}"
    )
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(summary + "\n")
    print(summary)


if __name__ == "__main__":
    ensure_dirs()
    if "--archive" in sys.argv:
        archive_step()
    elif "--news" in sys.argv:        # periodic: news + apps + securities + linkedin
        news_new = fetch_news()
        apps_new = fetch_apps()
        sec_alerts = fetch_securities()
        li_changes = run_linkedin()
        update_status(
            news={"n": len(news_new)}, apps={"n": len(apps_new)},
            securities={"n": len(sec_alerts)}, linkedin={"n": len(li_changes)},
        )
        build_dashboard()
    elif "--apps" in sys.argv:
        apps_new = fetch_apps()
        update_status(apps={"n": len(apps_new)})
        build_dashboard()
    elif "--verify" in sys.argv:      # re-hash all evidence + walk the chain
        import evidence
        print(json.dumps(evidence.verify(), indent=2))
    else:
        main()
