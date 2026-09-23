"""Charles-style proxy capture engine using mitmproxy.

Runs a MITM proxy in a background thread, records all flows, and converts
them into SignalIQ's JSON capture format on stop.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

_state = {"master": None, "thread": None, "flows": [], "started_at": None}


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class _Recorder:
    """mitmproxy addon that records completed flows."""

    def __init__(self, store):
        self.store = store

    def response(self, flow):
        try:
            req, res = flow.request, flow.response
            entry = {
                "url": req.pretty_url,
                "method": req.method,
                "status": res.status_code if res else None,
                "request_headers": dict(req.headers),
                "response_headers": dict(res.headers) if res else {},
                "request_body": "",
                "response_body": "",
                "timestamp": flow.request.timestamp_start,
            }
            if req.content:
                try:
                    entry["request_body"] = req.get_text(strict=False)[:2_000_000]
                except Exception:
                    pass
            ct = (res.headers.get("content-type", "") if res else "").lower()
            if res and res.content and any(
                    t in ct for t in ("xml", "json", "text", "javascript")):
                try:
                    entry["response_body"] = res.get_text(strict=False)[:2_000_000]
                except Exception:
                    pass
            self.store.append(entry)
        except Exception:
            pass


def start_proxy(port: int = 8080) -> dict:
    """Start the MITM proxy in a background thread."""
    if _state["master"] is not None:
        return {"status": "already_running", "ip": _local_ip(), "port": port,
                "flows_so_far": len(_state["flows"])}

    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    _state["flows"] = []
    started = threading.Event()
    error = []

    def run():
        async def main():
            opts = Options(listen_host="0.0.0.0", listen_port=port)
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            master.addons.add(_Recorder(_state["flows"]))
            _state["master"] = master
            started.set()
            await master.run()
        try:
            asyncio.run(main())
        except Exception as e:
            error.append(str(e))
            started.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _state["thread"] = t
    _state["started_at"] = time.time()
    started.wait(timeout=10)

    if error:
        _state["master"] = None
        return {"status": "error", "error": error[0]}
    return {"status": "running", "ip": _local_ip(), "port": port,
            "cert_url": "http://mitm.it"}


def proxy_status() -> dict:
    running = _state["master"] is not None
    return {
        "running": running,
        "flows_captured": len(_state["flows"]),
        "uptime_s": round(time.time() - _state["started_at"], 1)
        if running and _state["started_at"] else 0,
    }


def stop_proxy(out_path: str) -> dict:
    """Stop the proxy and write captured flows to a session file."""
    master = _state["master"]
    if master is None:
        return {"status": "not_running", "flows_captured": 0}
    try:
        master.shutdown()
    except Exception:
        pass
    if _state["thread"]:
        _state["thread"].join(timeout=10)
    flows = list(_state["flows"])
    _state.update({"master": None, "thread": None, "flows": [], "started_at": None})
    Path(out_path).write_text(json.dumps(flows, indent=2), encoding="utf-8")
    return {"status": "stopped", "flows_captured": len(flows), "out_path": out_path}
