"""Standalone build-progress chart renderer for the vixx.vn website monitor.

Reads DATA_DIR/history.jsonl (a daily time series) and writes a self-contained,
dark-themed docs/history.html containing a pure inline-SVG multi-series line chart.

Stdlib only. Does NOT import vixx_watch or dashboard. Reads history.jsonl
read-only; only writes docs/history.html.
"""

import json
import os
import html
import math
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
OUTPUT_PATH = os.path.join(DOCS_DIR, "history.html")

# Palette (matches dashboard)
BG = "#0f1115"
CARD = "#1a1d24"
MUTED = "#8b93a7"
FG = "#e6e9ef"
ACCENT = "#5b9dff"   # Live pages (primary)
GRID = "#262a33"

# Series definitions: (data key, label, color, is_primary)
SERIES = [
    ("pages", "Live pages", ACCENT, True),
    ("links", "Links", "#2ea043", False),
    ("news_total", "News mentions", "#d29922", False),
    ("apps_total", "Apps", "#f85149", False),
]

# SVG geometry
VB_W = 960
VB_H = 420
M_LEFT = 56
M_RIGHT = 24
M_TOP = 24
M_BOTTOM = 64  # extra room for rotated date labels


def load_history(path=HISTORY_PATH):
    """Read history.jsonl defensively. Return list of dicts sorted by date asc,
    keeping the LAST record per date. Never raises on bad/missing data."""
    if not os.path.exists(path):
        return []
    by_date = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                date = rec.get("date")
                if not isinstance(date, str) or not date:
                    continue
                by_date[date] = rec  # last wins
    except OSError:
        return []
    return [by_date[d] for d in sorted(by_date.keys())]


def _num(rec, key):
    """Coerce a record field to a non-negative number, defaulting to 0."""
    v = rec.get(key, 0)
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(n) or math.isinf(n) or n < 0:
        return 0.0
    return n


def nice_max(raw_max):
    """Round a data maximum up to a clean axis maximum."""
    if raw_max <= 0:
        return 10.0
    exp = math.floor(math.log10(raw_max))
    base = 10 ** exp
    frac = raw_max / base
    if frac <= 1:
        nice = 1
    elif frac <= 2:
        nice = 2
    elif frac <= 2.5:
        nice = 2.5
    elif frac <= 5:
        nice = 5
    else:
        nice = 10
    return nice * base


def _x_for(i, n, plot_x, plot_w):
    """Pixel X for the i-th point of n points."""
    if n <= 1:
        return plot_x + plot_w / 2.0
    return plot_x + plot_w * (i / (n - 1))


def _y_for(value, y_max, plot_y, plot_h):
    """Pixel Y for a value given the axis max (0..y_max)."""
    if y_max <= 0:
        return plot_y + plot_h
    return plot_y + plot_h * (1.0 - value / y_max)


def _fmt_num(v):
    """Format an axis/legend number compactly."""
    if v == int(v):
        return str(int(v))
    return ("%.1f" % v).rstrip("0").rstrip(".")


def build_svg(records):
    """Build the inline <svg> string from a list of (already cleaned) records.

    Records must be sorted by date asc, one per date. Returns SVG markup.
    Handles the single-point case (no polyline). Caller guarantees len >= 1.
    """
    n = len(records)
    dates = [r.get("date", "") for r in records]

    # Extract per-series numeric sequences.
    series_vals = {}
    overall_max = 0.0
    for key, _label, _color, _primary in SERIES:
        vals = [_num(r, key) for r in records]
        series_vals[key] = vals
        if vals:
            overall_max = max(overall_max, max(vals))

    y_max = nice_max(overall_max)

    plot_x = M_LEFT
    plot_y = M_TOP
    plot_w = VB_W - M_LEFT - M_RIGHT
    plot_h = VB_H - M_TOP - M_BOTTOM

    parts = []
    parts.append(
        '<svg viewBox="0 0 %d %d" width="100%%" '
        'preserveAspectRatio="xMidYMid meet" '
        'xmlns="http://www.w3.org/2000/svg" '
        'font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif" '
        'role="img" aria-label="vixx.vn build progress chart">' % (VB_W, VB_H)
    )

    # Horizontal gridlines + Y labels (5 divisions).
    divisions = 5
    for g in range(divisions + 1):
        val = y_max * g / divisions
        gy = _y_for(val, y_max, plot_y, plot_h)
        parts.append(
            '<line x1="%.1f" y1="%.2f" x2="%.1f" y2="%.2f" stroke="%s" stroke-width="1"/>'
            % (plot_x, gy, plot_x + plot_w, gy, GRID)
        )
        parts.append(
            '<text x="%.1f" y="%.2f" fill="%s" font-size="12" '
            'text-anchor="end" dominant-baseline="middle">%s</text>'
            % (plot_x - 8, gy, MUTED, html.escape(_fmt_num(val)))
        )

    # X axis date ticks: ~6-8 evenly spaced, rotated.
    if n == 1:
        tick_idxs = [0]
    else:
        target = min(8, n)
        tick_idxs = sorted(set(
            round(i * (n - 1) / (target - 1)) for i in range(target)
        )) if target > 1 else [0]
    baseline_y = plot_y + plot_h
    for idx in tick_idxs:
        tx = _x_for(idx, n, plot_x, plot_w)
        parts.append(
            '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="1"/>'
            % (tx, baseline_y, tx, baseline_y + 5, GRID)
        )
        label = html.escape(dates[idx])
        parts.append(
            '<text x="%.2f" y="%.2f" fill="%s" font-size="11" '
            'text-anchor="end" transform="rotate(-40 %.2f %.2f)">%s</text>'
            % (tx, baseline_y + 18, MUTED, tx, baseline_y + 18, label)
        )

    # Sitemap presence band: subtle marker at bottom for dates where sitemap is true.
    for i, r in enumerate(records):
        if r.get("sitemap") is True:
            mx = _x_for(i, n, plot_x, plot_w)
            parts.append(
                '<circle cx="%.2f" cy="%.2f" r="2" fill="%s" opacity="0.5"/>'
                % (mx, baseline_y + 30, MUTED)
            )

    # Draw secondary series first, then primary on top.
    ordered = [s for s in SERIES if not s[3]] + [s for s in SERIES if s[3]]
    for key, _label, color, primary in ordered:
        vals = series_vals[key]
        pts = [
            (_x_for(i, n, plot_x, plot_w), _y_for(vals[i], y_max, plot_y, plot_h))
            for i in range(n)
        ]
        sw = 2.8 if primary else 1.6
        if n >= 2:
            poly = " ".join("%.2f,%.2f" % (x, y) for x, y in pts)
            parts.append(
                '<polyline points="%s" fill="none" stroke="%s" '
                'stroke-width="%.1f" stroke-linejoin="round" stroke-linecap="round"/>'
                % (poly, color, sw)
            )
        r_pt = 3.5 if primary else 2.5
        for x, y in pts:
            parts.append(
                '<circle cx="%.2f" cy="%.2f" r="%.1f" fill="%s"/>'
                % (x, y, r_pt, color)
            )

    # Single-point label
    if n == 1:
        x = _x_for(0, n, plot_x, plot_w)
        pv = series_vals["pages"][0]
        y = _y_for(pv, y_max, plot_y, plot_h)
        parts.append(
            '<text x="%.2f" y="%.2f" fill="%s" font-size="12" text-anchor="middle">'
            '%s live pages</text>'
            % (x, y - 10, FG, html.escape(_fmt_num(pv)))
        )

    parts.append("</svg>")
    return "".join(parts)


