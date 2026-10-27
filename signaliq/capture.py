"""Capture engines: browser (Playwright) with login + HTTP replay (stdlib)."""

from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .vendors import match_vendor


def _take_screenshot(page, quality: int = 35) -> Optional[str]:
    """Best-effort JPEG screenshot of what the headless browser is actually
    rendering right now, as a data: URI ready for an <img src>. This is the
    only honest way to show "what is the automation seeing" - it captures
    the real automated browser's view (including a CAPTCHA/bot-check page,
    if that's what actually loaded), not a fresh load in the viewer's own
    browser on their own network, which would show something completely
    different and misleading. Low JPEG quality keeps the payload small
    since this gets polled repeatedly during a capture.
    """
    try:
        raw = page.screenshot(type="jpeg", quality=quality, timeout=5000)
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


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


_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.href,
  text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 120),
  inMain: !!a.closest('main, article, [role=main]'),
}))
"""

# Pages that are unsafe to auto-visit (logout/delete style links) or that
# almost never carry ads (legal/about/contact), plus non-page file types.
_SKIP_PATH = re.compile(
    r"(log-?out|sign-?out|log-?in|sign-?in|sign-?up|register|account|profile|"
    r"cart|checkout|subscribe|unsubscribe|delete|remove|admin|privacy|terms|"
    r"cookie|contact|about|career|advertis|donate|newsletter|rss|feed|search|"
    r"comment|share|print)", re.I)
_SKIP_EXT = re.compile(
    r"\.(pdf|zip|jpe?g|png|gif|svg|webp|mp4|mp3|webm|xml|rss|json|docx?|"
    r"xlsx?|pptx?|exe|dmg|apk|txt)$", re.I)


def _collect_links(page) -> list:
    """Same-site links worth considering, already filtered for safety (no
    logout/delete/account/legal/file links, no other sites). Each is a dict:
    url, norm (path without trailing slash), text, text_len, in_main,
    article (looks like a deep content page rather than a section)."""
    try:
        raw = page.evaluate(_LINKS_JS) or []
        current = urlparse(page.url)
    except Exception:
        return []

    def host_key(h: str) -> str:
        h = h.lower().split(":")[0]
        return h[4:] if h.startswith("www.") else h

    base = host_key(current.netloc)
    cur_path = current.path.rstrip("/") or "/"
    seen: set = set()
    out: list = []
    for a in raw:
        try:
            u = urlparse(a.get("href", ""))
        except Exception:
            continue
        if u.scheme not in ("http", "https"):
            continue
        h = host_key(u.netloc)
        if not (h == base or h.endswith("." + base)):
            continue
        path = u.path or "/"
        norm = path.rstrip("/") or "/"
        if norm == "/" or norm == cur_path:
            continue
        if _SKIP_PATH.search(path) or _SKIP_EXT.search(path):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        clean = f"{u.scheme}://{u.netloc}{path}"
        if u.query and len(u.query) < 80:
            clean += f"?{u.query}"
        segments = [x for x in norm.split("/") if x]
        last = segments[-1] if segments else ""
        text = a.get("text", "") or ""
        out.append({"url": clean, "norm": norm, "path": path, "text": text, "text_len": len(text),
                    "in_main": bool(a.get("inMain")),
                    "article": len(segments) >= 2 or ("-" in last and len(last) > 18)})
    return out


def _pick_internal_links(page, n: int) -> list:
    """Pick up to n internal pages worth visiting after the homepage: one
    section page (like /sports) plus article-style pages, since those are
    laid out and monetized differently from a homepage. Pages are only ever
    navigated to directly (never clicked), so nothing on the site is triggered.
    """
    if n <= 0:
        return []
    links = _collect_links(page)
    sections = [(l["url"], l["text_len"], l["in_main"]) for l in links if not l["article"]]
    articles = [(l["url"], l["text_len"], l["in_main"]) for l in links if l["article"]]
    # Headline-style links (long text, inside main content) first.
    articles.sort(key=lambda x: (x[2], x[1]), reverse=True)
    pool: list = []
    if n >= 2 and sections:
        pool.append(sections[0])
    pool.extend(articles)
    pool.extend(sections)
    out: list = []
    for item in pool:
        if item[0] not in out:
            out.append(item[0])
        if len(out) >= n:
            break
    return out


_VIDEO_STRONG_RE = re.compile(r"/(watch|videos?|episodes?|play)(/|-|_|\d|$)", re.I)
_VIDEO_PATH_RE = re.compile(
    r"/(watch|videos?|episodes?|ep|play|player|stream|movies?|films?|series|shows?|tv|clips?|vod|live|"
    r"programs?|trailers?)(/|-|_|\d|$)", re.I)
_VIDEO_TEXT_RE = re.compile(r"(watch|play|episode|\bep\.?\s*\d+|trailer|stream)", re.I)


def _pick_video_links(page, n: int, exclude=()) -> list:
    """Links that look like they lead to something you can watch (an episode,
    a video, a watch page), best guesses first. Streaming sites usually go
    home -> show page -> episode page, so this is used up to two hops deep."""
    scored = []
    for l in _collect_links(page):
        if l["url"] in exclude:
            continue
        score = 0
        if _VIDEO_STRONG_RE.search(l["path"]):
            score += 6
        elif _VIDEO_PATH_RE.search(l["path"]):
            score += 3
        if _VIDEO_TEXT_RE.search(l["text"]):
            score += 2
        if score <= 0:
            continue
        if l["in_main"]:
            score += 1
        scored.append((score, l["url"]))
    scored.sort(key=lambda x: -x[0])
    out: list = []
    for _, url in scored:
        if url not in out:
            out.append(url)
        if len(out) >= n:
            break
    return out


_AD_SCAN_JS = r"""
() => {
  const out = {gpt: [], iframes: [], containers: 0};
  try {
    if (window.googletag && googletag.pubads) {
      out.gpt = googletag.pubads().getSlots().map(s => {
        let sizes = [];
        try {
          sizes = (s.getSizes() || []).map(z => typeof z === 'string' ? z :
                  (z && z.getWidth ? z.getWidth() + 'x' + z.getHeight() : String(z)));
        } catch (e) {}
        return {id: s.getSlotElementId(), adUnit: s.getAdUnitPath(), sizes};
      });
    }
  } catch (e) {}
  const adish = /(google_ads|doubleclick|googlesyndication|2mdn|adnxs|criteo|taboola|outbrain|adform|pubmatic|rubicon|smartadserver|amazon-adsystem|adsafeprotected|clmbtech|\/ads?\/|adserver|banner|sponsor|advert|gpt)/i;
  out.iframes = Array.from(document.querySelectorAll('iframe'))
    .map(f => ({src: f.getAttribute('src') || '', id: f.id || '', w: f.offsetWidth, h: f.offsetHeight}))
    .filter(f => f.w > 1 && f.h > 1 && adish.test(f.src + ' ' + f.id))
    .slice(0, 60);
  out.containers = document.querySelectorAll(
    'ins.adsbygoogle, [id^="div-gpt-ad"], [id*="google_ads"], [data-ad-slot], ' +
    '[data-google-query-id], [class*="adslot"], [class*="ad-slot"], [class*="advert"]'
  ).length;
  return out;
}
"""


def _scan_ad_slots(page, page_url: str) -> Optional[dict]:
    """Look at the page itself (not the network) for ad slots: Google
    Publisher Tag slots, ad iframes and ad containers. This catches ads
    served by networks the vendor list doesn't know, because it only needs
    the page to contain an ad slot, not for us to recognise who filled it."""
    try:
        raw = page.evaluate(_AD_SCAN_JS) or {}
    except Exception:
        return None

    def host_of(u: str) -> str:
        try:
            return urlparse(u).netloc or ""
        except Exception:
            return ""

    return {
        "page": page_url,
        "gpt_slots": (raw.get("gpt") or [])[:60],
        "ad_iframes": [
            {"host": host_of(f.get("src", "")) or "(inline)", "id": f.get("id", ""),
             "size": f"{f.get('w', 0)}x{f.get('h', 0)}"}
            for f in (raw.get("iframes") or [])
        ],
        "containers": int(raw.get("containers") or 0),
    }


# Lists every <video>, including ones inside open shadow roots (many modern
# players are custom elements). All the snippets below use the same list and
# the same order, so an index means the same video everywhere.
_VIDS_EXPR = ("(() => { const out = []; const walk = (root) => { try { "
              "root.querySelectorAll('video').forEach(v => out.push(v)); "
              "root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); }); "
              "} catch (e) {} }; walk(document); return out; })()")

_VIDEOS_JS = r"""
() => __VIDS__.map((v, i) => {
  const r = v.getBoundingClientRect();
  return {
    i, w: Math.round(r.width), h: Math.round(r.height),
    dur: isNaN(v.duration) ? null : (isFinite(v.duration) ? v.duration : -1),
    paused: v.paused, t: v.currentTime, src: v.currentSrc || v.src || '',
    err: v.error ? v.error.code : null,
    ad: !!v.closest('[class*="ima-ad"], [class*="ad-container"], [id*="ad-container"], [class*="adContainer"], [class*="ad-playing"]'),
    visible: r.width > 120 && r.height > 70 && r.bottom > -2000 && r.top < window.innerHeight + 4000,
  };
})
"""
_PLAY_JS = r"""
(i) => {
  const v = __VIDS__[i]; if (!v) return null;
  try { v.scrollIntoView({block: 'center'}); } catch (e) {}
  v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {});
  return {dur: isFinite(v.duration) ? v.duration : null, t: v.currentTime, paused: v.paused};
}
"""
_SEEK_JS = r"""
([i, frac]) => {
  const v = __VIDS__[i]; if (!v) return null;
  if (!isFinite(v.duration) || v.duration <= 0) return {ok: false};
  const target = Math.max(0, Math.min(v.duration - 1.5, v.duration * frac));
  v.muted = true; v.currentTime = target; const p = v.play(); if (p && p.catch) p.catch(() => {});
  return {ok: true, t: target, dur: v.duration};
}
"""
_STATE_JS = r"""
(i) => {
  const v = __VIDS__[i]; if (!v) return null;
  return {t: v.currentTime, dur: isNaN(v.duration) ? null : (isFinite(v.duration) ? v.duration : -1),
          paused: v.paused, ended: v.ended, ready: v.readyState};
}
"""
_ALL_TIMES_JS = r"""
() => __VIDS__.map(v => ({t: v.currentTime, paused: v.paused}))
"""
_VIDEOS_JS = _VIDEOS_JS.replace("__VIDS__", _VIDS_EXPR)
_PLAY_JS = _PLAY_JS.replace("__VIDS__", _VIDS_EXPR)
_SEEK_JS = _SEEK_JS.replace("__VIDS__", _VIDS_EXPR)
_STATE_JS = _STATE_JS.replace("__VIDS__", _VIDS_EXPR)
_ALL_TIMES_JS = _ALL_TIMES_JS.replace("__VIDS__", _VIDS_EXPR)

# Records every DRM key-system request the page makes and whether this
# browser could satisfy it - the usual silent reason protected streaming
# video never starts.
_EME_SPY_SCRIPT = """
(() => { try {
  const orig = navigator.requestMediaKeySystemAccess;
  if (!orig) return;
  window.__siq_eme = [];
  navigator.requestMediaKeySystemAccess = function(ks) {
    const rec = {keySystem: String(ks), ok: null};
    window.__siq_eme.push(rec);
    const pr = orig.apply(this, arguguments);
    pr.then(() => { rec.ok = true; }, (e) => { rec.ok = false; rec.err = String((e && e.message) || e).slice(0, 120); });
    return pr;
  };
} catch (e) {} })();
""".replace("arguguments", "arguments")

# Buttons that start a video player. Tried in every frame of the page,
# because players very often live inside an iframe.
_PLAY_SELECTORS = [
    ".vjs-big-play-button", ".jw-icon-display", ".jw-display-icon-container",
    ".plyr__control--overlaid", ".ytp-large-play-button", ".bmpui-ui-hugeplaybacktogglebutton",
    ".shaka-play-button", ".mejs__overlay-play", ".fp-play", ".flowplayer .fp-ui",
    "button[aria-label*='play' i]", "[role='button'][aria-label*='play' i]",
    "button[title*='play' i]", "[data-testid*='play' i]", "[class*='play-button' i]",
    "[class*='playButton' i]", "[class*='big-play' i]", "[class*='start-play' i]",
    "button:has-text('Play')", "button:has-text('Watch')",
]


def _list_videos(page) -> list:
    """Every <video> on the page, in every frame (players are often iframes)."""
    out: list = []
    for fr in page.frames:
        try:
            vids = fr.evaluate(_VIDEOS_JS) or []
        except Exception:
            continue
        for v in vids:
            v["frame"] = fr
            out.append(v)
    return out


def _pick_main_video(vids: list, allow_ad: bool = False):
    cands = [v for v in vids if v.get("visible") and (allow_ad or not v.get("ad"))]
    return max(cands, key=lambda v: v["w"] * v["h"]) if cands else None


def _qualifies(v) -> bool:
    """Real content - not a short looping hero/background clip."""
    if not v or v["w"] < 200 or v["h"] < 110:
        return False
    d = v.get("dur")
    return d is None or d < 0 or d >= 75


def _any_playing(page, seconds: float = 1.8) -> bool:
    """Is any video on the page - content OR an ad - actually advancing?"""
    def snap():
        out = []
        for fr in page.frames:
            try:
                out.append(fr.evaluate(_ALL_TIMES_JS) or [])
            except Exception:
                out.append([])
        return out
    a = snap()
    page.wait_for_timeout(int(seconds * 1000))
    b = snap()
    for fa, fb in zip(a, b):
        for va, vb in zip(fa, fb):
            if vb["t"] - va["t"] > 0.3:
                return True
    return False


def _click_play_controls(page) -> bool:
    for fr in page.frames:
        for sel in _PLAY_SELECTORS:
            try:
                el = fr.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=2000)
                    return True
            except Exception:
                continue
    return _click_play_by_name(page)


def _click_video_center(main) -> bool:
    try:
        handle = main["frame"].evaluate_handle("(i) => " + _VIDS_EXPR + "[i]", main["i"])
        el = handle.as_element()
        if el is None:
            return False
        el.click(timeout=2000, force=True)
        return True
    except Exception:
        return False


_VIDEO_ERRORS = {1: "playback was aborted", 2: "a network error", 3: "the video could not be decoded",
                 4: "the source is not supported (often DRM-protected or an unsupported format)"}


def _why_not_playing(page, main) -> str:
    """The most useful explanation we can give for a video that never started."""
    parts = []
    recs = []
    for fr in page.frames:
        try:
            recs += fr.evaluate("() => (window.__siq_eme || [])") or []
        except Exception:
            pass
    failed = [r for r in recs if r.get("ok") is False]
    if failed:
        parts.append(f"the site asked for DRM ({failed[0].get('keySystem')}) and this browser could not provide it "
                     "- try 'Use my installed Google Chrome'")
    elif recs:
        parts.append("the site uses DRM")
    if main and main.get("err"):
        parts.append(f"the video reported an error: {_VIDEO_ERRORS.get(main['err'], 'code ' + str(main['err']))}")
    return "; ".join(parts)


def _start_playback(page, p):
    """Try hard to get a video playing on the current page.

    Returns (main_video, playing, reason). Plays muted, clicks play buttons
    in any frame, and finally clicks the video itself - but only while
    nothing is playing, because clicking a playing player would pause it.
    """
    reason = "no video player was found on this page"
    playing = False
    main = None
    for attempt in range(3):
        if attempt == 0:
            _dismiss_overlays(page)
        main = _pick_main_video(_list_videos(page))
        if main:
            reason = ("a video player was found but the video never started (it may need a login, "
                      "a click on something this tool couldn't find, or use DRM / a format this browser can't play)")
            try:
                main["frame"].evaluate(_PLAY_JS, main["i"])
            except Exception:
                pass
        if _any_playing(page, 2.0):
            playing = True
            break
        if attempt == 0:
            p("Looking for a play button...")
        clicked = _click_play_controls(page)
        if not clicked and main:
            clicked = _click_video_center(main)
        if not clicked and attempt >= 1:
            clicked = _click_player_container(page)    # last resort: the middle of a 16:9 player-looking box
        if not clicked and attempt >= 1 and main is None:
            break          # nothing on this page looks like a player - don't keep trying
        page.wait_for_timeout(2500)
    if not playing:
        page.wait_for_timeout(2000)
        playing = _any_playing(page, 2.0)
    page.wait_for_timeout(1200)  # let the duration load
    vids = _list_videos(page)
    main = _pick_main_video(vids) or main
    if main is None and playing:
        main = _pick_main_video(vids, allow_ad=True)  # only an ad player is visible
    if not playing:
        extra = _why_not_playing(page, main)
        if extra:
            reason = reason + ". Likely cause: " + extra
    return main, playing, ("" if playing else reason)


def _video_state(frame, idx):
    try:
        return frame.evaluate(_STATE_JS, idx)
    except Exception:
        return None


def _content_time_at(samples: list, t: float):
    last = None
    for sm in samples:
        if sm["t"] <= t:
            last = sm.get("content_time_s")
    return last


def _watch_video_ads(page, budget_s: float, p, phases: list, samples: list,
                     page_url: str, main: dict, ad_seen, start_t: Optional[float] = None) -> dict:
    """Watch the page's video the way an audience would, and record enough to
    say afterwards which ad belongs to which break.

    * Plays it muted and watches for at least 20 seconds (a pre-roll can
      delay the real content, so it waits until the content has started).
    * Then jumps to 25%, 50%, 75% and the end. After each jump it waits for
      an ad to be requested (up to the time available), and once one is, lets
      it play before moving on - instead of a fixed timer.
    * Samples the video's position about once a second, so every ad request
      can be tied to "the video was at 12:04 when this fired".

    ad_seen(t0) -> timestamp of the first video-ad request after t0, or None.
    """
    frame, idx = main["frame"], main["i"]
    t_begin = time.time()
    deadline = t_begin + float(budget_s)
    last = {"dur": None}
    ticks = [0]

    def tick(message):
        st = _video_state(frame, idx)
        if st:
            d = st.get("dur")
            dur = d if (d is not None and d > 0) else None
            if dur:
                last["dur"] = dur
            samples.append({"t": time.time(), "page": page_url, "content_time_s": st.get("t"),
                            "duration_s": dur, "paused": st.get("paused"), "ended": st.get("ended")})
        ticks[0] += 1
        if ticks[0] % 2 == 0:
            p(message, {"screenshot": _take_screenshot(page)})
        else:
            p(message)
        return st

    def watch_for(seconds: float, message: str, stop_when=None):
        end = min(time.time() + seconds, deadline)
        while time.time() < end:
            page.wait_for_timeout(1000)
            st = tick(message)
            if stop_when is not None and stop_when(st):
                break

    result = {"found": True, "page": page_url, "duration_s": None, "jumps": [],
              "src": (main.get("src") or "")[:200]}

    p("Video playing - watching for a pre-roll ad...", {"screenshot": _take_screenshot(page)})
    if not main.get("ad"):
        try:
            frame.evaluate(_PLAY_JS, idx)
        except Exception:
            pass
    # "start" is when the capture began trying to play this video (that is
    # when a pre-roll can begin to be requested), not when this loop began.
    phases.append({"phase": "start", "t": start_t or time.time(), "page": page_url,
                   "content_time_s": 0, "duration_s": None})

    def started(st):
        return bool(st) and (st.get("t") or 0) >= 5 and (time.time() - t_begin) >= 20

    watch_for(min(45.0, max(20.0, budget_s * 0.35)), "Watching for a pre-roll ad...", started)

    if main.get("ad"):
        # Only an ad player was visible, so there is no content video to jump around in.
        watch_for(max(0.0, deadline - time.time()), "Watching the video...")
        result["watched_s"] = round(time.time() - t_begin)
        return result

    st = _video_state(frame, idx)
    dur = last["dur"] or ((st or {}).get("dur") if (st or {}).get("dur") and st["dur"] > 0 else None)
    result["duration_s"] = dur

    if dur and dur >= 40:
        # Plan the jumps around the time that is actually left, so a small
        # budget still covers pre, mid AND post-roll instead of running out
        # before the end of the video is reached (~8s is the least a break needs).
        n_slots = int((deadline - time.time()) // 8)
        if n_slots >= 4:
            plan = [25, 50, 75, 100]
        elif n_slots == 3:
            plan = [30, 60, 100]
        elif n_slots == 2:
            plan = [50, 100]
        elif n_slots == 1:
            plan = [100]
        else:
            plan = []
        for k, pct in enumerate(plan):
            left = deadline - time.time()
            if left < 6:
                break
            is_end = pct >= 100
            allot = min(40.0, max(8.0, left / (len(plan) - k)))
            if is_end:
                p("Jumping to the end of the video to trigger a post-roll ad...", {"screenshot": _take_screenshot(page)})
            else:
                p(f"Jumping to {pct}% of the video to trigger a mid-roll ad...", {"screenshot": _take_screenshot(page)})
            try:
                r = frame.evaluate(_SEEK_JS, [idx, 0.999 if is_end else pct / 100.0]) or {}
            except Exception:
                r = {}
            t_seek = time.time()
            phases.append({"phase": "end" if is_end else f"seek_{pct}", "t": t_seek, "page": page_url,
                           "content_time_s": r.get("t"), "duration_s": dur})
            result["jumps"].append(pct)
            min_wait = 8 if is_end else 6

            def stop(st, t_seek=t_seek, min_wait=min_wait):
                if time.time() - t_seek < min_wait:
                    return False
                seen = ad_seen(t_seek)
                if seen is None:
                    return True            # nothing was requested - move on
                base = _content_time_at(samples, seen)
                resumed = st is not None and base is not None and (st.get("t") or 0) >= base + 2.5
                return resumed or (time.time() - seen >= 25)   # ad finished, or let it play ~25s

            watch_for(allot, "Watching for a post-roll ad..." if is_end else f"Watching for a mid-roll at {pct}%...", stop)
    else:
        remaining = max(0.0, deadline - time.time())
        if remaining:
            watch_for(remaining, "Watching the video...")
        p("The video is live or too short to jump around in, so it was just watched while playing.")
    result["watched_s"] = round(time.time() - t_begin)
    return result


# ---------------------------------------------------------------------------
# Auction spy: records, from inside the page, what header bidding (Prebid) and
# Google Ad Manager (GPT) actually did - every auction, every bidder's answer,
# who won, and every slot render/refresh. This is exact (it is the page's own
# event stream), unlike guessing from network traffic. It is installed before
# any page script runs, and its records are collected before the browser
# navigates away (they live in page memory).
# ---------------------------------------------------------------------------
_AUCTION_SPY_SCRIPT = r"""
(() => {
  if (window.__siq_bids) return;
  const store = window.__siq_bids = [];
  const MAX = 6000;
  const push = (o) => { try { if (!o.t) o.t = Date.now(); if (store.length < MAX) store.push(o); } catch (e) {} };
  const num = (x) => { if (x === null || x === undefined || x === '') return null; const n = typeof x === 'number' ? x : parseFloat(x); return isFinite(n) ? n : null; };
  const sizeOf = (b) => (b && (b.size || ((b.width && b.height) ? b.width + 'x' + b.height : null))) || null;

  // ---------------- Prebid ----------------
  const compact = (type, a, t) => {
    try {
      if (type === 'auctionInit' || type === 'auctionEnd') {
        return {src: 'pb', e: type, t, auctionId: a.auctionId,
                adUnitCodes: a.adUnitCodes || (a.adUnits || []).map(u => u.code), timeout: a.timeout || null,
                bidsReceived: (a.bidsReceived || []).length, noBids: (a.noBids || []).length};
      }
      if (type === 'bidRequested') {
        return {src: 'pb', e: type, t, auctionId: a.auctionId, bidder: a.bidderCode || a.bidder,
                requestId: a.bidderRequestId || null, units: (a.bids || []).map(b => b.adUnitCode)};
      }
      if (type === 'bidResponse' || type === 'bidWon') {
        return {src: 'pb', e: type, t, auctionId: a.auctionId, adUnitCode: a.adUnitCode, bidder: a.bidderCode || a.bidder,
                cpm: num(a.cpm), originalCpm: num(a.originalCpm), currency: a.currency || null, size: sizeOf(a),
                adId: a.adId || null, creativeId: a.creativeId || null, dealId: a.dealId || null,
                netRevenue: a.netRevenue === undefined ? null : !!a.netRevenue, ttr: num(a.timeToRespond),
                mediaType: a.mediaType || null,
                advertiser: (a.meta && a.meta.advertiserDomains && a.meta.advertiserDomains[0]) || null};
      }
      if (type === 'noBid') {
        return {src: 'pb', e: type, t, auctionId: a.auctionId, adUnitCode: a.adUnitCode, bidder: a.bidder || a.bidderCode};
      }
      if (type === 'bidTimeout') {
        return (Array.isArray(a) ? a : [a]).map(b => ({src: 'pb', e: type, t, auctionId: b.auctionId, adUnitCode: b.adUnitCode, bidder: b.bidder || b.bidderCode}));
      }
      if (type === 'bidRejected') {
        return {src: 'pb', e: type, t, auctionId: a.auctionId, adUnitCode: a.adUnitCode, bidder: a.bidder || a.bidderCode,
                reason: a.rejectionReason !== undefined ? String(a.rejectionReason) : null};
      }
      if (type === 'setTargeting') {
        return {src: 'pb', e: type, t, targeting: a};
      }
    } catch (e) {}
    return null;
  };
  const emit = (rec, name, past) => {
    if (!rec) return;
    (Array.isArray(rec) ? rec : [rec]).forEach(r => { r.pb = name; if (past) r.past = 1; push(r); });
  };
  const TYPES = ['auctionInit', 'auctionEnd', 'bidRequested', 'bidResponse', 'noBid', 'bidTimeout', 'bidWon', 'bidRejected', 'setTargeting'];
  const hooked = {};
  const hookPrebid = (name) => {
    try {
      const pb = window[name];
      if (!pb || hooked[name] || typeof pb.onEvent !== 'function') return;
      hooked[name] = true;
      try {   // events that happened before we attached
        const past = typeof pb.getEvents === 'function' ? (pb.getEvents() || []) : [];
        const origin = performance.timeOrigin || 0;
        past.forEach(ev => emit(compact(ev.eventType, ev.args || {}, origin && ev.elapsedTime ? Math.round(origin + ev.elapsedTime) : Date.now()), name, true));
      } catch (e) {}
      TYPES.forEach(type => { try { pb.onEvent(type, (a) => emit(compact(type, a || {}, Date.now()), name, false)); } catch (e) {} });
    } catch (e) {}
  };

  // ---------------- Google Publisher Tag ----------------
  const KEYS = ['hb_bidder', 'hb_pb', 'hb_adid', 'hb_size', 'hb_source', 'hb_format', 'hb_deal', 'amznbid', 'amznp', 'amznsz', 'amzniid'];
  const targetingOf = (slot) => {
    const out = {};
    try { const m = slot.getTargetingMap() || {}; KEYS.forEach(k => { if (m[k] !== undefined) out[k] = Array.isArray(m[k]) ? m[k].join(',') : String(m[k]); }); } catch (e) {}
    return out;
  };
  let gptHooked = false;
  const hookGpt = () => {
    try {
      if (gptHooked) return;
      const pa = googletag.pubads();
      gptHooked = true;
      const base = (ev, name) => { const s = ev.slot; return {src: 'gpt', e: name, t: Date.now(), slot: s.getSlotElementId(), adUnit: s.getAdUnitPath(), targeting: targetingOf(s)}; };
      pa.addEventListener('slotRequested', ev => { try { push(base(ev, 'slotRequested')); } catch (e) {} });
      pa.addEventListener('slotResponseReceived', ev => { try { push(base(ev, 'slotResponseReceived')); } catch (e) {} });
      pa.addEventListener('slotRenderEnded', ev => {
        try {
          const r = base(ev, 'slotRenderEnded');
          r.isEmpty = !!ev.isEmpty; r.size = Array.isArray(ev.size) ? ev.size.join('x') : (ev.size ? String(ev.size) : null);
          r.lineItemId = ev.lineItemId === undefined ? null : ev.lineItemId; r.creativeId = ev.creativeId === undefined ? null : ev.creativeId;
          r.advertiserId = ev.advertiserId === undefined ? null : ev.advertiserId; r.campaignId = ev.campaignId === undefined ? null : ev.campaignId;
          r.sourceLineItemId = ev.sourceAgnosticLineItemId === undefined ? null : ev.sourceAgnosticLineItemId;
          r.yieldCompanyIds = ev.yieldGroupCompanyIds || null;
          push(r);
        } catch (e) {}
      });
      pa.addEventListener('impressionViewable', ev => { try { push(base(ev, 'impressionViewable')); } catch (e) {} });
    } catch (e) {}
  };
  try {
    window.googletag = window.googletag || {};
    googletag.cmd = googletag.cmd || [];
    googletag.cmd.push(hookGpt);
  } catch (e) {}

  const poll = () => {
    try { ((window._pbjsGlobals && window._pbjsGlobals.length) ? window._pbjsGlobals : ['pbjs']).forEach(hookPrebid); } catch (e) {}
    try { if (!gptHooked && window.googletag && googletag.apiReady && typeof googletag.pubads === 'function') hookGpt(); } catch (e) {}
  };
  setInterval(poll, 300);
})();
"""
_HARVEST_JS = "() => { const s = window.__siq_bids || []; const out = s.slice(); s.length = 0; return out; }"
_SLOTS_JS = ("() => { try { return googletag.pubads().getSlots().map(s => s.getSlotElementId()); } catch (e) { return []; } }")

# Overlays (app-install banners, sign-up nags, newsletter modals) that sit on
# top of a player and swallow the click that would start it.
_OVERLAY_CLOSE = [
    "button[aria-label*='close' i]", "[role='button'][aria-label*='close' i]",
    "button:has-text('Not now')", "button:has-text('No thanks')", "button:has-text('Maybe later')",
    "button:has-text('Continue without')", "[class*='modal' i] [class*='close' i]",
    "[class*='popup' i] [class*='close' i]", "[class*='dialog' i] button[class*='close' i]",
]
_PLAYER_HOSTS = ("youtube", "youtu.be", "vimeo", "jwplatform", "jwplayer", "brightcove", "dailymotion",
                 "wistia", "kaltura", "vidyard", "/player", "player.", "embed")
_NAME_RE = r"^\s*(play|watch(\s+(now|free|episode|trailer))?|start(\s+watching)?|resume|continue watching)\b"
_PLAYER_BOX_JS = r"""
() => {
  const sel = '[class*="player" i], [id*="player" i], [class*="video" i], [data-testid*="player" i], [data-testid*="video" i]';
  let best = null, area = 0;
  document.querySelectorAll(sel).forEach(el => {
    const r = el.getBoundingClientRect(); const a = r.width * r.height; const ratio = r.width / (r.height || 1);
    if (r.width >= 300 && r.height >= 170 && r.width <= 1500 && r.height <= 900 && ratio >= 1.3 && ratio <= 2.4 &&
        r.bottom > 0 && r.top < window.innerHeight * 3 && a > area) { best = el; area = a; }
  });
  if (!best) return null;
  best.scrollIntoView({block: 'center'});
  const r = best.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
}
"""
_DIAG_JS = r"""
() => {
  const txt = ((document.body && document.body.innerText) || '').slice(0, 8000).toLowerCase();
  const vids = __VIDS__;
  const iframes = Array.from(document.querySelectorAll('iframe')).map(f => { try { return new URL(f.src, location.href).host; } catch (e) { return ''; } }).filter(Boolean);
  const btns = Array.from(document.querySelectorAll('button, [role=button], a')).map(e => (((e.getAttribute('aria-label') || e.innerText || e.title || '') + '').trim()))
    .filter(t => /play|watch|start|resume|trailer|episode/i.test(t) && t.length < 40);
  return {
    title: (document.title || '').slice(0, 100), videos: vids.length,
    iframes: Array.from(new Set(iframes)).slice(0, 8), play_buttons: Array.from(new Set(btns)).slice(0, 8),
    login_wall: /(log in|sign in|sign up|create account|subscribe|members only|viki pass)/.test(txt) && vids.length === 0,
    geo_block: /(not available in your (country|region|area)|unavailable in your (country|region|area)|isn.t available in your)/.test(txt),
    bot_check: /(captcha|verify you are human|press & hold|before we continue|access denied|unusual traffic|just a moment)/.test(txt),
  };
}
""".replace("__VIDS__", _VIDS_EXPR)


def _settle(page, max_ms: int = 7000) -> None:
    """Single-page apps keep rendering after the page 'loads'. Wait (best
    effort) for the network to calm down so links and players exist before we
    look for them. Ad-heavy pages never go fully quiet, so this is capped."""
    try:
        page.wait_for_load_state("networkidle", timeout=max_ms)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _dismiss_overlays(page) -> int:
    """Close pop-ups that would swallow the click meant for a player."""
    n = 0
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for sel in _OVERLAY_CLOSE:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=1500)
                n += 1
                page.wait_for_timeout(400)
                if n >= 3:
                    break
        except Exception:
            continue
    return n


def _click_play_by_name(page) -> bool:
    """Click a visible button called Play / Watch / Start watching / Resume."""
    import re as _re
    for fr in page.frames:
        try:
            loc = fr.get_by_role("button", name=_re.compile(_NAME_RE, _re.I))
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _click_player_container(page) -> bool:
    """Last resort: click the middle of the biggest 16:9-ish player-looking box."""
    try:
        pt = page.evaluate(_PLAYER_BOX_JS)
        if not pt:
            return False
        page.mouse.click(pt["x"], pt["y"])
        return True
    except Exception:
        return False


def _player_signs(page) -> bool:
    """Is there anything on this page that looks like a video player?"""
    try:
        if any(v.get("visible") and v["w"] >= 200 and v["h"] >= 110 for v in _list_videos(page)):
            return True
        srcs = page.evaluate("() => Array.from(document.querySelectorAll('iframe')).map(f => ((f.src || '') + ' ' + (f.id || '') + ' ' + (f.className || '')).toLowerCase())") or []
        if any(any(h in s for h in _PLAYER_HOSTS) for s in srcs):
            return True
        for sel in _PLAY_SELECTORS[:12]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
    except Exception:
        pass
    return False


def _page_diag(page) -> dict:
    """What this page looked like when no video would play (for the report)."""
    try:
        d = page.evaluate(_DIAG_JS) or {}
    except Exception:
        d = {}
    return d


def _light_scroll(page) -> None:
    """A few quick scroll steps so lazily-loaded lists (episodes, cards) appear."""
    try:
        for _ in range(3):
            page.evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.8))")
            page.wait_for_timeout(500)
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


def _identity_for(browser_version: str) -> dict:
    """Build a browser identity that matches the browser actually running.

    Playwright's default announces itself as "HeadlessChrome" in the
    sec-ch-ua client hint, and overriding only the User-Agent string leaves
    that mismatch in place (UA says Chrome 128, hints say HeadlessChrome
    141). Ad servers treat both as bot signals. This makes the User-Agent,
    the client-hint headers and navigator.userAgentData all agree on one
    ordinary desktop Chrome of the same version as the real engine.
    """
    full = browser_version or "128.0.0.0"
    major = full.split(".")[0] or "128"
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not?A_Brand", "version": "99"},
    ]
    sec_ch_ua = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brands)
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    script = """
(() => {
  try {
    const brands = %s;
    const full = %s;
    const data = {
      brands, mobile: false, platform: 'Windows',
      getHighEntropyValues: (hints) => Promise.resolve({
        brands, mobile: false, platform: 'Windows', platformVersion: '10.0.0',
        architecture: 'x86', bitness: '64', model: '', wow64: false,
        uaFullVersion: full,
        fullVersionList: brands.map(b => ({brand: b.brand, version: full})),
      }),
      toJSON: () => ({brands, mobile: false, platform: 'Windows'}),
    };
    Object.defineProperty(navigator, 'userAgentData', {get: () => data, configurable: true});
  } catch (e) {}
})();
""" % (json.dumps(brands), json.dumps(full))
    return {
        "user_agent": ua,
        "headers": {
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        "script": script,
    }


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
                org = data.get("org") or data.get("isp") or ""
                return {
                    "ip": data.get("ip") or data.get("query", ""),
                    "country": data.get("country_name") or data.get("country", ""),
                    "country_code": data.get("country_code") or data.get("countryCode", ""),
                    "region": data.get("region") or data.get("regionName", ""),
                    "city": data.get("city", ""),
                    # Who actually owns this IP - a residential ISP name
                    # (Comcast, Jio, BSNL, ...) vs. a cloud provider name
                    # (Google LLC, Amazon.com, Microsoft Corporation, ...)
                    # is the real signal bot-detection systems check, not
                    # country. This makes that distinction visible instead
                    # of asking the person to just trust it.
                    "org": org,
                    "looks_like_datacenter": any(
                        kw in org.lower() for kw in
                        ("google", "amazon", "microsoft", "azure", "cloud",
                         "digitalocean", "linode", "vultr", "oracle", "hetzner",
                         "ovh", "aws", "gcp")
                    ),
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
                        proxy_password: str = "",
                        progress_cb=None,
                        extra_pages: int = 0,
                        video_path: str = "",
                        video_watch_s: int = 0,
                        find_video_page: bool = True,
                        use_chrome: bool = False,
                        show_browser: bool = False,
                        refresh_watch_s: int = 0) -> dict:
    """Open a web app, log in if credentials given, simulate playback, and
    record all network traffic. Returns capture stats + output path.

    proxy_server (optional): route the browser's traffic through a proxy,
    e.g. "http://us.proxyprovider.com:8080" - needed if you want to see
    geo-targeted (e.g. US-only) ad demand while running this from outside
    that region. Ad servers geo-target off the request's real IP, which
    Playwright/Chromium cannot spoof on its own - a proxy with an exit IP
    in the target country is the only way around that short of a VPN.

    extra_pages (optional): after the main page, also visit this many
    internal pages (a section page plus article pages) and capture their
    traffic into the same session - ad setups often differ between a
    homepage and an article. 0 = main page only.

    video_watch_s (optional): time budget, per page, for watching a video.
    The video is played muted (at least ~20 seconds of real playback), then
    the capture jumps to 25%, 50%, 75% and the end so mid-roll and post-roll
    ads get a chance to fire, waiting for each ad to show up. The video's
    position is sampled about once a second so every ad request can be tied
    to "the video was at 12:04 when this fired". 0 = leave video alone.

    refresh_watch_s (optional): after scrolling, stay on the page this many
    seconds with the ad slots in view so ads can refresh, and record every
    header-bidding auction and ad-server render from the page's own Prebid /
    Google Publisher Tag events (who was asked, who answered, who won).
    0 = don't wait for refreshes.

    find_video_page (optional): if the pages visited have no real video, open
    likely episode / watch pages (up to two clicks deep: home -> show ->
    episode) until one plays. Needed for streaming sites.

    use_chrome (optional): launch the Google Chrome installed on this
    computer instead of the bundled Chromium. Chrome includes the Widevine
    DRM module, which many streaming sites need before they will play (or
    show ads on) protected video. Falls back to Chromium if Chrome isn't
    installed.

    show_browser (optional): show the browser window instead of running it
    invisibly (falls back to invisible if no display is available).

    video_path (optional): if set, the whole session is recorded and saved
    here as a .webm video. The file is only finalized when the browser
    closes, so it can be replayed afterwards but not streamed live.

    progress_cb (optional): callable(str) invoked with a human-readable
    status line at each real stage of the capture (browser launched, page
    loaded, consent handled, scrolling, etc). Without this the caller has
    no way to distinguish "working normally" from "stuck" until it either
    finishes or errors - this makes real progress visible instead of one
    static message for the whole duration.
    """
    def _progress(msg: str, live: Optional[dict] = None):
        if progress_cb:
            try:
                progress_cb(msg, live)
            except TypeError:
                # backward-compatible with any caller still expecting the
                # old single-argument signature
                try:
                    progress_cb(msg)
                except Exception:
                    pass
            except Exception:
                pass

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
        # no-user-gesture-required: players that wait for a click before they
        # will start (and before they request their ads) can start by themselves.
        browser_args = ["--disable-blink-features=AutomationControlled",
                        "--autoplay-policy=no-user-gesture-required"]
        if sys.platform.startswith("linux"):
            browser_args += ["--no-sandbox", "--disable-dev-shm-usage"]  # needed in containers
        launch_kwargs = dict(
            headless=headless and not show_browser,
            args=browser_args,
            proxy=proxy_config,
            timeout=30_000,  # fail fast with a clear error instead of hanging forever
        )
        # Ordered launch attempts. The installed Chrome (if asked for) has the
        # Widevine DRM module; Chromium's "new" headless mode (Playwright >=
        # 1.49) is the full browser, far closer to a real visitor than the
        # stripped headless shell, which many ad servers refuse to serve;
        # the plain default is the last resort for older Playwright versions.
        attempts = []
        if use_chrome:
            attempts.append(("installed-chrome", {"channel": "chrome",
                                                  "ignore_default_args": ["--disable-component-update"]}))
        attempts.append(("new-headless", {"channel": "chromium"}))
        attempts.append(("headless-shell", {}))
        browser = None
        launch_mode = ""
        last_error = None
        for _round in range(2):
            for mode, extra in attempts:
                try:
                    browser = pw.chromium.launch(**launch_kwargs, **extra)
                    launch_mode = mode
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            if browser is not None or launch_kwargs["headless"]:
                break
            launch_kwargs["headless"] = True   # a visible window isn't possible here - go invisible
        if browser is None:
            raise last_error  # type: ignore[misc]
        if use_chrome and launch_mode != "installed-chrome":
            _progress("Google Chrome could not be launched here - using the built-in browser instead.")
        _progress("Browser launched, opening a new tab...")
        video_tmp = tempfile.mkdtemp(prefix="siq_video_") if video_path else ""

        identity = _identity_for(getattr(browser, "version", "") or "")

        def _make_context(with_video: bool):
            kw = dict(
                ignore_https_errors=True,
                viewport={"width": 1366, "height": 900},
                user_agent=identity["user_agent"],
                extra_http_headers=identity["headers"],
                locale="en-US",
            )
            if with_video:
                kw["record_video_dir"] = video_tmp
                kw["record_video_size"] = {"width": 960, "height": 600}
            c = browser.new_context(**kw)
            c.add_init_script(_STEALTH_INIT_SCRIPT)
            c.add_init_script(identity["script"])
            c.add_init_script(_EME_SPY_SCRIPT)
            c.add_init_script(_AUCTION_SPY_SCRIPT)
            return c

        # Video recording is a nice-to-have: if it can't start on this
        # machine (e.g. the recorder component is missing), carry on
        # without it instead of failing the whole capture.
        context = None
        try:
            context = _make_context(bool(video_path))
            exit_geo = _detect_exit_geo(context)
            page = context.new_page()
        except Exception:
            if not video_path:
                raise
            _progress("Video recording could not start here - continuing without it.")
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            context = _make_context(False)
            exit_geo = _detect_exit_geo(context)
            page = context.new_page()

        recording_started_at = time.time()  # the video recording starts as the page is created
        video_phases: list = []
        video_watch: list = []
        if exit_geo:
            where = ", ".join(x for x in (exit_geo.get("city"), exit_geo.get("country")) if x)
            _progress(f"Capturing from {where or 'unknown location'}"
                      + (f" · network: {exit_geo['org']}" if exit_geo.get("org") else ""),
                      {"exit_geo": exit_geo})

        # Live tally so the person watching can see ad-tech vendors show up
        # in real time instead of staring at one static message for the
        # whole capture. This is participation frequency (how often a
        # vendor's domain was hit), not bid price or auction win/loss -
        # actual bid economics are resolved server-side between exchanges
        # and are usually invisible to (or encrypted from) the browser, so
        # they can't be shown here honestly.
        vendor_tally: dict = {}
        total_count = [0]
        current_page = [url]  # which page is being visited right now

        def on_response(response):
            # This callback fires on EVERY network response - hundreds of
            # times per capture - so any single unhandled exception here
            # (e.g. a gzip/binary POST body that Playwright's post_data
            # can't UTF-8 decode, common on real Prebid/header-bidding
            # calls) crashes the whole capture, and in production has been
            # severe enough to take the whole server process down with it.
            # Every risky access below is now individually guarded so one
            # bad request can never derail the rest of the capture.
            try:
                req = response.request
                try:
                    post_data = req.post_data or ""
                except Exception:
                    post_data = ""  # binary/gzip/undecodable body - skip it,
                                    # not worth crashing the capture over
                entry = {
                    "url": req.url, "method": req.method, "status": response.status,
                    "request_headers": dict(req.headers),
                    "response_headers": dict(response.headers),
                    "request_body": post_data, "timestamp": time.time(),
                    "page": current_page[0],
                }
                ct = (response.headers.get("content-type") or "").lower()
                if any(t in ct for t in ("xml", "json", "text")):
                    try:
                        entry["response_body"] = response.text()[:2_000_000]
                    except Exception:
                        entry["response_body"] = ""
                captured.append(entry)

                total_count[0] += 1
                try:
                    host = urlparse(entry["url"]).netloc.lower()
                    vm = match_vendor(host)
                except Exception:
                    vm = None
                live = {"total": total_count[0], "vendor_tally": dict(vendor_tally)}
                if vm:
                    vendor_tally[vm[1]] = vendor_tally.get(vm[1], 0) + 1
                    live["vendor_tally"] = dict(vendor_tally)
                    ad_count = sum(vendor_tally.values())
                    top = sorted(vendor_tally.items(), key=lambda kv: -kv[1])[:3]
                    top_str = ", ".join(f"{k} ({v})" for k, v in top)
                    _progress(f"{total_count[0]} requests captured, {ad_count} ad-related "
                             f"so far. Most active: {top_str}", live)
                elif total_count[0] % 20 == 0:
                    _progress(f"{total_count[0]} requests captured so far...", live)
            except Exception:
                pass  # never let one bad response kill the whole capture

        page.on("response", on_response)
        _progress(f"Opening {url} (up to 60s for the page to load)...")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        _progress(f"Page loaded ({len(captured)} requests so far). Checking for a cookie/consent banner...",
                 {"screenshot": _take_screenshot(page)})
        page.wait_for_timeout(2000)
        consent_accepted = _accept_consent_banners(page)
        _progress("Consent handled." if consent_accepted else "No consent banner found/accepted.")
        page.wait_for_timeout(1500)

        if username and password:
            _progress("Attempting login with the provided credentials...")
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

        visited = [url]
        ad_slots: list = []
        video_samples: list = []
        auction_events: list = []
        tried_pages: set = {url}
        videos_done = [0]
        failed_shots = [0]

        def norm_url(u: str) -> str:
            u = (u or "").split("#")[0].lower()
            for pre in ("https://", "http://"):
                if u.startswith(pre):
                    u = u[len(pre):]
            return u.replace("www.", "", 1).rstrip("/")

        def ad_seen(t0: float):
            """Time of the first video-ad request (VAST/VMAP) since t0, or None."""
            first = None
            for e in captured[-600:]:
                ts = e.get("timestamp") or 0
                if ts < t0:
                    continue
                u = (e.get("url") or "").lower()
                body = (e.get("response_body") or "")[:1500].lower()
                if "vast" in u or "vmap" in u or "<vast" in body or "<vmap" in body:
                    if first is None or ts < first:
                        first = ts
            return first

        def harvest_auctions() -> None:
            """Collect what Prebid / Google Publisher Tag reported from inside the page.
            The page keeps these only in memory, so this runs before every navigation."""
            for fr in page.frames:
                try:
                    evs = fr.evaluate(_HARVEST_JS) or []
                except Exception:
                    continue
                for ev in evs:
                    ev["page"] = current_page[0]
                    if fr != page.main_frame:
                        ev["frame"] = (fr.url or "")[:120]
                    auction_events.append(ev)
            if len(auction_events) > 8000:
                del auction_events[: len(auction_events) - 8000]

        def sync_page_tag() -> None:
            """A click can navigate (a card, a Watch button). Keep the tags honest."""
            try:
                now = page.url
            except Exception:
                return
            if now and norm_url(now) != norm_url(current_page[0]):
                current_page[0] = now
                if now not in visited:
                    visited.append(now)
                tried_pages.add(now)

        def open_page(target: str, label: str) -> bool:
            harvest_auctions()
            current_page[0] = target
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                _progress(f"Could not load {label} - moving on.")
                return False
            page.wait_for_timeout(1500)
            _settle(page)
            _accept_consent_banners(page, timeout_ms=2500)
            _dismiss_overlays(page)
            _progress(f"Loaded {label} ({len(captured)} requests so far).",
                      {"screenshot": _take_screenshot(page)})
            return True

        def try_video_here(label: str) -> bool:
            """Start and watch the video on the current page, if it has a real one."""
            t_try = time.time()
            main, playing, reason = _start_playback(page, _progress)
            sync_page_tag()
            entry = {"found": False, "page": current_page[0], "reason": reason}
            if main is not None and playing and _qualifies(main):
                _progress(f"Found a playing video on {label} - watching it for ads...")
                res = _watch_video_ads(page, float(video_watch_s), _progress, video_phases,
                                       video_samples, current_page[0], main, ad_seen, start_t=t_try)
                video_watch.append(res)
                harvest_auctions()
                _slots2 = _scan_ad_slots(page, current_page[0])
                if _slots2 and all(x["page"] != _slots2["page"] for x in ad_slots):
                    ad_slots.append(_slots2)
                return True
            if main is not None and playing:
                entry["reason"] = ("the only video is a short looping clip (a background or hero video), "
                                   "not episode or article content")
            # Say what the page looked like, so a failure explains itself.
            diag = _page_diag(page)
            entry["diag"] = diag
            signals = []
            if diag.get("bot_check"):
                signals.append("the page looks like a bot check or an access-denied page")
            if diag.get("geo_block"):
                signals.append("the page says the video is not available in this country or region")
            if diag.get("login_wall"):
                signals.append("the page asks you to log in or sign up")
            if signals:
                entry["reason"] = entry["reason"] + ". The page itself shows: " + "; ".join(signals)
            if failed_shots[0] < 6:
                entry["screenshot"] = _take_screenshot(page, quality=30)
                failed_shots[0] += 1
            video_watch.append(entry)
            return False

        def discover_video() -> bool:
            """Open likely watch pages, up to two clicks deep, until a video plays."""
            queue = _pick_video_links(page, 4, tried_pages)
            loads = 0
            for _hop in range(2):
                nxt: list = []
                for target in queue:
                    if target in tried_pages or loads >= 6:
                        continue
                    tried_pages.add(target)
                    loads += 1
                    label = urlparse(target).path or "/"
                    _progress(f"Looking for a video: opening {label} ...")
                    if not open_page(target, label):
                        continue
                    visited.append(target)
                    before = page.url
                    if try_video_here(label):
                        return True
                    if norm_url(page.url) != norm_url(before):
                        # A Watch/Play click took us to another page (say, an episode that
                        # needs a login). Step back so the list of other episodes is still here.
                        try:
                            page.go_back(wait_until="domcontentloaded", timeout=15_000)
                            current_page[0] = page.url
                            _settle(page, 4000)
                        except Exception:
                            pass
                    _light_scroll(page)          # lists of episodes often load as you scroll
                    nxt += _pick_video_links(page, 3, tried_pages)
                queue = nxt[:4]
            return False

        def observe_refreshes(seconds: float) -> None:
            """Stay put with the ad slots in view so ads can refresh, and collect
            every auction and render as it happens."""
            if seconds <= 0:
                return
            def slot_ids():
                try:
                    return page.evaluate(_SLOTS_JS) or []
                except Exception:
                    return []
            _progress("Watching for ad refreshes (keeping the ad slots in view)...")
            t0 = time.time()
            end = t0 + seconds
            k = 0
            last_harvest = 0.0
            checked = False
            while time.time() < end:
                page.wait_for_timeout(1000)
                now = time.time()
                if now - last_harvest >= 5:
                    last_harvest = now
                    harvest_auctions()
                    n_init = sum(1 for e in auction_events if e.get("e") == "auctionInit")
                    n_render = sum(1 for e in auction_events if e.get("e") == "slotRenderEnded")
                    _progress(f"Watching ad refreshes: {n_init} header-bidding auction(s) and "
                              f"{n_render} ad render(s) so far...", {"screenshot": _take_screenshot(page)})
                    if not checked and now - t0 >= 10:
                        checked = True
                        if not auction_events and not slot_ids():
                            _progress("No ad slots or header-bidding activity found on this page - "
                                      "skipping the refresh watch.")
                            return
                if int(now - t0) % 8 == 0:
                    ids = slot_ids()
                    if ids:
                        try:
                            page.evaluate("(id) => { const el = document.getElementById(id); "
                                          "if (el) el.scrollIntoView({block: 'center'}); }", ids[k % len(ids)])
                            k += 1
                        except Exception:
                            pass
                    try:
                        page.mouse.move(200 + (k * 37) % 400, 200 + (k * 53) % 300)
                    except Exception:
                        pass
            harvest_auctions()

        # Pick the extra pages from the MAIN page first: finding a video can navigate away.
        extra_targets = _pick_internal_links(page, extra_pages) if extra_pages > 0 else []
        if extra_pages > 0 and not extra_targets:
            _progress("No suitable internal links found on this page - skipping extra pages.")

        main_watched = False
        main_probed = False
        if video_watch_s > 0:
            _settle(page)
            if _player_signs(page):
                _progress("A video player was spotted - starting it before scrolling...")
                main_probed = True
                if try_video_here("the main page"):
                    videos_done[0] += 1
                    main_watched = True
        else:
            # video watching is off: keep the old, simple behaviour
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

        scroll_s = float(duration_s)
        if video_watch_s > 0 and find_video_page and not main_watched:
            scroll_s = min(scroll_s, 30.0)   # streaming sites: spend the time finding the video instead
        if main_watched:
            scroll_s = min(scroll_s, 20.0)   # the video already took a while
        _progress(f"Scrolling through the page for {int(scroll_s)}s to trigger lazy-loaded ads...")
        _scroll_through_page(page, scroll_s, _progress,
                             stop_when=(lambda: video_watch_s > 0 and not main_probed and _player_signs(page)))
        harvest_auctions()

        if video_watch_s > 0 and not main_watched:
            if not main_probed and _player_signs(page):
                main_probed = True
                if try_video_here("the main page"):
                    videos_done[0] += 1
                    main_watched = True

        if refresh_watch_s > 0:
            observe_refreshes(float(refresh_watch_s))

        _slots = _scan_ad_slots(page, current_page[0])
        if _slots and all(x["page"] != _slots["page"] for x in ad_slots):
            ad_slots.append(_slots)

        if video_watch_s > 0 and not main_watched and find_video_page:
            _progress("No playable video on the main page - looking for an episode or watch page...")
            if discover_video():
                videos_done[0] += 1
            else:
                _progress("Could not find a video that plays on this site.")

        for idx, target in enumerate(extra_targets, 1):
            if target in tried_pages:
                continue
            tried_pages.add(target)
            label = urlparse(target).path or "/"
            _progress(f"Visiting internal page {idx} of {len(extra_targets)}: {label}")
            if not open_page(target, label):
                continue
            try:
                page.evaluate("document.querySelectorAll('video')"
                              ".forEach(v => v.play().catch(()=>{}))")
            except Exception:
                pass
            _scroll_through_page(page, min(25.0, max(10.0, duration_s * 0.35)), _progress)
            visited.append(target)
            harvest_auctions()
            if refresh_watch_s > 0:
                observe_refreshes(min(30.0, refresh_watch_s / 2.0))
            _slots = _scan_ad_slots(page, target)
            if _slots and all(x["page"] != _slots["page"] for x in ad_slots):
                ad_slots.append(_slots)
            if video_watch_s > 0 and videos_done[0] < 2:
                if try_video_here(label):
                    videos_done[0] += 1

        harvest_auctions()

        _progress(f"Capture complete ({len(captured)} requests across {len(visited)} page(s)). "
                  "Saving the recording and closing the browser...")
        video_obj = None
        try:
            video_obj = page.video
        except Exception:
            video_obj = None
        try:
            context.close()  # finalizes the video file
        except Exception:
            pass
        video_saved = False
        if video_path and video_obj is not None:
            try:
                video_obj.save_as(video_path)
                video_saved = Path(video_path).exists() and Path(video_path).stat().st_size > 0
            except Exception:
                video_saved = False
        try:
            browser.close()
        except Exception:
            pass
        if video_tmp:
            shutil.rmtree(video_tmp, ignore_errors=True)

    Path(out_path).write_text(json.dumps(captured, indent=2), encoding="utf-8")
    return {"out_path": out_path, "requests_captured": len(captured),
            "login_status": login_status, "duration_s": duration_s,
            "consent_accepted": consent_accepted, "proxy_used": bool(proxy_server),
            "exit_geo": exit_geo, "pages_visited": visited, "video_saved": video_saved,
            "launch_mode": launch_mode, "browser_version": getattr(browser, "version", ""),
            "ad_slots": ad_slots, "video_phases": video_phases, "video_watch": video_watch,
            "video_samples": video_samples, "auction_events": auction_events,
            "recording_started_at": recording_started_at}


def _wait_with_snapshots(page, total_ms: int, p, message: str, slice_ms: int = 2000) -> None:
    """Wait total_ms, but in short slices with a fresh screenshot between
    them, so the live view refreshes every couple of seconds rather than
    once per scroll step."""
    remaining = int(total_ms)
    while remaining > 0:
        chunk = min(slice_ms, remaining)
        page.wait_for_timeout(chunk)
        remaining -= chunk
        if remaining > 0:
            shot = _take_screenshot(page)
            if shot:
                p(message, {"screenshot": shot})


def _scroll_through_page(page, duration_s: float, progress_cb=None, stop_when=None) -> None:
    """Scroll down the page over the capture window instead of sitting still.

    Most display ad slots (GAM lazy-loaded units, infinite-scroll placements,
    sticky/anchor units that only render near-viewport) never fire a request
    if the page never scrolls. This walks the page in steps for most of the
    window, then scrolls back toward the top so above-the-fold refreshes
    (common on GAM slot refresh) get a chance to fire too. Video playback
    (started by the caller before this runs) continues throughout.
    """
    def _p(msg, live=None):
        if progress_cb:
            try:
                progress_cb(msg, live)
            except TypeError:
                try:
                    progress_cb(msg)
                except Exception:
                    pass
            except Exception:
                pass

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
        _p(f"Scrolling ({i}/{steps})...", {"screenshot": _take_screenshot(page)})
        _wait_with_snapshots(page, step_ms, _p, f"Scrolling ({i}/{steps})...")
        if stop_when is not None:
            try:
                if stop_when():
                    _p("A video player appeared - stopping the scroll to play it.")
                    return
            except Exception:
                pass

    remaining_ms = int(duration_s * 1000) - (steps * step_ms)
    if remaining_ms > 0:
        try:
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        except Exception:
            pass
        _p("Back near the top, letting above-the-fold units refresh...")
        page.wait_for_timeout(remaining_ms)
