"""SignalIQ Dashboard - local web server.

Run with:
    python -m dashboard.server
or:
    uvicorn dashboard.server:app --reload --port 8787

Then open http://127.0.0.1:8787 in a browser. This must run on YOUR machine
(not a locked-down sandbox) because it launches a real headless browser via
Playwright to visit whatever website you give it, and needs normal internet
access to reach ad servers.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import base64
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make the project root (parent of dashboard/) importable so `signaliq` and
# the top-level actions.py resolve regardless of the working directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signaliq.audit import run_audit, AuditResult  # noqa: E402
from signaliq.audience import build_audience_profile, build_display_signals, build_plain_summary  # noqa: E402
from signaliq.classifier import classify_all  # noqa: E402
from signaliq.video_ads import analyze_video_ads  # noqa: E402
from signaliq.bid_analysis import analyze_bidding  # noqa: E402
from signaliq.parser import load_traffic  # noqa: E402
from signaliq.capture import run_browser_capture, replay_request  # noqa: E402
from signaliq.proxy_capture import start_proxy, stop_proxy, proxy_status  # noqa: E402

SESSIONS_DIR = ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="SignalIQ Dashboard")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Shared team login. Set SIGNALIQ_USER + SIGNALIQ_PASS as environment
# variables on the server before starting this - if either is unset, auth is
# OFF (fine for localhost-only use, NOT fine once this is reachable over the
# internet, since this app launches a headless browser against any URL a
# visitor gives it).
# ---------------------------------------------------------------------------
DASHBOARD_USER = os.environ.get("SIGNALIQ_USER", "")
DASHBOARD_PASS = os.environ.get("SIGNALIQ_PASS", "")


@app.middleware("http")
async def _require_login(request: Request, call_next):
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        return await call_next(request)
    if request.method == "HEAD":
        # Render (and most hosts) health-check with a bare HEAD / and no
        # credentials. Blocking that with 401 can make the platform think
        # the deploy is unhealthy and cycle the container. Real page loads
        # are always GET, so exempting HEAD costs no real security.
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    valid = False
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            user, _, pw = decoded.partition(":")
            valid = (secrets.compare_digest(user, DASHBOARD_USER)
                      and secrets.compare_digest(pw, DASHBOARD_PASS))
        except Exception:
            valid = False
    if not valid:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="SignalIQ"'})
    return await call_next(request)

# ---------------------------------------------------------------------------
# In-memory job tracking for long-running browser captures
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **kw):
    with _jobs_lock:
        # live_stats gets merged field-by-field into whatever's already
        # there, not wholesale-replaced. Different progress calls report
        # different subsets (a screenshot here, a vendor tally there) -
        # a plain overwrite would make each new update erase the fields
        # the previous update had, e.g. losing the last screenshot the
        # moment a vendor-count-only update comes in right after it.
        if "live_stats" in kw:
            live = kw.pop("live_stats")
            if live is not None:
                existing = _jobs[job_id].get("live_stats") or {}
                _jobs[job_id]["live_stats"] = {**existing, **live}
        _jobs[job_id].update(kw)


def _run_capture_job(job_id: str, url: str, username: str, password: str,
                      duration_seconds: int, target_name: str,
                      baseline_daily_revenue: float,
                      proxy_server: str = "", proxy_username: str = "",
                      proxy_password: str = "", extra_pages: int = 2,
                      video_watch_seconds: int = 120, find_video_page: bool = True,
                      use_chrome: bool = False, show_browser: bool = False,
                      refresh_watch_seconds: int = 60):
    session_id = f"session_{abs(hash(url + str(time.time()))) % 100000}"
    out_path = SESSIONS_DIR / f"{session_id}.json"
    try:
        _set_job(job_id, status="capturing",
                 message=f"Launching headless browser and opening {url} ..."
                         + (" (via proxy)" if proxy_server else ""))
        stats = run_browser_capture(
            url, username=username, password=password,
            duration_s=float(duration_seconds), out_path=str(out_path),
            proxy_server=proxy_server, proxy_username=proxy_username,
            proxy_password=proxy_password,
            progress_cb=lambda msg, live=None: _set_job(job_id, message=msg, live_stats=live),
            extra_pages=extra_pages,
            video_path=str(SESSIONS_DIR / f"{session_id}.webm"),
            video_watch_s=video_watch_seconds,
            find_video_page=find_video_page,
            use_chrome=use_chrome,
            show_browser=show_browser,
            refresh_watch_s=refresh_watch_seconds,
        )
        try:
            meta = {k: stats.get(k) for k in (
                "exit_geo", "pages_visited", "video_saved", "proxy_used",
                "consent_accepted", "launch_mode", "browser_version", "ad_slots",
                "duration_s", "video_phases", "video_watch", "recording_started_at")}
            samples = list(stats.get("video_samples") or [])
            if len(samples) > 900:                     # keep the shape, drop the bulk
                step = -(-len(samples) // 900)
                samples = samples[::step]
            meta["video_samples"] = samples
            meta["auction_events"] = list(stats.get("auction_events") or [])[:6000]
            meta["chrome_requested"] = bool(use_chrome)
            meta["browser_window_requested"] = bool(show_browser)
            meta["captured_at"] = time.time()
            _meta_path(session_id).write_text(json.dumps(meta), encoding="utf-8")
        except Exception:
            pass
        _set_job(job_id, status="auditing", message="Capture complete, running audit ...",
                 capture_stats=stats, session_id=session_id)
        result = run_audit(str(out_path), target=target_name or url,
                            baseline_revenue=baseline_daily_revenue)
        _set_job(job_id, status="done", message="Done.",
                 session_id=session_id, result=_session_detail(session_id, result))
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, status="error", message=str(exc))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _meta_path(session_id: str) -> Path:
    # Deliberately NOT *.json: the sessions list globs session_*.json.
    return SESSIONS_DIR / f"{session_id}.meta"


def _load_meta(session_id: str):
    try:
        return json.loads(_meta_path(session_id).read_text(encoding="utf-8"))
    except Exception:
        return None


def _valid_session_id(session_id: str) -> bool:
    """Session ids are always session_<digits>. Checking this stops a
    crafted id from reaching outside the sessions folder."""
    return bool(re.fullmatch(r"session_\d+", session_id or ""))


# ---------------------------------------------------------------------------
# Video ad creatives: save a copy of a creative so it can be replayed later
# ---------------------------------------------------------------------------

CREATIVE_MAX_BYTES = 60 * 1024 * 1024
_CREATIVE_TYPES = {"video/mp4": "mp4", "video/webm": "webm", "video/ogg": "ogv", "video/x-m4v": "m4v"}
_CREATIVE_MEDIA = {"mp4": "video/mp4", "webm": "video/webm", "ogv": "video/ogg", "m4v": "video/mp4"}


def _creative_dir(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}_creatives"


def _creative_name(url: str, media_type: str):
    """File name a saved creative is stored under, or None when the creative
    is a streaming playlist (HLS/DASH) that can't be saved as one file."""
    ext = _CREATIVE_TYPES.get((media_type or "").lower().split(";")[0].strip())
    if not ext:
        m = re.search(r"\.(mp4|webm|ogv|m4v)(?:$|[?#])", (url or "").lower())
        ext = m.group(1) if m else None
    if not ext:
        return None
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + "." + ext


