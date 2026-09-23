"""Sema4.ai actions exposing the SignalIQ engine to the agent."""

import json
import time
from pathlib import Path

from sema4ai.actions import action

from signaliq.audit import run_audit
from signaliq.report import render_markdown_report
from signaliq.parser import load_traffic
from signaliq.classifier import classify_all
from signaliq.capture import run_browser_capture as _browser_capture
from signaliq.capture import replay_request as _replay
from signaliq.proxy_capture import start_proxy, stop_proxy, proxy_status

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)


@action
def run_browser_capture(url: str, username: str = "", password: str = "",
                        duration_seconds: int = 45) -> str:
    """Launch a headless browser, open a web app, log in if credentials are
    provided, simulate playback, and record network traffic. Use for WEBSITES
    and web players (not native apps).

    Args:
        url: The webapp/player URL to capture.
        username: Login username or email. Empty if no login needed.
        password: Login password. Empty if no login needed.
        duration_seconds: How long to record after playback starts.

    Returns:
        session_id plus capture stats (request count, login status).
    """
    session_id = f"session_{abs(hash(url + str(time.time()))) % 100000}"
    out = SESSIONS_DIR / f"{session_id}.json"
    try:
        stats = _browser_capture(url, username=username, password=password,
                                 duration_s=float(duration_seconds),
                                 out_path=str(out))
    except Exception as e:
        return f"Capture failed: {e}"
    return (f"session_id: {session_id} | requests captured: "
            f"{stats['requests_captured']} | login: {stats['login_status']}")


@action
def start_app_capture(port: int = 8080) -> str:
    """Start a Charles-style MITM proxy to capture traffic from a MOBILE or
    CTV app. The user must point their device Wi-Fi proxy at the returned
    IP:port and install the certificate.

    Args:
        port: Proxy listen port. Default 8080.

    Returns:
        Proxy IP, port, and device setup instructions to relay to the user.
    """
    r = start_proxy(port)
    if r["status"] == "error":
        return f"Proxy failed to start: {r['error']}"
    if r["status"] == "already_running":
        return (f"Proxy already running at {r['ip']}:{port} with "
                f"{r['flows_so_far']} requests captured. Say 'stop capture'.")
    return (
        f"Proxy running at {r['ip']}:{r['port']}.\n\n"
        f"DEVICE SETUP (one time):\n"
        f"1. Device must be on the SAME Wi-Fi as this machine.\n"
        f"2. Device Wi-Fi settings -> Proxy = Manual, host {r['ip']}, "
        f"port {r['port']}.\n"
        f"3. On the device browser open http://mitm.it and install the cert.\n"
        f"   - iOS: also enable in Settings > General > About > "
        f"Certificate Trust Settings.\n"
        f"   - Android: apps trust user certs only for your own app, an "
        f"emulator, or a rooted device.\n"
        f"4. Open the app and play content through an ad break (~60s).\n"
        f"5. Say 'done' or 'stop capture' when finished."
    )


@action
def check_app_capture() -> str:
    """Check whether the app-capture proxy is running and how many requests
    have been captured so far.

    Returns:
        Proxy status and live request count.
    """
    s = proxy_status()
    if not s["running"]:
        return "Proxy is not running. Use start_app_capture to begin."
    return (f"Proxy running for {s['uptime_s']}s - "
            f"{s['flows_captured']} requests captured so far.")


@action
def stop_app_capture() -> str:
    """Stop the app-capture proxy and save captured traffic as a session.

    Returns:
        session_id and capture stats.
    """
    session_id = f"session_{abs(hash(str(time.time()))) % 100000}"
    out = SESSIONS_DIR / f"{session_id}.json"
    r = stop_proxy(str(out))
    if r["status"] == "not_running":
        return "No proxy is running. Use start_app_capture first."
    return (f"Capture complete. session_id: {session_id} | requests: "
            f"{r['flows_captured']}. Next: diagnose_capture('{session_id}') "
            f"then audit_session('{session_id}').")


