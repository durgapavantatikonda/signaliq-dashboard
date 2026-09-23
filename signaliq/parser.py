"""Traffic ingestion & normalization: HAR files, JSON capture arrays, dicts."""

from __future__ import annotations

import json
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class TrafficEntry:
    """A single normalized network request/response pair."""

    index: int
    url: str
    method: str = "GET"
    status: Optional[int] = None
    request_headers: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)
    request_body: str = ""
    response_body: str = ""
    mime_type: str = ""
    started_at: str = ""
    time_ms: Optional[float] = None
    category: str = "Other"
    vendor: str = ""

    @property
    def request_json(self) -> Optional[Any]:
        return _try_json(self.request_body)

    @property
    def response_json(self) -> Optional[Any]:
        return _try_json(self.response_body)

    @property
    def id(self) -> str:
        return f"REQ-{self.index:04d}"


def _try_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _headers_to_dict(headers: Any) -> dict:
    if isinstance(headers, dict):
        return {str(k).lower(): str(v) for k, v in headers.items()}
    out: dict = {}
    if isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and "name" in h:
                out[str(h["name"]).lower()] = str(h.get("value", ""))
    return out


def _decode_har_content(content: dict) -> str:
    text = content.get("text", "") or ""
    if content.get("encoding") == "base64" and text:
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def _entries_from_har(har: dict) -> list:
    entries = []
    for i, e in enumerate(har.get("log", {}).get("entries", [])):
        req = e.get("request", {}) or {}
        res = e.get("response", {}) or {}
        post = req.get("postData", {}) or {}
        content = res.get("content", {}) or {}
        entries.append(
            TrafficEntry(
                index=i,
                url=req.get("url", ""),
                method=req.get("method", "GET"),
                status=res.get("status"),
                request_headers=_headers_to_dict(req.get("headers", [])),
                response_headers=_headers_to_dict(res.get("headers", [])),
                request_body=post.get("text", "") or "",
                response_body=_decode_har_content(content),
                mime_type=content.get("mimeType", "") or "",
                started_at=e.get("startedDateTime", ""),
                time_ms=e.get("time"),
            )
        )
    return entries


def _entries_from_capture(items: Iterable) -> list:
    entries = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        body = item.get("request_body") or item.get("requestBody") or item.get("body") or ""
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        resp = (item.get("response_body") or item.get("responseBody")
                or item.get("response") or "")
        if isinstance(resp, (dict, list)):
            resp = json.dumps(resp)
        entries.append(
            TrafficEntry(
                index=i,
                url=item.get("url", ""),
                method=item.get("method", "GET"),
                status=item.get("status") or item.get("status_code"),
                request_headers=_headers_to_dict(item.get("request_headers") or item.get("headers") or {}),
                response_headers=_headers_to_dict(item.get("response_headers") or {}),
                request_body=str(body),
                response_body=str(resp),
                mime_type=item.get("mime_type", "") or item.get("mimeType", ""),
                started_at=str(item.get("timestamp", "") or item.get("started_at", "")),
                time_ms=item.get("time_ms") or item.get("time"),
            )
        )
    return entries


def load_traffic(source) -> list:
    """Load traffic from a file path, JSON string, or python object."""
    if isinstance(source, (list, dict)):
        data = source
    else:
        s = str(source)
        p = Path(s)
        if p.exists():
            raw_bytes = p.read_bytes()
            if not raw_bytes.strip():
                raise ValueError(f"File is empty: {s}")
            if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
                raw = raw_bytes.decode("utf-16")
            else:
                raw = raw_bytes.decode("utf-8-sig", errors="replace")
        else:
            raw = s
        try:
            data = json.loads(raw)
        except ValueError as exc:
            preview = raw[:80].replace("\n", " ")
            raise ValueError(
                f"Could not parse input as JSON/HAR: {exc}. "
                f"Starts with: {preview!r}") from exc

    if isinstance(data, dict) and "log" in data:
        return _entries_from_har(data)
    if isinstance(data, list):
        return _entries_from_capture(data)
    if isinstance(data, dict) and "entries" in data:
        return _entries_from_capture(data["entries"])
    raise ValueError("Unrecognized traffic format: expected HAR or JSON capture array")
