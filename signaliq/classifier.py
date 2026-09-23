"""Ad-tech traffic classification."""

from __future__ import annotations

from enum import Enum
from urllib.parse import urlparse

from .parser import TrafficEntry
from .vendors import match_vendor


class Category(str, Enum):
    OPENRTB = "OpenRTB"
    VAST = "VAST"
    SSAI = "SSAI"
    DISPLAY = "Display Ad"
    HEADER_BIDDING = "Header Bidding"
    IDENTITY_SYNC = "Identity Sync"
    VERIFICATION = "Verification"
    CONSENT = "Consent/CMP"
    TRACKING = "Tracking Pixel"
    MEDIA = "Media"
    API = "API"
    OTHER = "Other"


_VENDOR_GROUP_TO_CATEGORY = {
    "Display Ad Server": Category.DISPLAY,
    "Header Bidding": Category.HEADER_BIDDING,
    "Identity Sync": Category.IDENTITY_SYNC,
    "Verification": Category.VERIFICATION,
    "Consent/CMP": Category.CONSENT,
}


SSAI_DOMAINS = ("dai.google.com", "yospace.com", "mediatailor", "freewheel.tv", "ssai")
MEDIA_EXTENSIONS = (".m3u8", ".mp4", ".ts", ".mpd", ".m4s")
TRACKING_TOKENS = (
    "/track", "/imp", "/click", "beacon", "quartile", "impression",
    "/start", "/complete", "/firstquartile", "/midpoint", "/thirdquartile",
)


def _is_openrtb(entry: TrafficEntry, url_l: str) -> bool:
    if "/openrtb" in url_l or "/bid" in url_l:
        return True
    if "doubleclick" in url_l and "openrtb" in url_l:
        return True
    body = entry.request_json
    if isinstance(body, dict) and "imp" in body and ("app" in body or "site" in body):
        return True
    return False


def _is_vast(entry: TrafficEntry, url_l: str) -> bool:
    if "output=vast" in url_l or "output=xml_vast" in url_l:
        return True
    resp = (entry.response_body or "").lstrip()[:2000].lower()
    return "<vast" in resp


def classify_entry(entry: TrafficEntry) -> Category:
    url_l = (entry.url or "").lower()
    parsed = urlparse(url_l)
    host = parsed.netloc
    path = parsed.path

    if _is_openrtb(entry, url_l):
        return Category.OPENRTB
    if _is_vast(entry, url_l):
        return Category.VAST
    if any(d in host for d in SSAI_DOMAINS):
        return Category.SSAI
    vendor_match = match_vendor(host)
    if vendor_match:
        group, _name = vendor_match
        return _VENDOR_GROUP_TO_CATEGORY.get(group, Category.OTHER)
    if any(path.endswith(ext) for ext in MEDIA_EXTENSIONS):
        return Category.MEDIA
    if any(tok in url_l for tok in TRACKING_TOKENS):
        return Category.TRACKING
    if "api/" in path or "/v1/" in path or "/v2/" in path:
        return Category.API
    return Category.OTHER


def classify_all(entries: list) -> list:
    for e in entries:
        e.category = classify_entry(e).value
        parsed = urlparse((e.url or "").lower())
        vm = match_vendor(parsed.netloc)
        e.vendor = vm[1] if vm else ""
    return entries