@action
def diagnose_capture(session_id: str) -> str:
    """Check a captured session for certificate pinning or setup problems,
    explaining WHY ad traffic may be missing.

    Args:
        session_id: The session ID from a capture action.

    Returns:
        A plain-English diagnosis of capture health.
    """
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return f"No session found with id '{session_id}'."
    try:
        entries = classify_all(load_traffic(str(path)))
    except Exception as e:
        return f"Could not read session: {e}"

    total = len(entries)
    if total == 0:
        return ("0 requests captured. The device proxy is not pointed at us, "
                "or not on the same Wi-Fi. Recheck proxy + certificate.")
    ad_traffic = [e for e in entries
                  if e.category in ("OpenRTB", "VAST", "SSAI", "Tracking Pixel")]
    if total > 20 and not ad_traffic:
        return (f"Captured {total} requests but ZERO ad traffic. Likely (1) no "
                f"ad break played, or (2) this app uses CERTIFICATE PINNING on "
                f"its ad SDK - the same wall Charles hits. If normal traffic is "
                f"visible but ad calls are not, it is pinning. Try the app's web "
                f"player, or a non-pinned app (free game with rewarded video).")
    if ad_traffic:
        return (f"Healthy capture: {total} requests, {len(ad_traffic)} "
                f"ad-related. Ready - call audit_session('{session_id}').")
    return (f"Captured {total} requests, no ad traffic. Play through an ad "
            f"break, then capture again.")


@action
def get_session_traffic(session_id: str) -> str:
    """Fetch the classified network request log for a captured session.

    Args:
        session_id: The session ID from a capture action.

    Returns:
        A category summary plus one line per request.
    """
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return f"No session found with id '{session_id}'. Try list_sessions."
    try:
        entries = classify_all(load_traffic(str(path)))
    except Exception as e:
        return f"Failed to load session traffic: {e}"
    if not entries:
        return "Session exists but contains no captured requests."
    lines = [f"{e.id}  {e.category:<15} {e.method:<6} {e.url[:120]}"
             for e in entries]
    counts = {}
    for e in entries:
        counts[e.category] = counts.get(e.category, 0) + 1
    summary = " | ".join(f"{cat}: {n}" for cat, n in
                         sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"CATEGORY SUMMARY: {summary}\n\n" + "\n".join(lines)


@action
def audit_session(session_id: str, target_name: str = "",
                  baseline_daily_revenue: float = 5000.0) -> str:
    """Run the full SignalIQ compliance audit on a captured session.

    Args:
        session_id: The session ID from a capture action.
        target_name: App/stream name shown in the report header.
        baseline_daily_revenue: Daily revenue in USD for the risk calc.

    Returns:
        The complete Markdown audit report. Present it verbatim.
    """
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return f"No session found with id '{session_id}'. Try list_sessions."
    try:
        result = run_audit(str(path), target=target_name,
                           baseline_revenue=baseline_daily_revenue)
    except Exception as e:
        return f"Audit failed: {e}"
    return render_markdown_report(result)


@action
def audit_traffic_json(traffic_json: str, target_name: str = "",
                       baseline_daily_revenue: float = 5000.0) -> str:
    """Audit traffic provided directly (HAR contents or a JSON capture array).

    Args:
        traffic_json: Raw HAR content or JSON capture array as a string.
        target_name: App/stream name shown in the report header.
        baseline_daily_revenue: Daily revenue in USD for the risk calc.

    Returns:
        The complete Markdown audit report. Present it verbatim.
    """
    try:
        data = json.loads(traffic_json)
    except ValueError as e:
        return (f"Could not parse content as JSON/HAR: {e}. Ask the user for "
                f"valid HAR contents or a JSON array of requests.")
    try:
        result = run_audit(data, target=target_name,
                           baseline_revenue=baseline_daily_revenue)
    except Exception as e:
        return f"Audit failed: {e}"
    return render_markdown_report(result)


@action
def replay_request(url: str, method: str = "GET",
                   headers_json: str = "{}", body: str = "") -> str:
    """Send a custom HTTP request to test ad-server behavior (e.g. re-fire a
    failing VAST tag).

    Args:
        url: The target URL.
        method: HTTP method. Default GET.
        headers_json: JSON object of request headers.
        body: Optional request body.

    Returns:
        JSON with status, timing, headers, and response body.
    """
    try:
        headers = json.loads(headers_json or "{}")
    except ValueError:
        return "headers_json is not valid JSON."
    result = _replay(url, method=method, headers=headers, body=body)
    return json.dumps(result, indent=2)[:8000]


@action
def list_sessions() -> str:
    """List all captured sessions available for auditing.

    Returns:
        One line per session: session_id, request count, capture time.
    """
    files = sorted(SESSIONS_DIR.glob("session_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "No sessions captured yet. Use a capture action first."
    lines = []
    for f in files:
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            n = -1
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
        lines.append(f"{f.stem}  |  {n} requests  |  captured {ts}")
    return "\n".join(lines)
