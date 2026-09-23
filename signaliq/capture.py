"""Capture engines: browser (Playwright) with login + HTTP replay (stdlib)."""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


def replay_request(url: str, method: str = "GET",
                   headers: Optional[dict] = None, body: str = "",
                   timeout: float = 15.0) -> dict:
    """Send a single HTTP request and return status/headers/body."""
    req = urllib.request.Request(
        url, method=method.upper(),
        data=body.encode("utf-8") if body else None,
        headers=headers or {"User-Agent": "SignalIQ-Replay/1.0"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "url": url, "method": method.upper(), "status": resp.status,
                "time_ms": round((time.time() - started) * 1000, 1),
                "response_headers": dict(resp.headers.items()),
                "response_body": resp.read(1_000_000).decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "error": str(e),
                "response_body": e.read(100_000).decode("utf-8", errors="replace")}
    except Exception as e:
        return {"url": url, "status": None, "error": str(e)}


_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_CONSENT_BUTTON_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "button[aria-label='Accept All Cookies']",
    "button[aria-label='Accept all']",
    "button[aria-label='Accept All']",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept All')",
    "button:has-text('Accept all')",
    "button:has-text('Accept Cookies')",
    "button:has-text('I Accept')",
    "button:has-text('Agree')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('Got it')",
    "#didomi-notice-agree-button",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    ".qc-cmp2-summary-buttons button[mode='primary']",
]


