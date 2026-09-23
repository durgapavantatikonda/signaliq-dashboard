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

import json
import sys
import threading
import time
import uuid
from pathlib import Path

import base64
import os
import secrets

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
        _jobs[job_id].update(kw)


def _run_capture_job(job_id: str, url: str, username: str, password: str,
                      duration_seconds: int, target_name: str,
                      baseline_daily_revenue: float,
                      proxy_server: str = "", proxy_username: str = "",
                      proxy_password: str = ""):
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
            progress_cb=lambda msg: _set_job(job_id, message=msg),
        )
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

def _session_detail(session_id: str, result: AuditResult) -> dict:
    audience = build_audience_profile(result.entries)
    display = build_display_signals(result.entries)
    plain_summary = build_plain_summary(audience, display)
    traffic = [
        {
            "id": e.id, "category": e.category, "method": e.method,
            "status": e.status, "url": e.url, "vendor": getattr(e, "vendor", ""),
            "timestamp": e.started_at,
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
        "audit": result.to_dict(),
        "audience": audience,
        "display": display,
        "plain_summary": plain_summary,
        "traffic": traffic,
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
    t = threading.Thread(
        target=_run_capture_job,
        args=(job_id, req.url, req.username, req.password, req.duration_seconds,
              req.target_name, req.baseline_daily_revenue,
              req.proxy_server, req.proxy_username, req.proxy_password),
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
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        raise HTTPException(404, "No such session.")
    result = _load_and_audit(str(path), target_name, baseline_daily_revenue)
    return _session_detail(session_id, result)


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