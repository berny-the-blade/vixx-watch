"""Full-page PNG screenshots of web pages for forensic evidence.

Uses headless Chromium via Playwright. Designed to be imported by
vixx_watch.py:

    import screenshot
    if screenshot.available():
        shots = screenshot.capture(urls, out_dir)  # {url: png_path}

The target site (vixx.vn) serves an EXPIRED TLS cert, so HTTPS errors are
ignored. The site is a Next.js client-rendered app, so we wait for network
idle plus a short settle delay to let it hydrate before capturing.

If Playwright/Chromium are not installed, available() returns False and
capture() returns {} -- the tracker keeps running without screenshots.
"""

import os
import re
import sys

# Per-URL navigation/render budget (milliseconds).
PER_URL_TIMEOUT_MS = 30_000
# Extra settle time after network idle so the SPA finishes hydrating (ms).
SETTLE_MS = 3_000

VIEWPORT = {"width": 1366, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _sanitize(url):
    """Sanitize a URL into a filename stem (mirrors vixx_watch.snap_name)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")[:150]


def available():
    """True if Playwright is importable AND a Chromium browser can launch.

    Never raises; returns False on any failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def capture(urls, out_dir):
    """Render each URL to a full-page PNG in out_dir.

    Returns {url: png_path} for successes only (failures omitted). Returns {}
    immediately if Playwright/Chromium are unavailable. Launches a single
    browser/context for the whole batch and always closes it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("screenshot: playwright not installed; skipping captures", file=sys.stderr)
        return {}

    os.makedirs(out_dir, exist_ok=True)
    results = {}

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"screenshot: chromium launch failed ({e}); skipping captures", file=sys.stderr)
                return {}

            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=USER_AGENT,
                ignore_https_errors=True,  # vixx.vn has an EXPIRED cert
            )
            try:
                for url in urls:
                    page = context.new_page()
                    page.set_default_timeout(PER_URL_TIMEOUT_MS)
                    try:
                        page.goto(url, wait_until="networkidle", timeout=PER_URL_TIMEOUT_MS)
                        # Let the Next.js client app finish hydrating/rendering.
                        page.wait_for_timeout(SETTLE_MS)
                        png_path = os.path.join(out_dir, _sanitize(url) + ".png")
                        page.screenshot(path=png_path, full_page=True)
                        results[url] = png_path
                    except Exception as e:
                        print(f"screenshot: failed {url}: {e}", file=sys.stderr)
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
            finally:
                context.close()
                browser.close()
    except Exception as e:
        print(f"screenshot: batch error ({e})", file=sys.stderr)

    return results


if __name__ == "__main__":
    test_url = "https://vixx.vn/vi"
    out = os.path.join(".", "_shottest")
    print(f"available() -> {available()}")
    res = capture([test_url], out)
    print(f"capture() -> {res}")
    for url, path in res.items():
        size = os.path.getsize(path)
        print(f"  {url}\n    {path}\n    {size} bytes ({size / 1024:.1f} KB)")
    if not res:
        print("No screenshots produced.")