def _accept_consent_banners(page, timeout_ms: int = 4000) -> bool:
    """Best-effort click of a cookie/consent 'Accept all' button, checking
    the top frame and any CMP iframes. Many sites (OneTrust, Didomi,
    Cookiebot, Quantcast) do not fire ad-server requests - including GAM -
    until a consent choice is recorded, so skipping this step silently
    suppresses most Display/Header Bidding traffic even though the page
    "worked" and returned 200s for everything else.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    frames_to_try = [page] + list(page.frames)
    while time.time() < deadline:
        for frame in frames_to_try:
            for sel in _CONSENT_BUTTON_SELECTORS:
                try:
                    el = frame.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=1500)
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(300)
        frames_to_try = [page] + list(page.frames)
    return False


def _detect_exit_geo(context) -> Optional[dict]:
    """Confirm what country/IP this capture is actually exiting from -
    verifies a VPN/proxy is really routing traffic where you think it is,
    rather than silently falling back to your real location. Country-
    agnostic: works for any target country, not just the US. Best-effort -
    returns None if the lookup itself is blocked/unreachable (which can
    happen on some restrictive exit networks).
    """
    for lookup_url in ("https://ipapi.co/json/", "https://ip-api.com/json/"):
        try:
            resp = context.request.get(lookup_url, timeout=8000)
            if resp.ok:
                data = resp.json()
                return {
                    "ip": data.get("ip") or data.get("query", ""),
                    "country": data.get("country_name") or data.get("country", ""),
                    "country_code": data.get("country_code") or data.get("countryCode", ""),
                    "region": data.get("region") or data.get("regionName", ""),
                    "city": data.get("city", ""),
                }
        except Exception:
            continue
    return None


def run_browser_capture(url: str, username: str = "", password: str = "",
                        duration_s: float = 45.0,
                        out_path: str = "capture.json",
                        headless: bool = True,
                        proxy_server: str = "",
                        proxy_username: str = "",
                        proxy_password: str = "") -> dict:
    """Open a web app, log in if credentials given, simulate playback, and
    record all network traffic. Returns capture stats + output path.

    proxy_server (optional): route the browser's traffic through a proxy,
    e.g. "http://us.proxyprovider.com:8080" - needed if you want to see
    geo-targeted (e.g. US-only) ad demand while running this from outside
    that region. Ad servers geo-target off the request's real IP, which
    Playwright/Chromium cannot spoof on its own - a proxy with an exit IP
    in the target country is the only way around that short of a VPN.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && "
            "playwright install chromium") from exc

    captured = []
    login_status = "not_attempted"
    consent_accepted = False

    proxy_config = None
    if proxy_server:
        proxy_config = {"server": proxy_server}
        if proxy_username:
            proxy_config["username"] = proxy_username
        if proxy_password:
            proxy_config["password"] = proxy_password

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            proxy=proxy_config,
        )
        context = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1366, "height": 900},
            user_agent=_DESKTOP_UA,
            locale="en-US",
        )
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        exit_geo = _detect_exit_geo(context)
        page = context.new_page()

        def on_response(response):
            req = response.request
            entry = {
                "url": req.url, "method": req.method, "status": response.status,
                "request_headers": dict(req.headers),
                "response_headers": dict(response.headers),
                "request_body": req.post_data or "", "timestamp": time.time(),
            }
            ct = (response.headers.get("content-type") or "").lower()
            if any(t in ct for t in ("xml", "json", "text")):
                try:
                    entry["response_body"] = response.text()[:2_000_000]
                except Exception:
                    entry["response_body"] = ""
            captured.append(entry)

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)
        consent_accepted = _accept_consent_banners(page)
        page.wait_for_timeout(1500)

        if username and password:
            login_status = "failed"
            user_selectors = [
                "input[type='email']", "input[name='email']",
                "input[name='username']", "input[id*='email' i]",
                "input[id*='user' i]", "input[autocomplete='username']",
            ]
            pass_selectors = ["input[type='password']"]
            submit_selectors = [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Sign in')", "button:has-text('Log in')",
                "button:has-text('Login')", "button:has-text('Continue')",
            ]
            try:
                if not any(page.query_selector(s) for s in pass_selectors):
                    for opener in ("a:has-text('Sign in')", "a:has-text('Log in')",
                                   "button:has-text('Sign in')",
                                   "button:has-text('Log in')"):
                        el = page.query_selector(opener)
                        if el:
                            el.click()
                            page.wait_for_timeout(2000)
                            break
                for s in user_selectors:
                    el = page.query_selector(s)
                    if el:
                        el.fill(username)
                        break
                if not any(page.query_selector(s) for s in pass_selectors):
                    for s in submit_selectors:
                        el = page.query_selector(s)
                        if el:
                            el.click()
                            page.wait_for_timeout(2500)
                            break
                for s in pass_selectors:
                    el = page.query_selector(s)
                    if el:
                        el.fill(password)
                        break
                for s in submit_selectors:
                    el = page.query_selector(s)
                    if el:
                        el.click()
                        break
                page.wait_for_timeout(5000)
                if not page.query_selector("input[type='password']"):
                    login_status = "success"
            except Exception:
                login_status = "failed"

        for sel in ("video", "button[aria-label*='play' i]",
                    "button[class*='play' i]", ".play-button",
                    "[data-testid*='play' i]"):
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=3000)
                    break
            except Exception:
                continue
        try:
            page.evaluate("document.querySelectorAll('video')"
                          ".forEach(v => v.play().catch(()=>{}))")
        except Exception:
            pass

        _scroll_through_page(page, duration_s)
        browser.close()

    Path(out_path).write_text(json.dumps(captured, indent=2), encoding="utf-8")
    return {"out_path": out_path, "requests_captured": len(captured),
            "login_status": login_status, "duration_s": duration_s,
            "consent_accepted": consent_accepted, "proxy_used": bool(proxy_server),
            "exit_geo": exit_geo}


def _scroll_through_page(page, duration_s: float) -> None:
    """Scroll down the page over the capture window instead of sitting still.

    Most display ad slots (GAM lazy-loaded units, infinite-scroll placements,
    sticky/anchor units that only render near-viewport) never fire a request
    if the page never scrolls. This walks the page in steps for most of the
    window, then scrolls back toward the top so above-the-fold refreshes
    (common on GAM slot refresh) get a chance to fire too. Video playback
    (started by the caller before this runs) continues throughout.
    """
    try:
        total_height = page.evaluate("document.body.scrollHeight") or 0
    except Exception:
        total_height = 0

    if total_height <= 0:
        page.wait_for_timeout(int(duration_s * 1000))
        return

    steps = max(4, min(12, int(duration_s // 3)))
    scroll_budget_ms = int(duration_s * 1000 * 0.7)
    step_ms = max(400, scroll_budget_ms // steps)

    for i in range(1, steps + 1):
        target = int(total_height * (i / steps))
        try:
            page.evaluate(f"window.scrollTo({{top: {target}, behavior: 'smooth'}})")
        except Exception:
            pass
        page.wait_for_timeout(step_ms)

    remaining_ms = int(duration_s * 1000) - (steps * step_ms)
    if remaining_ms > 0:
        try:
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        except Exception:
            pass
        page.wait_for_timeout(remaining_ms)