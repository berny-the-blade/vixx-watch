#!/usr/bin/env python3
"""Standalone dashboard renderer for vixx-watch.

Reads JSON/MD data files under data/ and writes a static monitoring
dashboard to docs/index.html. Stdlib only. Does NOT import vixx_watch
(reads data files directly, defines its own constants) so it can run
concurrently with the crawler.
"""

import json
import os
import html
import re
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
OUTPUT = os.path.join(DOCS_DIR, "index.html")

# Wayback constants (self-contained; do not import from vixx_watch).
WB_LATEST = "https://web.archive.org/web/29991231235959/"  # +url -> newest capture
WB_HISTORY = "https://web.archive.org/web/*/"               # +url -> capture calendar

NEWS_LIMIT = 5         # visible at once (client-side toggle picks which 5)
NEWS_POOL = 80         # items rendered hidden so the narrow toggle has material
APPS_LIMIT = 25
CHANGELOG_LIMIT = 5
RECENT_DAYS = 3        # status banner window
APP_NEW_DAYS = 7       # "NEW" badge window for apps


# ---------------------------------------------------------------------------
# Defensive data loading
# ---------------------------------------------------------------------------
def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _read_json(path, default):
    txt = _read_text(path)
    if not txt.strip():
        return default
    try:
        data = json.loads(txt)
    except Exception:
        return default
    return data if data is not None else default


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
_RFC2822_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_dt(s):
    """Best-effort parse of ISO8601, RFC2822, or 'YYYY-MM-DD'. Returns aware
    UTC datetime or None."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    # ISO8601 (optionally trailing Z)
    iso = s
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # RFC2822: 'Sat, 06 Jun 2026 05:19:13 GMT'
    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})"
        r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        s,
    )
    if m:
        day = int(m.group(1))
        mon = _RFC2822_MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss, tzinfo=timezone.utc)
            except Exception:
                return None
    # Bare date 'YYYY-MM-DD'
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def rel_time(s, now=None):
    """Relative-time string for a date string; falls back to the raw string
    when unparseable."""
    dt = parse_dt(s)
    if dt is None:
        return s or "—"
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    secs = delta.total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 60:
        out = "just now"
        return out
    mins = secs / 60
    if mins < 60:
        out = "%dm" % int(mins)
    elif mins < 60 * 24:
        out = "%dh" % int(mins / 60)
    elif mins < 60 * 24 * 30:
        out = "%dd" % int(mins / (60 * 24))
    elif mins < 60 * 24 * 365:
        out = "%dmo" % int(mins / (60 * 24 * 30))
    else:
        out = "%dy" % int(mins / (60 * 24 * 365))
    return (out + " from now") if future else (out + " ago")


def within_days(s, days, now=None):
    dt = parse_dt(s)
    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    return 0 <= (now - dt).total_seconds() <= days * 86400


def _e(s):
    return html.escape("" if s is None else str(s))


# ---------------------------------------------------------------------------
# Changelog parsing
# ---------------------------------------------------------------------------
def parse_changelog(text):
    """Return a list of entries newest-first:
    {ts, baseline_note, sections: [{title, count, bullets:[...]}]}.
    Entries are split on '## ' headers."""
    if not text or not text.strip():
        return []
    # Split into blocks beginning with a top-level "## " header.
    blocks = re.split(r"(?m)^##\s+", text)
    entries = []
    for blk in blocks:
        blk = blk.strip()
        if not blk:
            continue
        lines = blk.splitlines()
        ts = lines[0].strip()
        baseline_note = ""
        sections = []
        cur = None
        for line in lines[1:]:
            raw = line.rstrip()
            stripped = raw.strip()
            if not stripped:
                continue
            mnote = re.match(r"^_(.*)_$", stripped)
            if mnote and cur is None and not baseline_note:
                baseline_note = mnote.group(1).strip()
                continue
            msec = re.match(r"^###\s+(.*?)(?:\s*\((\d+)\))?\s*$", stripped)
            if msec:
                cur = {
                    "title": msec.group(1).strip(),
                    "count": msec.group(2),
                    "bullets": [],
                }
                sections.append(cur)
                continue
            mbul = re.match(r"^[-*]\s+(.*)$", stripped)
            if mbul and cur is not None:
                cur["bullets"].append(mbul.group(1).strip())
                continue
            # stray line; if italic note before any section keep as note
            if cur is None and not baseline_note:
                baseline_note = stripped
        entries.append({
            "ts": ts,
            "baseline_note": baseline_note,
            "sections": sections,
        })
    # Newest-first: the file is written newest-on-top after a leading blank,
    # so preserve file order (already newest-first per spec) but sort by ts
    # when parseable to be safe.
    def keyf(e):
        dt = parse_dt(e["ts"])
        return dt or datetime.min.replace(tzinfo=timezone.utc)
    if all(parse_dt(e["ts"]) for e in entries) and entries:
        entries.sort(key=keyf, reverse=True)
    return entries


def is_baseline_entry(entry):
    note = (entry.get("baseline_note") or "").lower()
    return "baseline" in note


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
STYLE = """
:root{--bg:#0f1115;--card:#1a1d24;--mut:#8b93a7;--fg:#e6e9ef;--ac:#5b9dff;--ok:#2ea043;--warn:#d29922;--alert:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,Segoe UI,Arial,sans-serif}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1040px;margin:0 auto;padding:24px 18px 60px}
header.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 14px;margin-bottom:4px}
h1{font-size:22px;margin:0;font-weight:700}
.sub{color:var(--mut);font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 8px}
.stat{background:var(--card);border-radius:8px;padding:9px 14px;min-width:84px}
.stat .n{font-size:19px;font-weight:700;line-height:1.1}
.stat .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.banner{padding:13px 16px;border-radius:9px;margin:14px 0 22px;font-weight:600;font-size:14px}
.banner.alert{background:rgba(248,81,73,.14);border:1px solid var(--alert);color:#ffb4ae}
.banner.ok{background:rgba(46,160,67,.12);border:1px solid var(--ok);color:#85e89d}
.card{background:var(--card);border-radius:11px;padding:16px 18px;margin:16px 0;border:1px solid #21252e}
h2{font-size:15px;margin:0 0 4px;font-weight:700;letter-spacing:.01em}
.cardnote{color:var(--mut);font-size:12px;margin:0 0 12px}
.toggle{float:right;font-size:12px;color:var(--mut);font-weight:600;cursor:pointer;user-select:none}
.toggle input{vertical-align:middle;margin-right:5px;cursor:pointer}
.news{list-style:none;margin:0;padding:0}
.news li{padding:11px 0;border-bottom:1px solid #262a33}
.news li:last-child{border-bottom:0}
.news .t{font-size:15px;font-weight:600;line-height:1.4}
.news .ten{color:var(--mut);font-style:italic;font-size:13.5px;margin-top:2px}
.news .meta{margin-top:5px;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;color:var(--mut);font-size:12px}
.badge{font-size:10.5px;padding:2px 7px;border-radius:10px;font-weight:600;letter-spacing:.03em}
.badge.vi{background:rgba(91,157,255,.18);color:#9cc2ff}
.badge.en{background:rgba(139,147,167,.2);color:#c3c9d6}
.badge.ios{background:rgba(139,147,167,.2);color:#dfe3ec}
.badge.play{background:rgba(46,160,67,.18);color:#85e89d}
.badge.new{background:rgba(248,81,73,.2);color:#ffb4ae}
.apps{list-style:none;margin:0;padding:0}
.apps li{padding:9px 0;border-bottom:1px solid #262a33;display:flex;flex-wrap:wrap;gap:5px 10px;align-items:center}
.apps li:last-child{border-bottom:0}
.apps .an{font-weight:600}
.apps .am{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid #262a33;vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px}
.b{font-size:11px;padding:2px 7px;border-radius:10px}
.b.ok{background:rgba(46,160,67,.2);color:#85e89d}
.b.warn{background:rgba(210,153,34,.2);color:#e3c266}
.b.alert{background:rgba(248,81,73,.2);color:#ffb4ae}
.b.mut{background:#262a33;color:var(--mut)}
.chg{margin:0 0 14px}
.chg:last-child{margin-bottom:0}
.chg .ct{font-weight:600;font-size:13px}
.chg .cn{color:var(--mut);font-style:italic;font-size:12px;margin:2px 0}
.chg h4{font-size:12.5px;margin:9px 0 3px;color:var(--fg)}
.chg ul{margin:0 0 0 2px;padding:0 0 0 18px;font-size:13px}
.chg li{margin:1px 0;word-break:break-all}
details{margin-top:6px}
summary{cursor:pointer;color:var(--ac);font-size:13px}
summary:hover{text-decoration:underline}
details ul{margin:8px 0 0;padding-left:18px;font-size:13px}
details li{margin:2px 0;word-break:break-all}
.muted{color:var(--mut);font-size:12px;margin-top:10px}
.foot{color:var(--mut);font-size:11.5px;margin-top:30px;text-align:center}
"""


def stat(n, label):
    return ('<div class="stat"><div class="n">%s</div>'
            '<div class="l">%s</div></div>') % (_e(n), _e(label))


NARROW_RE = re.compile(r"vixex|\bfpt\b|gelex|vix\s*crypto", re.I)


def _news_scope(it):
    """'narrow' = Vixex / FPT / GELEX specific; 'broad' = general sector news."""
    q = it.get("query") or ""
    if q and q != "vn:exchange":          # entity / Vixex-scoped queries
        return "narrow"
    return "narrow" if NARROW_RE.search(it.get("title", "")) else "broad"


def render_news(news, now):
    total = len(news)
    items = news[:NEWS_POOL]              # render a pool; JS shows NEWS_LIMIT of active filter
    rows = []
    for it in items:
        scope = _news_scope(it)
        title = it.get("title") or "(untitled)"
        link = it.get("link") or "#"
        lang = (it.get("lang") or "").lower()
        title_en = (it.get("title_en") or "").strip()
        rows.append('<li data-scope="%s">' % scope)
        rows.append('<div class="t"><a href="%s" target="_blank" rel="noopener">%s</a></div>'
                    % (_e(link), _e(title)))
        if title_en and lang != "en" and title_en != title:
            rows.append('<div class="ten">%s</div>' % _e(title_en))
        langbadge = '<span class="badge %s">%s</span>' % (
            "en" if lang == "en" else "vi", "EN" if lang == "en" else "VI")
        when = it.get("published") or it.get("first_seen") or ""
        meta = [langbadge]
        if scope == "narrow":
            meta.append('<span class="badge play">Vixex/backer</span>')
        else:
            meta.append('<span class="badge en">sector</span>')
        if it.get("source"):
            meta.append('<span>%s</span>' % _e(it.get("source")))
        meta.append('<span>&middot; %s</span>' % _e(rel_time(when, now)))
        rows.append('<div class="meta">%s</div>' % "".join(meta))
        rows.append('</li>')
    body = "".join(rows) if rows else '<li class="muted">No news items tracked yet.</li>'
    toggle = ('<label class="toggle"><input type="checkbox" id="narrowonly"> '
              'Vixex + backers only</label>')
    note = ('<p class="cardnote">Showing the %d most recent. Tick the box to limit '
            'to Vixex / FPT / GELEX only.</p>' % NEWS_LIMIT)
    script = (
        "<script>(function(){var L=%d,cb=document.getElementById('narrowonly');"
        "if(!cb)return;function f(){var n=cb.checked,s=0;"
        "document.querySelectorAll('#newslist>li').forEach(function(li){"
        "var ok=!n||li.dataset.scope==='narrow';"
        "if(ok&&s<L){li.style.display='';s++;}else{li.style.display='none';}});"
        "try{localStorage.setItem('vixx_narrow',n?'1':'0');}catch(e){}}"
        "cb.addEventListener('change',f);"
        "try{if(localStorage.getItem('vixx_narrow')==='1')cb.checked=true;}catch(e){}"
        "f();})();</script>" % NEWS_LIMIT)
    return ('<section class="card"><h2>News %s</h2>%s'
            '<ul class="news" id="newslist">%s</ul>%s</section>'
            % (toggle, note, body, script))


def render_apps(apps, now):
    items = apps[:APPS_LIMIT]
    if not items:
        return ('<section class="card"><h2>App-store watch</h2>'
                '<p class="muted">No matching apps detected yet.</p></section>')
    rows = []
    for a in items:
        store = (a.get("store") or "").strip()
        sb_cls = "ios" if store.lower() == "ios" else "play"
        sb = '<span class="badge %s">%s</span>' % (sb_cls, _e(store or "?"))
        name = a.get("name") or "(unnamed)"
        url = a.get("url") or "#"
        namehtml = '<span class="an"><a href="%s" target="_blank" rel="noopener">%s</a></span>' % (
            _e(url), _e(name))
        metabits = []
        for k in ("developer", "genre"):
            if a.get(k):
                metabits.append(_e(a.get(k)))
        if a.get("country"):
            metabits.append(_e(str(a.get("country")).upper()))
        meta = '<span class="am">%s</span>' % " · ".join(metabits) if metabits else ""
        newbadge = ""
        if within_days(a.get("first_seen"), APP_NEW_DAYS, now):
            newbadge = '<span class="badge new">NEW</span>'
        rows.append('<li>%s%s%s%s</li>' % (sb, namehtml, meta, newbadge))
    note = ""
    if len(apps) > APPS_LIMIT:
        note = '<p class="cardnote">Showing %d of %d.</p>' % (len(items), len(apps))
    return ('<section class="card"><h2>App-store watch</h2>%s'
            '<ul class="apps">%s</ul></section>') % (note, "".join(rows))


def render_linkedin(li, now):
    if not isinstance(li, dict) or not li:
        return ('<section class="card"><h2>LinkedIn</h2>'
                '<p class="muted">No LinkedIn data yet.</p></section>')
    url = li.get("url") or "https://vn.linkedin.com/company/vixex"
    if not li.get("accessible", False):
        return ('<section class="card"><h2>LinkedIn</h2>'
                '<p class="muted">Public page not accessible right now '
                '(HTTP %s). <a href="%s" target="_blank" rel="noopener">open</a></p>'
                '</section>' % (_e(li.get("raw_status", "?")), _e(url)))
    name = li.get("name") or "VIXEX"
    followers = li.get("followers")
    fb = ('<span class="badge vi">%s followers</span>' % _e(followers)) if followers is not None else ""
    tagline = li.get("tagline") or ""
    posts = li.get("posts") or []
    post_html = ""
    if posts:
        lis = "".join('<li><span class="an">%s</span> <span class="am">%s</span></li>'
                      % (_e(p.get("text", "")), _e(p.get("when", ""))) for p in posts[:5])
        post_html = '<ul class="apps">%s</ul>' % lis
    return ('<section class="card"><h2>LinkedIn &mdash; '
            '<a href="%s" target="_blank" rel="noopener">%s</a> %s</h2>'
            '<p class="cardnote">%s &middot; checked %s</p>%s</section>'
            % (_e(url), _e(name), fb, _e(tagline),
               _e(rel_time(li.get("fetched_at", ""), now)), post_html))


def render_securities(sec, now):
    """VIX Securities app watch — incumbent broker; alert if it adds crypto."""
    if not isinstance(sec, dict) or not sec:
        return ('<section class="card"><h2>VIX Securities &mdash; crypto watch</h2>'
                '<p class="muted">No VIX Securities app detected yet.</p></section>')
    rows = []
    for rec in sorted(sec.values(), key=lambda r: not r.get("crypto")):
        store = rec.get("store", "")
        sb = '<span class="badge %s">%s</span>' % (
            "ios" if store.lower() == "ios" else "play", _e(store or "?"))
        name = '<span class="an"><a href="%s" target="_blank" rel="noopener">%s</a></span>' % (
            _e(rec.get("url") or "#"), _e(rec.get("name") or "(unnamed)"))
        dev = '<span class="am">%s</span>' % _e(rec.get("developer")) if rec.get("developer") else ""
        flag = ('<span class="badge new">CRYPTO MENTIONED</span>' if rec.get("crypto")
                else '<span class="badge en">no crypto yet</span>')
        rows.append('<li>%s%s%s%s</li>' % (sb, name, dev, flag))
    return ('<section class="card"><h2>VIX Securities &mdash; crypto watch</h2>'
            '<p class="cardnote">Incumbent broker (separate from Vixex). '
            'Flags if their app listing starts mentioning crypto.</p>'
            '<ul class="apps">%s</ul></section>' % "".join(rows))


_URL_RE = re.compile(r"https?://\S+")


def _linkify(text):
    """Escape text; turn bare URLs into links."""
    out = []
    last = 0
    for m in _URL_RE.finditer(text):
        out.append(_e(text[last:m.start()]))
        u = m.group(0)
        out.append('<a href="%s" target="_blank" rel="noopener">%s</a>'
                    % (_e(u), _e(u)))
        last = m.end()
    out.append(_e(text[last:]))
    return "".join(out)


def render_changelog(entries):
    shown = entries[:CHANGELOG_LIMIT]
    if not shown:
        return ('<section class="card"><h2>Site changes</h2>'
                '<p class="muted">No changelog entries yet.</p></section>')
    blocks = []
    for e in shown:
        parts = ['<div class="chg">']
        parts.append('<div class="ct">%s</div>' % _e(e.get("ts") or ""))
        if e.get("baseline_note"):
            parts.append('<div class="cn">%s</div>' % _e(e["baseline_note"]))
        for sec in e.get("sections", []):
            title = sec.get("title") or ""
            cnt = sec.get("count")
            head = title + ((" (%s)" % cnt) if cnt is not None else "")
            parts.append('<h4>%s</h4>' % _e(head))
            if sec.get("bullets"):
                lis = "".join('<li>%s</li>' % _linkify(b) for b in sec["bullets"])
                parts.append('<ul>%s</ul>' % lis)
        parts.append('</div>')
        blocks.append("".join(parts))
    older = ""
    if len(entries) > CHANGELOG_LIMIT:
        older = ('<p class="muted">+%d older entries.</p>'
                 % (len(entries) - CHANGELOG_LIMIT))
    return ('<section class="card"><h2>Site changes</h2>%s%s</section>'
            % ("".join(blocks), older))


def render_pages(state, wayback):
    pages = state.get("pages") or {}
    wb_pages = (wayback.get("pages") or {}) if isinstance(wayback, dict) else {}
    if not pages:
        return ('<section class="card"><h2>Pages</h2>'
                '<p class="muted">No pages tracked yet.</p></section>')
    rows = []
    for url in sorted(pages.keys()):
        info = pages[url] or {}
        wb = wb_pages.get(url) or {}
        wb_status = wb.get("status") or "—"
        archived = wb.get("archived") or ""
        if wb_status == "OK" and archived:
            snap = archived
        else:
            snap = WB_LATEST + url
        hist = WB_HISTORY + url
        # archive status badge
        if wb_status == "OK":
            sb = '<span class="b ok">OK</span>'
        elif wb_status == "pending":
            sb = '<span class="b warn">pending</span>'
        elif wb_status and wb_status != "—":
            sb = '<span class="b alert">%s</span>' % _e(wb_status)
        else:
            sb = '<span class="b mut">—</span>'
        http = info.get("status")
        http_badge = ""
        if http is not None:
            cls = "ok" if http == 200 else "alert"
            http_badge = '<span class="b %s">%s</span>' % (cls, _e(http))
        rows.append(
            '<tr><td><a href="%s" target="_blank" rel="noopener">%s</a></td>'
            '<td>%s</td>'
            '<td><a href="%s" target="_blank" rel="noopener">snapshot</a> · '
            '<a href="%s" target="_blank" rel="noopener">history</a></td>'
            '<td>%s</td></tr>'
            % (_e(url), _e(url), http_badge, _e(snap), _e(hist), sb))
    return ('<section class="card"><h2>Pages</h2>'
            '<table><thead><tr><th>URL</th><th>HTTP</th>'
            '<th>Wayback</th><th>Archive (today)</th></tr></thead>'
            '<tbody>%s</tbody></table></section>') % "".join(rows)


def render_links(state):
    links = state.get("links") or []
    if not links:
        return ""
    lis = "".join('<li><a href="%s" target="_blank" rel="noopener">%s</a></li>'
                  % (_e(u), _e(u)) for u in links)
    return ('<section class="card"><details><summary>All tracked links (%d)'
            '</summary><ul>%s</ul></details></section>' % (len(links), lis))


def compute_banner(changelog, news, apps, now):
    reasons = []
    # (a) non-baseline changelog entry in last RECENT_DAYS
    site_change = False
    for e in changelog:
        if is_baseline_entry(e):
            continue
        if within_days(e.get("ts"), RECENT_DAYS, now):
            site_change = True
            break
    if site_change:
        reasons.append("site change")
    # (b) news first_seen within window
    n_news = sum(1 for i in news if within_days(i.get("first_seen"), RECENT_DAYS, now))
    if n_news:
        reasons.append("%d new article%s" % (n_news, "" if n_news == 1 else "s"))
    n_apps = sum(1 for a in apps if within_days(a.get("first_seen"), RECENT_DAYS, now))
    if n_apps:
        reasons.append("%d new app%s" % (n_apps, "" if n_apps == 1 else "s"))
    if reasons:
        return ('<div class="banner alert">&#9888; Changes detected &mdash; %s'
                ' (last %d days)</div>'
                % (_e(", ".join(reasons)), RECENT_DAYS))
    return ('<div class="banner ok">&#10003; No recent changes'
            ' (last %d days)</div>' % RECENT_DAYS)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_dashboard():
    os.makedirs(DOCS_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)

    state = _read_json(os.path.join(DATA_DIR, "state.json"), {})
    if not isinstance(state, dict):
        state = {}
    wayback = _read_json(os.path.join(DATA_DIR, "wayback_pending.json"), {})
    if not isinstance(wayback, dict):
        wayback = {}
    news = _read_json(os.path.join(DATA_DIR, "news_latest.json"), [])
    if not isinstance(news, list):
        news = []
    apps = _read_json(os.path.join(DATA_DIR, "apps_latest.json"), [])
    if not isinstance(apps, list):
        apps = []
    linkedin = _read_json(os.path.join(DATA_DIR, "linkedin.json"), {})
    if not isinstance(linkedin, dict):
        linkedin = {}
    securities = _read_json(os.path.join(DATA_DIR, "securities.json"), {})
    if not isinstance(securities, dict):
        securities = {}
    changelog = parse_changelog(_read_text(os.path.join(DATA_DIR, "changelog.md")))

    # Sort news newest-first defensively (file is pre-sorted; keep stable).
    def news_key(i):
        dt = parse_dt(i.get("published")) or parse_dt(i.get("first_seen"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)
    try:
        news = sorted(news, key=news_key, reverse=True)
    except Exception:
        pass

    crawled = state.get("crawled_at") or ""
    sitemap = state.get("sitemap") or {}
    sitemap_present = bool(sitemap.get("present")) if isinstance(sitemap, dict) else False

    li_followers = linkedin.get("followers") if linkedin.get("accessible") else None
    stats_html = "".join([
        stat(len(state.get("pages") or {}), "pages"),
        stat(len(state.get("links") or []), "links"),
        stat(len(news), "news"),
        stat(len(apps), "apps"),
        stat(li_followers if li_followers is not None else "—", "followers"),
        stat("yes" if sitemap_present else "no", "sitemap"),
    ])

    banner = compute_banner(changelog, news, apps, now)

    parts = []
    parts.append('<!doctype html><html lang="en"><head>')
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append('<title>vixx.vn change monitor</title>')
    parts.append('<style>%s</style></head><body><div class="wrap">' % STYLE)
    parts.append('<header class="top"><h1>vixx.vn change monitor</h1>'
                 '<span class="sub">last crawl: %s &middot; built %s</span>'
                 '<a class="sub" href="history.html" style="margin-left:auto">'
                 '&#128200; build-progress chart &rarr;</a></header>'
                 % (_e(crawled or "—"),
                    _e(now.strftime("%Y-%m-%d %H:%M UTC"))))
    parts.append('<div class="stats">%s</div>' % stats_html)
    parts.append(banner)
    parts.append(render_news(news, now))
    parts.append(render_linkedin(linkedin, now))
    parts.append(render_apps(apps, now))
    parts.append(render_securities(securities, now))
    parts.append(render_changelog(changelog))
    parts.append(render_pages(state, wayback))
    parts.append(render_links(state))
    parts.append('<p class="foot">vixx-watch &middot; static monitoring console</p>')
    parts.append('</div></body></html>')

    htmldoc = "".join(parts)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(htmldoc)
    return OUTPUT


if __name__ == "__main__":
    path = build_dashboard()
    print("wrote", path)
