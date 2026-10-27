"""Ad-tech traffic classification."""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlparse

from .parser import TrafficEntry
from .vendors import match_vendor


class Category(str, Enum):
    OPENRTB = "OpenRTB"
    VAST = "VAST"
    SSAI = "SSAI"
    DISPLAY = "Display Ad"
    AD_OTHER = "Other Ad"
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


# Heuristics for ad requests from vendors that aren't in vendors.py (regional
# networks, or a publisher's own first-party ad server). Deliberately
# conservative: whole host labels / path segments / specific parameters, not
# loose substrings, so ordinary pages like "uploads.x.com" or "/loading"
# don't match.
_AD_HOST_RE = re.compile(
    r"(^|\.)(ads?|adserver|adservice|adserv|adsystem|adx|advert[a-z]*|adtech|"
    r"adnetwork|banners?|rtb|bidder|prebid|dsp|ssp)\d*\.", re.I)
_AD_PATH_RE = re.compile(
    r"(gampad|pagead|adserver|adserve|adframe|adunit|adslot|adsense|prebid|"
    r"openrtb|vmap)|(^|/)(ads?|adv|adverts?|advertising|banners?|dfp)(/|\.|$)", re.I)
_AD_PARAM_RE = re.compile(
    r"(^|&)(adunit|ad_unit|adslot|ad_slot|adid|ad_id|adformat|ad_type|gdfp_req|"
    r"bidder|bidid|placementid|placement_id|zoneid|zone_id|pubid|publisher_id)=|"
    r"(^|&)iu=/", re.I)


def _looks_like_ad(host: str, path: str, query: str) -> bool:
    return bool(_AD_HOST_RE.search(host) or _AD_PATH_RE.search(path)
                or _AD_PARAM_RE.search(query))


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
    return "<vast" in resp or "<vmap:vmap" in resp or "<vmap " in resp


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
    if _looks_like_ad(host, path, parsed.query):
        return Category.AD_OTHER
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