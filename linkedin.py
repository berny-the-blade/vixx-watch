"""Standalone LinkedIn public company-page monitor for Vixex.

Stdlib only. Fetches the public LinkedIn company page, extracts whatever
public metadata is available (meta/og tags + JSON-LD), detects blocking
(authwall/403/429/999), writes a snapshot, and diffs against the previous
snapshot to log changes.

Designed to be imported by vixx_watch.py (calls fetch_linkedin()) or run
directly.
"""

import datetime
import gzip
import html
import io
import json
import os
import re
import ssl
import urllib.error
import urllib.request

LINKEDIN_URL = "https://vn.linkedin.com/company/vixex"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "linkedin.json")
CHANGES_FILE = os.path.join(DATA_DIR, "linkedin_changes.md")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TIMEOUT = 30


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


def _decode_body(raw, headers):
    """Decode possibly-gzipped bytes into a str."""
    if not raw:
        return ""
    enc = ""
    try:
        enc = (headers.get("Content-Encoding") or "").lower()
    except Exception:
        enc = ""
    data = raw
    if "gzip" in enc:
        try:
            data = gzip.decompress(raw)
        except Exception:
            # Some servers mislabel; try gzip stream, else fall back to raw.
            try:
                data = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except Exception:
                data = raw
    for codec in ("utf-8", "latin-1"):
        try:
            return data.decode(codec, errors="replace")
        except Exception:
            continue
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _fetch_raw():
    """Return (status:int, body:str, error:str). Never raises."""
    ctx = ssl.create_default_context()  # normal verification
    req = urllib.request.Request(
        LINKEDIN_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "vi,en",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 0
            raw = resp.read()
            body = _decode_body(raw, resp.headers)
            return int(status), body, ""
    except urllib.error.HTTPError as e:
        # HTTPError is also a response: read body if possible.
        status = int(getattr(e, "code", 0) or 0)
        body = ""
        try:
            raw = e.read()
            body = _decode_body(raw, getattr(e, "headers", {}) or {})
        except Exception:
            body = ""
        return status, body, "http_error:%s" % status
    except urllib.error.URLError as e:
        return 0, "", "url_error:%s" % (getattr(e, "reason", e),)
    except ssl.SSLError as e:
        return 0, "", "ssl_error:%s" % (e,)
    except Exception as e:  # noqa: BLE001 - never crash
        return 0, "", "error:%s" % (e,)


def _meta(body, prop_attr, prop_val):
    """Extract a meta tag content by property/name, attr order tolerant."""
    if not body:
        return ""
    # property/name first, then content
    pat1 = re.compile(
        r'<meta[^>]+%s=["\']%s["\'][^>]*\bcontent=["\']([^"\']*)["\']'
        % (prop_attr, re.escape(prop_val)),
        re.IGNORECASE,
    )
    # content first, then property/name
    pat2 = re.compile(
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*%s=["\']%s["\']'
        % (prop_attr, re.escape(prop_val)),
        re.IGNORECASE,
    )
    for pat in (pat1, pat2):
        m = pat.search(body)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _all_ld_json(body):
    """Return list of parsed JSON-LD objects (best effort)."""
    out = []
    if not body:
        return out
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        body,
        re.IGNORECASE | re.DOTALL,
    ):
        chunk = m.group(1).strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except Exception:
            # try to salvage by unescaping HTML entities
            try:
                obj = json.loads(html.unescape(chunk))
            except Exception:
                continue
        if isinstance(obj, list):
            out.extend([o for o in obj if isinstance(o, dict)])
        elif isinstance(obj, dict):
            # @graph wrapper
            graph = obj.get("@graph")
            if isinstance(graph, list):
                out.extend([o for o in graph if isinstance(o, dict)])
            out.append(obj)
    return out


def _find_org(ld_objects):
    """Pick the Organization-like JSON-LD object."""
    for o in ld_objects:
        t = o.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(
            str(x).lower() in ("organization", "corporation")
            for x in types
        ):
            return o
    # fallback: any object that has a "name"
    for o in ld_objects:
        if o.get("name"):
            return o
    return {}


def _parse_followers(body):
    """Find a follower count in visible text. Returns int or None."""
    if not body:
        return None
    # Patterns: "12,345 followers", "12.345 người theo dõi", "1K followers"
    candidates = []
    for pat in (
        r'([\d.,]+)\s*followers',
        r'([\d.,]+)\s*người theo dõi',
        r'"followerCount"\s*:\s*"?(\d+)"?',
        r'"followingInfo".{0,120}?"followerCount"\s*:\s*(\d+)',
    ):
        for m in re.finditer(pat, body, re.IGNORECASE):
            candidates.append(m.group(1))
    for raw in candidates:
        n = _num(raw)
        if n is not None:
            return n
    return None


def _num(s):
    """Parse a number that may use , or . as thousands separators or K/M."""
    if s is None:
        return None
    s = s.strip()
    mult = 1
    m = re.match(r'^([\d.,]+)\s*([KkMm])?$', s)
    if not m:
        s = re.sub(r'[^\d]', '', s)
        if s.isdigit():
            return int(s)
        return None
    digits, suffix = m.group(1), m.group(2)
    if suffix:
        if suffix.lower() == "k":
            mult = 1000
        elif suffix.lower() == "m":
            mult = 1000000
        # for K/M, treat the dot as decimal
        digits = digits.replace(",", "")
        try:
            return int(float(digits) * mult)
        except Exception:
            return None
    # plain integer with thousands separators
    cleaned = re.sub(r'[^\d]', '', digits)
    if cleaned.isdigit():
        return int(cleaned)
    return None


def _is_blocked(status, body):
    if status in (403, 429, 999):
        return True
    if not body:
        return status not in (200,)
    low = body.lower()
    markers = (
        "authwall",
        "join linkedin to see",
        "join linkedin to view",
        "sign in to see",
        "please sign in",
    )
    if any(mk in low for mk in markers):
        return True
    return False


def _extract(body):
    """Return dict of extracted fields from public HTML."""
    result = {
        "name": "",
        "tagline": "",
        "followers": None,
        "employees": "",
        "industry": "",
        "website": "",
        "posts": [],
    }
    if not body:
        return result

    og_title = _meta(body, "property", "og:title")
    og_desc = _meta(body, "property", "og:description")

    ld = _all_ld_json(body)
    org = _find_org(ld)

    # name
    name = org.get("name") or og_title or ""
    if isinstance(name, str):
        result["name"] = html.unescape(name).strip()

    # tagline / description
    desc = org.get("description") or og_desc or ""
    if isinstance(desc, str):
        result["tagline"] = html.unescape(desc).strip()

    # website
    website = org.get("url") or org.get("sameAs") or ""
    if isinstance(website, list):
        website = website[0] if website else ""
    if isinstance(website, str):
        result["website"] = website.strip()

    # industry
    industry = org.get("industry") or ""
    if not industry:
        m = re.search(r'"industry"\s*:\s*"([^"]+)"', body)
        if m:
            industry = m.group(1)
    if isinstance(industry, str):
        result["industry"] = html.unescape(industry).strip()

    # employees / headcount
    employees = ""
    nemp = org.get("numberOfEmployees")
    if isinstance(nemp, dict):
        lo = nemp.get("minValue", "")
        hi = nemp.get("maxValue", "")
        if lo or hi:
            employees = "%s-%s" % (lo, hi)
    elif nemp:
        employees = str(nemp)
    if not employees:
        m = re.search(
            r'([\d,]+\s*-\s*[\d,]+|[\d,]+\+?)\s*employees', body, re.IGNORECASE
        )
        if m:
            employees = m.group(1).strip()
    result["employees"] = employees

    # followers
    result["followers"] = _parse_followers(body)

    # posts (public HTML rarely contains them; best-effort)
    posts = []
    for m in re.finditer(
        r'data-test-[^>]*activity[^>]*>(.*?)</', body, re.IGNORECASE | re.DOTALL
    ):
        txt = re.sub(r'<[^>]+>', ' ', m.group(1))
        txt = html.unescape(re.sub(r'\s+', ' ', txt)).strip()
        if txt and len(txt) > 10:
            posts.append({"text": txt[:500], "when": ""})
    result["posts"] = posts[:10]

    return result


def _load_previous():
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _diff(prev, new):
    """Return (change_strings, change_md_lines)."""
    changes = []
    if not prev:
        return changes  # first run, no diff

    for field in ("tagline", "employees", "industry", "website"):
        old = prev.get(field) or ""
        cur = new.get(field) or ""
        if old != cur:
            changes.append("%s: %r -> %r" % (field, old, cur))

    old_f = prev.get("followers")
    new_f = new.get("followers")
    if old_f != new_f and (old_f is not None or new_f is not None):
        changes.append("followers: %s -> %s" % (old_f, new_f))

    old_texts = set()
    for p in prev.get("posts") or []:
        if isinstance(p, dict):
            old_texts.add(p.get("text", ""))
    for p in new.get("posts") or []:
        if isinstance(p, dict):
            t = p.get("text", "")
            if t and t not in old_texts:
                changes.append("new post: %s" % t)

    # accessibility transition
    if bool(prev.get("accessible")) != bool(new.get("accessible")):
        changes.append(
            "accessible: %s -> %s"
            % (prev.get("accessible"), new.get("accessible"))
        )

    return changes


def _append_changes(ts, changes):
    if not changes:
        return
    try:
        lines = ["## %s" % ts]
        for c in changes:
            lines.append("- %s" % c)
        lines.append("")
        with open(CHANGES_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def fetch_linkedin():
    """Fetch, snapshot, and diff the public LinkedIn page. Returns change list."""
    _ensure_data_dir()
    ts = _now_iso()

    status, body, err = _fetch_raw()

    blocked = _is_blocked(status, body)
    accessible = (status == 200) and not blocked

    if accessible:
        fields = _extract(body)
    else:
        # Even when blocked, try a light extraction (og tags sometimes leak).
        fields = _extract(body) if body else {
            "name": "",
            "tagline": "",
            "followers": None,
            "employees": "",
            "industry": "",
            "website": "",
            "posts": [],
        }

    snapshot = {
        "fetched_at": ts,
        "url": LINKEDIN_URL,
        "accessible": bool(accessible),
        "raw_status": int(status),
        "name": fields.get("name", ""),
        "tagline": fields.get("tagline", ""),
        "followers": fields.get("followers"),
        "employees": fields.get("employees", ""),
        "industry": fields.get("industry", ""),
        "website": fields.get("website", ""),
        "posts": fields.get("posts", []),
    }
    if err:
        snapshot["fetch_error"] = err

    prev = _load_previous()
    changes = _diff(prev, snapshot)
    _append_changes(ts, changes)

    # Always write a valid snapshot, even when blocked.
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return changes


if __name__ == "__main__":
    print(fetch_linkedin())