def _annotate_creatives(session_id: str, video_ads: dict) -> None:
    for ad in video_ads.get("ad_requests", []):
        for cr in ad.get("creatives", []):
            for mf in cr.get("media_files", []):
                name = _creative_name(mf.get("url", ""), mf.get("type", ""))
                mf["saveable"] = bool(name)
                if name and (_creative_dir(session_id) / name).exists():
                    mf["local_url"] = f"/api/sessions/{session_id}/creative/{name}"


def _check_public_url(url: str) -> None:
    """Only plain http(s) URLs that resolve to public internet addresses may
    be downloaded, so this feature can't be used to reach into the machine
    or network the dashboard runs on."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(400, "Only http(s) creative URLs can be saved.")
    if os.environ.get("SIGNALIQ_ALLOW_PRIVATE_CREATIVES") == "1":
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError:
        raise HTTPException(502, "Could not look up the creative's host.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise HTTPException(400, "That creative is on a private/internal address, so it won't be downloaded.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # we follow redirects ourselves, re-checking each hop
        return None


def _download_creative(url: str):
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(4):
        _check_public_url(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
            "Accept": "*/*"})
        try:
            resp = opener.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urllib.parse.urljoin(url, loc)
                continue
            raise HTTPException(502, f"The ad server answered HTTP {e.code} (the link may have expired).")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Could not download the creative: {e}")
        data = bytearray()
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > CREATIVE_MAX_BYTES:
                raise HTTPException(413, "That creative is larger than 60 MB, so it was not saved.")
        return bytes(data)
    raise HTTPException(502, "Too many redirects while downloading the creative.")


def _session_detail(session_id: str, result: AuditResult) -> dict:
    audience = build_audience_profile(result.entries)
    display = build_display_signals(result.entries)
    meta = _load_meta(session_id)
    slot_total = 0
    for pg in ((meta or {}).get("ad_slots") or []):
        slot_total += len(pg.get("gpt_slots") or []) + len(pg.get("ad_iframes") or []) + int(pg.get("containers") or 0)
    try:
        video_ads = analyze_video_ads(result.entries, meta)
        _annotate_creatives(session_id, video_ads)
    except Exception as exc:  # noqa: BLE001 - never let this break a report
        video_ads = {"has_video_ads": False, "error": str(exc), "summary": {}, "ad_requests": [],
                     "vmaps": [], "timeline": [], "phases": [], "notes": []}
    try:
        bidding = analyze_bidding(result.entries, meta)
    except Exception as exc:  # noqa: BLE001 - never let this break a report
        bidding = {"available": False, "error": str(exc), "source": "none", "slots": [], "overall": {},
                   "bidders": [], "notes": []}
    vsum = video_ads.get("summary") or {}
    plain_summary = build_plain_summary(audience, display, {
        "other_ad_requests": sum(1 for e in result.entries if e.category == "Other Ad"),
        "ad_slots": slot_total,
        "video_ads": {"requests": vsum.get("ad_requests", 0), "filled": vsum.get("filled", 0),
                      "no_fill": vsum.get("no_fill", 0), "positions": vsum.get("by_position", {})},
        "bidding": bidding.get("overall") if bidding.get("available") else None,
        "bidding_source": bidding.get("source"),
    })
    traffic = [
        {
            "id": e.id, "category": e.category, "method": e.method,
            "status": e.status, "url": e.url, "vendor": getattr(e, "vendor", ""),
            "timestamp": e.started_at,
            "page": getattr(e, "page", ""),
            "request_headers": e.request_headers,
            "response_headers": e.response_headers,
            # Truncated - full bodies can be up to 2MB each and a session
            # can have hundreds of requests, so sending everything in full
            # would bloat the payload to the browser for little benefit;
            # this is plenty for a manual "advanced review" read.
            "request_body": (e.request_body or "")[:5000],
            "response_body": (e.response_body or "")[:5000],
        }
        for e in result.entries
    ]
    return {
        "session_id": session_id,
        "video_ads": video_ads,
        "bidding": bidding,
        "audit": result.to_dict(),
        "audience": audience,
        "display": display,
        "plain_summary": plain_summary,
        "traffic": traffic,
        "has_video": (SESSIONS_DIR / f"{session_id}.webm").exists(),
        "capture_meta": meta,
    }


def _load_and_audit(source, target_name: str, baseline_daily_revenue: float) -> AuditResult:
    try:
        return run_audit(source, target=target_name, baseline_revenue=baseline_daily_revenue)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Audit failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CaptureRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    duration_seconds: int = 60
    target_name: str = ""
    baseline_daily_revenue: float = 5000.0
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    extra_pages: int = 2  # internal pages to visit after the main page (0-5)
    video_watch_seconds: int = 120  # watch the page's video this long, per page, jumping to cue points (0 = off)
    find_video_page: bool = True    # open episode / watch pages if no video plays on the pages visited
    use_chrome: bool = False        # use the installed Google Chrome (has the Widevine DRM module)
    show_browser: bool = False      # show the browser window instead of running invisibly
    refresh_watch_seconds: int = 60  # stay on the page with ad slots in view so ads refresh (0 = off)


class PasteRequest(BaseModel):
    traffic_json: str
    target_name: str = ""
    baseline_daily_revenue: float = 5000.0


class ReplayRequest(BaseModel):
    url: str
    method: str = "GET"
    headers: dict = {}
    body: str = ""


class AppStartRequest(BaseModel):
    port: int = 8080


class AppStopRequest(BaseModel):
    target_name: str = ""
    baseline_daily_revenue: float = 5000.0


# ---------------------------------------------------------------------------
# Website capture (Playwright)
# ---------------------------------------------------------------------------

@app.post("/api/capture")
def api_capture(req: CaptureRequest):
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "message": "Queued.", "created": time.time()}
    # If the form left the proxy fields blank, fall back to a server-wide
    # default (set via DEFAULT_PROXY_SERVER/USER/PASS env vars) so the
    # person running a capture doesn't have to paste the same proxy in
    # every single time. An explicit value in the form always wins.
    proxy_server = req.proxy_server or os.environ.get("DEFAULT_PROXY_SERVER", "")
    proxy_username = req.proxy_username or os.environ.get("DEFAULT_PROXY_USERNAME", "")
    proxy_password = req.proxy_password or os.environ.get("DEFAULT_PROXY_PASSWORD", "")
    t = threading.Thread(
        target=_run_capture_job,
        args=(job_id, req.url, req.username, req.password, req.duration_seconds,
              req.target_name, req.baseline_daily_revenue,
              proxy_server, proxy_username, proxy_password,
              max(0, min(5, req.extra_pages)),
              max(0, min(300, req.video_watch_seconds)),
              bool(req.find_video_page), bool(req.use_chrome), bool(req.show_browser),
              max(0, min(600, req.refresh_watch_seconds))),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
def api_list_sessions():
    files = sorted(SESSIONS_DIR.glob("session_*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            n = -1
        out.append({
            "session_id": f.stem, "requests": n,
            "captured_at": time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(f.stat().st_mtime)),
        })
    return out


@app.get("/api/sessions/{session_id}")
def api_session_detail(session_id: str, target_name: str = "",
                        baseline_daily_revenue: float = 5000.0):
    if not _valid_session_id(session_id):
        raise HTTPException(404, "No such session.")
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        raise HTTPException(404, "No such session.")
    result = _load_and_audit(str(path), target_name, baseline_daily_revenue)
    return _session_detail(session_id, result)


@app.get("/api/sessions/{session_id}/video")
def api_session_video(session_id: str):
    """The recorded replay of a capture session (WebM)."""
    if not _valid_session_id(session_id):
        raise HTTPException(404, "No such recording.")
    path = SESSIONS_DIR / f"{session_id}.webm"
    if not path.exists():
        raise HTTPException(404, "No recording for this session.")
    return FileResponse(str(path), media_type="video/webm")


class CreativeSaveRequest(BaseModel):
    url: str


@app.post("/api/sessions/{session_id}/creative/save")
def api_save_creative(session_id: str, req: CreativeSaveRequest):
    """Download one of this session's video ad creatives and keep a copy."""
    if not _valid_session_id(session_id):
        raise HTTPException(404, "No such session.")
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        raise HTTPException(404, "No such session.")
    entries = load_traffic(str(path))
    classify_all(entries)
    analysis = analyze_video_ads(entries, _load_meta(session_id))
    allowed = {}
    for ad in analysis["ad_requests"]:
        for cr in ad["creatives"]:
            for mf in cr["media_files"]:
                allowed[mf["url"]] = mf
    mf = allowed.get(req.url)
    if mf is None:
        raise HTTPException(400, "That URL is not a creative from this session.")
    name = _creative_name(mf["url"], mf.get("type", ""))
    if not name:
        raise HTTPException(400, "This creative is a streaming playlist (HLS/DASH) and can't be saved as one file.")
    folder = _creative_dir(session_id)
    folder.mkdir(exist_ok=True)
    target = folder / name
    if not target.exists():
        data = _download_creative(mf["url"])
        target.write_bytes(data)
    return {"local_url": f"/api/sessions/{session_id}/creative/{name}", "bytes": target.stat().st_size, "name": name}