def _legend_html(records):
    """Legend rows with each series' latest value."""
    last = records[-1] if records else {}
    rows = []
    for key, label, color, primary in SERIES:
        latest = _fmt_num(_num(last, key)) if records else "-"
        weight = "600" if primary else "400"
        rows.append(
            '<span class="leg">'
            '<span class="dot" style="background:%s"></span>'
            '<span class="lbl" style="font-weight:%s">%s</span>'
            '<span class="val">%s</span>'
            '</span>' % (color, weight, html.escape(label), html.escape(latest))
        )
    return "".join(rows)


def _page(body_inner, subtitle):
    """Wrap inner HTML in the full dark-theme page shell."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vixx.vn — build progress</title>
<style>
  :root {{
    --bg:{bg}; --card:{card}; --muted:{muted}; --fg:{fg};
    --accent:{accent}; --grid:{grid};
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--fg);
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    padding:20px;
  }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  a.back {{ color:var(--accent); text-decoration:none; font-size:14px; }}
  a.back:hover {{ text-decoration:underline; }}
  h1 {{ font-size:22px; margin:14px 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
  .card {{
    background:var(--card); border:1px solid var(--grid);
    border-radius:12px; padding:18px;
  }}
  .legend {{
    display:flex; flex-wrap:wrap; gap:16px; margin-top:14px;
    font-size:13px;
  }}
  .leg {{ display:inline-flex; align-items:center; gap:6px; }}
  .dot {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
  .lbl {{ color:var(--fg); }}
  .val {{ color:var(--muted); }}
  .empty {{ color:var(--muted); font-size:15px; line-height:1.6; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="index.html">&larr; Back to dashboard</a>
  <h1>vixx.vn — build progress</h1>
  <div class="sub">{subtitle}</div>
  <div class="card">
    {body}
  </div>
</div>
</body>
</html>
""".format(
        bg=BG, card=CARD, muted=MUTED, fg=FG, accent=ACCENT, grid=GRID,
        subtitle=subtitle, body=body_inner,
    )


def render_html(records):
    """Render full HTML string from cleaned records (any length, incl. 0)."""
    if not records:
        subtitle = "No data yet"
        body = (
            '<p class="empty">No history yet — the chart fills in once the '
            'daily monitor has run a few times.</p>'
        )
        return _page(body, subtitle)

    first_date = html.escape(records[0].get("date", "?"))
    last_date = html.escape(records[-1].get("date", "?"))
    last = records[-1]
    if len(records) == 1:
        range_txt = "Single record for %s" % last_date
    else:
        range_txt = "%s &rarr; %s (%d days)" % (
            first_date, last_date, len(records))
    latest_txt = "Latest: %s live pages, %s links, %s news, %s apps" % (
        _fmt_num(_num(last, "pages")),
        _fmt_num(_num(last, "links")),
        _fmt_num(_num(last, "news_total")),
        _fmt_num(_num(last, "apps_total")),
    )
    subtitle = "%s &middot; %s" % (range_txt, latest_txt)

    svg = build_svg(records)
    legend = _legend_html(records)
    body = '%s\n<div class="legend">%s</div>' % (svg, legend)
    return _page(body, subtitle)


def build_chart():
    """Read history.jsonl and write docs/history.html. Never crashes."""
    records = load_history()
    out = render_html(records)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(out)
    return OUTPUT_PATH, len(records)


if __name__ == "__main__":
    path, count = build_chart()
    print("Wrote %s (%d daily records)" % (path, count))
