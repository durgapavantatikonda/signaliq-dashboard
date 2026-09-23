"""Audience & signal extraction: turns raw OpenRTB bid-request bodies into a
human-readable audience/device/identity profile for the dashboard.

This is additive - it does not change scoring/audit behaviour, it just reads
the same TrafficEntry objects the auditor already classified and pulls out
the "who is this ad request describing" fields.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from .classifier import Category
from .parser import TrafficEntry

_DISPLAY_CATEGORIES = {
    Category.DISPLAY.value, Category.HEADER_BIDDING.value,
    Category.IDENTITY_SYNC.value, Category.VERIFICATION.value,
    Category.CONSENT.value,
}

# Common ad-request query-param keys worth surfacing, mapped to a plain label.
# Covers Google Ad Manager/GPT plus the params most SSPs/CMPs reuse.
_DISPLAY_PARAM_LABELS = {
    "iu": "ad_unit_path", "sz": "requested_sizes", "cust_params": "targeting_params",
    "correlator": "correlator_id", "tfcd": "child_directed_flag",
    "gdpr": "gdpr_in_scope", "gdpr_consent": "gdpr_consent_string",
    "us_privacy": "us_privacy_string", "npa": "non_personalized_ads",
    "addtl_consent": "additional_consent_string", "gpp": "gpp_consent_string",
}


def _get(d: Any, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


@dataclass
class RequestAudience:
    request_id: str
    url: str
    bundle: str = ""
    app_name: str = ""
    domain: str = ""
    ifa: str = ""
    ip: str = ""
    ua: str = ""
    devicetype: Optional[int] = None
    os: str = ""
    osv: str = ""
    make: str = ""
    model: str = ""
    connectiontype: Optional[int] = None
    geo_country: str = ""
    geo_city: str = ""
    geo_lat: Optional[float] = None
    geo_lon: Optional[float] = None
    consent: str = ""
    gdpr: Optional[int] = None
    us_privacy: str = ""
    eids_providers: list = field(default_factory=list)
    schain_present: bool = False
    bidfloor: Optional[float] = None
    video_mimes: list = field(default_factory=list)
    video_size: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


_DEVICETYPE_LABELS = {
    1: "Mobile/Tablet", 2: "Personal Computer", 3: "Connected TV",
    4: "Phone", 5: "Tablet", 6: "Connected Device", 7: "Set Top Box",
    8: "OOH Device",
}


def _extract_one(entry: TrafficEntry) -> Optional[RequestAudience]:
    body = entry.request_json
    if not isinstance(body, dict) or "imp" not in body:
        return None

    device = body.get("device") or {}
    app = body.get("app") or {}
    site = body.get("site") or {}
    user = body.get("user") or {}
    regs = body.get("regs") or {}
    source = body.get("source") or {}
    geo = device.get("geo") or {}
    imps = body.get("imp") or []
    imp0 = imps[0] if imps and isinstance(imps[0], dict) else {}
    video = imp0.get("video") or {}

    eids = user.get("eids") or _get(user, "ext", "eids", default=[])
    providers = []
    if isinstance(eids, list):
        for e in eids:
            if isinstance(e, dict) and e.get("source"):
                providers.append(str(e["source"]))

    a = RequestAudience(
        request_id=entry.id,
        url=entry.url,
        bundle=str(app.get("bundle", "") or ""),
        app_name=str(app.get("name", "") or ""),
        domain=str(site.get("domain", "") or site.get("page", "") or ""),
        ifa=str(device.get("ifa", "") or ""),
        ip=str(device.get("ip", "") or device.get("ipv6", "") or ""),
        ua=str(device.get("ua", "") or ""),
        devicetype=device.get("devicetype"),
        os=str(device.get("os", "") or ""),
        osv=str(device.get("osv", "") or ""),
        make=str(device.get("make", "") or ""),
        model=str(device.get("model", "") or ""),
        connectiontype=device.get("connectiontype"),
        geo_country=str(geo.get("country", "") or ""),
        geo_city=str(geo.get("city", "") or ""),
        geo_lat=geo.get("lat"),
        geo_lon=geo.get("lon"),
        consent=str(_get(user, "ext", "consent", default="") or ""),
        gdpr=_get(regs, "ext", "gdpr"),
        us_privacy=str(_get(regs, "ext", "us_privacy", default="") or ""),
        eids_providers=providers,
        schain_present=bool(_get(source, "ext", "schain")),
        bidfloor=imp0.get("bidfloor"),
        video_mimes=list(video.get("mimes") or []),
        video_size=(f"{video.get('w')}x{video.get('h')}"
                    if video.get("w") and video.get("h") else ""),
    )
    return a


def build_audience_profile(entries: list) -> dict:
    """Aggregate every OpenRTB request in a captured session into one
    audience/device/identity profile plus the per-request breakdown.
    """
    per_request = []
    for e in entries:
        if e.category != Category.OPENRTB.value:
            continue
        a = _extract_one(e)
        if a:
            per_request.append(a)

    if not per_request:
        return {"requests_with_bid_data": 0, "per_request": [], "profile": {}}

    def most_common(vals):
        vals = [v for v in vals if v]
        if not vals:
            return ""
        return Counter(vals).most_common(1)[0][0]

    all_providers = sorted({p for a in per_request for p in a.eids_providers})
    devicetype_code = per_request[0].devicetype
    profile = {
        "app_bundle": most_common(a.bundle for a in per_request),
        "app_name": most_common(a.app_name for a in per_request),
        "site_domain": most_common(a.domain for a in per_request),
        "advertising_id": most_common(a.ifa for a in per_request),
        "advertising_id_is_zeroed": most_common(a.ifa for a in per_request)
        in ("", "00000000-0000-0000-0000-000000000000"),
        "ip_address": most_common(a.ip for a in per_request),
        "user_agent": most_common(a.ua for a in per_request),
        "device_type": _DEVICETYPE_LABELS.get(devicetype_code, "Unknown")
        if devicetype_code else "Unknown",
        "os": most_common(a.os for a in per_request),
        "os_version": most_common(a.osv for a in per_request),
        "device_make": most_common(a.make for a in per_request),
        "device_model": most_common(a.model for a in per_request),
        "geo_country": most_common(a.geo_country for a in per_request),
        "geo_city": most_common(a.geo_city for a in per_request),
        "consent_string_present": any(a.consent for a in per_request),
        "gdpr_in_scope": any(a.gdpr == 1 for a in per_request),
        "us_privacy_string": most_common(a.us_privacy for a in per_request),
        "identity_providers": all_providers,
        "identity_provider_count": len(all_providers),
        "supply_chain_declared": any(a.schain_present for a in per_request),
        "avg_bidfloor": round(
            sum(a.bidfloor for a in per_request if a.bidfloor) /
            max(1, len([a for a in per_request if a.bidfloor])), 4
        ) if any(a.bidfloor for a in per_request) else None,
        "video_sizes_seen": sorted({a.video_size for a in per_request if a.video_size}),
        "video_mime_types_seen": sorted({m for a in per_request for m in a.video_mimes}),
    }

    return {
        "requests_with_bid_data": len(per_request),
        "per_request": [a.to_dict() for a in per_request],
        "profile": profile,
    }


def build_display_signals(entries: list) -> dict:
    """Aggregate DISPLAY / HEADER_BIDDING / IDENTITY_SYNC / VERIFICATION /
    CONSENT traffic: which ad-tech vendors actually fired, what ad units and
    sizes were requested, and what consent/privacy signals were sent on
    those calls. This is the display-ad counterpart to build_audience_profile
    (which only reads OpenRTB/VAST JSON bodies - display calls are mostly
    GET requests with signal packed into the query string).
    """
    vendor_counts: Counter = Counter()
    vendor_by_group: dict = {}
    ad_units: set = set()
    sizes: set = set()
    consent_seen = {"gdpr_consent": 0, "us_privacy": 0, "gpp": 0, "npa": 0}
    per_request = []

    for e in entries:
        if not getattr(e, "vendor", ""):
            continue
        if e.category not in _DISPLAY_CATEGORIES:
            continue
        vendor_counts[e.vendor] += 1
        vendor_by_group.setdefault(e.category, set()).add(e.vendor)

        parsed = urlparse(e.url or "")
        qs = parse_qs(parsed.query)
        found = {}
        for key, label in _DISPLAY_PARAM_LABELS.items():
            if key in qs and qs[key]:
                val = qs[key][0]
                found[label] = val
                if key == "iu":
                    ad_units.add(val)
                if key == "sz":
                    sizes.update(val.split("|") if "|" in val else [val])
                if key == "gdpr_consent" and val:
                    consent_seen["gdpr_consent"] += 1
                if key == "us_privacy" and val:
                    consent_seen["us_privacy"] += 1
                if key == "gpp" and val:
                    consent_seen["gpp"] += 1
                if key == "npa":
                    consent_seen["npa"] += 1
        if found:
            per_request.append({
                "request_id": e.id, "vendor": e.vendor, "category": e.category,
                "host": parsed.netloc, "signals": found,
            })

    return {
        "vendors_detected": [
            {"vendor": v, "requests": n} for v, n in vendor_counts.most_common()
        ],
        "vendors_by_category": {
            cat: sorted(vs) for cat, vs in vendor_by_group.items()
        },
        "ad_units": sorted(ad_units),
        "sizes_requested": sorted(sizes),
        "consent_signals_seen": consent_seen,
        "per_request": per_request,
    }