@app.get("/api/sessions/{session_id}/creative/{name}")
def api_get_creative(session_id: str, name: str):
    if not _valid_session_id(session_id) or not re.fullmatch(r"[0-9a-f]{16}\.(mp4|webm|ogv|m4v)", name or ""):
        raise HTTPException(404, "No such creative.")
    path = _creative_dir(session_id) / name
    if not path.exists():
        raise HTTPException(404, "No such creative.")
    return FileResponse(str(path), media_type=_CREATIVE_MEDIA[name.rsplit(".", 1)[1]])


# ---------------------------------------------------------------------------
# Paste HAR / JSON directly
# ---------------------------------------------------------------------------

@app.post("/api/paste")
def api_paste(req: PasteRequest):
    try:
        data = json.loads(req.traffic_json)
    except ValueError as exc:
        raise HTTPException(400, f"Not valid JSON/HAR: {exc}") from exc
    session_id = f"session_{abs(hash(str(time.time()))) % 100000}"
    (SESSIONS_DIR / f"{session_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")
    result = _load_and_audit(data, req.target_name, req.baseline_daily_revenue)
    return _session_detail(session_id, result)


# ---------------------------------------------------------------------------
# Replay a single request
# ---------------------------------------------------------------------------

@app.post("/api/replay")
def api_replay(req: ReplayRequest):
    return replay_request(req.url, method=req.method, headers=req.headers, body=req.body)


# ---------------------------------------------------------------------------
# Mobile / CTV app capture (mitmproxy)
# ---------------------------------------------------------------------------

@app.post("/api/app/start")
def api_app_start(req: AppStartRequest):
    r = start_proxy(req.port)
    if r["status"] == "error":
        raise HTTPException(500, r["error"])
    return r


@app.get("/api/app/status")
def api_app_status():
    return proxy_status()


@app.post("/api/app/stop")
def api_app_stop(req: AppStopRequest):
    session_id = f"session_{abs(hash(str(time.time()))) % 100000}"
    out_path = SESSIONS_DIR / f"{session_id}.json"
    r = stop_proxy(str(out_path))
    if r["status"] == "not_running":
        raise HTTPException(400, "No proxy running.")
    if r["flows_captured"] == 0:
        return {"session_id": session_id, "stopped": r, "audit": None}
    result = _load_and_audit(str(out_path), req.target_name, req.baseline_daily_revenue)
    return _session_detail(session_id, result)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    # Defaults to localhost-only (safe for your own machine). Docker/servers
    # set HOST=0.0.0.0 (the Dockerfile below does this via CMD) so it's
    # actually reachable from outside the container.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))
    uvicorn.run("dashboard.server:app", host=host, port=port, reload=False)
