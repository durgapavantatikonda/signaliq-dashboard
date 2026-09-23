"""OpenRTB bid-request compliance validation."""

from __future__ import annotations

import re
import uuid
from typing import Any

from ..parser import TrafficEntry
from .issues import Issue, Severity

TCF_STRING_RE = re.compile(r"^[A-Za-z0-9\-_\.~]{20,}$")


def _get(d: Any, path: str) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _is_valid_ifa(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if value.strip("0-") == "":
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return len(value) >= 16


def validate_openrtb(entry: TrafficEntry) -> list:
    body = entry.request_json
    if not isinstance(body, dict):
        return []

    issues = []
    rid = entry.id
    is_app = "app" in body

    def add(field, sev, msg, actual, expected):
        issues.append(Issue(
            field_path=field, severity=sev, message=msg,
            actual="" if actual is None else str(actual),
            expected=expected, request_ids=[rid],
        ))

    if is_app:
        bundle = _get(body, "app.bundle")
        if not bundle:
            add("app.bundle", Severity.CRITICAL,
                "App bundle identifier missing for app/CTV traffic. DSPs cannot "
                "match the supply to an app-ads.txt entry, blocking bids.",
                bundle, "Reverse-domain bundle ID, e.g. com.viki.android")

    ifa = _get(body, "device.ifa")
    if is_app and not _is_valid_ifa(ifa):
        add("device.ifa", Severity.CRITICAL,
            "Identifier for Advertising (IFA) missing or zeroed. Bids without "
            "IFA are auto-rejected by most DSPs on CTV/app inventory.",
            ifa, "UUID-format advertising identifier in device.ifa")

    gdpr = _get(body, "regs.ext.gdpr")
    if gdpr is not None and gdpr not in (0, 1, "0", "1"):
        add("regs.ext.gdpr", Severity.MEDIUM,
            "GDPR applicability flag must be 0 or 1.", gdpr, "0 or 1")

    if gdpr in (1, "1"):
        consent = _get(body, "user.ext.consent")
        if not consent or not TCF_STRING_RE.match(str(consent)):
            add("user.ext.consent", Severity.CRITICAL,
                "IAB TCF Consent String is empty or malformed in a GDPR-enforced "
                "request. Buyers will drop bids - IAB TCF Error 2.1a.",
                consent, "Valid base64-encoded IAB TCF v2 consent string")

    schain = _get(body, "source.ext.schain")
    if not schain:
        add("source.ext.schain", Severity.HIGH,
            "Supply-chain (schain) transparency object missing. Buyers enforcing "
            "sellers.json/schain validation will reduce or drop bids.",
            schain, "Populated source.ext.schain object with complete=1 nodes")

    eids = _get(body, "user.ext.eids")
    if not eids:
        add("user.ext.eids", Severity.HIGH,
            "No alternative identity signals (UID2, LiveRamp RampID, etc.). "
            "Cookieless buyers cannot target, reducing bid density.",
            eids, "Non-empty user.ext.eids array")

    for path, sev in (("device.make", Severity.HIGH),
                      ("device.model", Severity.HIGH),
                      ("device.os", Severity.MEDIUM)):
        val = _get(body, path)
        if not val:
            add(path, sev,
                f"{path} not populated - programmatic buyers use device hardware "
                "specs for CTV targeting and fraud filtering.",
                val, "Populated device hardware field")

    ua = _get(body, "device.ua")
    ip = _get(body, "device.ip") or _get(body, "device.ipv6")
    if not ua or not ip:
        missing = [n for n, v in (("device.ua", ua), ("device.ip", ip)) if not v]
        add("device.ua/device.ip", Severity.HIGH,
            f"Missing {' and '.join(missing)} - required for geo/fraud scoring.",
            "", "Populated device.ua and device.ip")

    us_privacy = _get(body, "regs.ext.us_privacy")
    if us_privacy is None and gdpr is None:
        add("regs.ext.us_privacy/regs.ext.gdpr", Severity.MEDIUM,
            "No privacy regulation signals (us_privacy or gdpr) present in regs.ext.",
            "", "regs.ext.us_privacy and/or regs.ext.gdpr populated")

    return issues
