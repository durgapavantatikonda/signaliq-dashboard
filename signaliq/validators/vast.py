"""VAST XML response compliance validation."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

from ..parser import TrafficEntry
from .issues import Issue, Severity

QUARTILE_EVENTS = ["start", "firstQuartile", "midpoint", "thirdQuartile", "complete"]


def _parse_vast(text: str) -> Optional[ET.Element]:
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    return root if root.tag.lower().endswith("vast") else None


def validate_vast(entry: TrafficEntry) -> list:
    root = _parse_vast(entry.response_body)
    if root is None:
        return []

    issues = []
    rid = entry.id

    def add(field, sev, msg, actual, expected):
        issues.append(Issue(field_path=field, severity=sev, message=msg,
                            actual=actual, expected=expected, request_ids=[rid]))

    ads = root.findall(".//Ad")

    root_errors = root.findall("Error")
    if root_errors and not ads:
        err_url = (root_errors[0].text or "").strip()
        code_hint = ""
        m = re.search(r"(?:error|ec|code)=(\d+)", err_url, re.I)
        if m:
            code_hint = f" (error code {m.group(1)})"
        add("VAST.Error", Severity.CRITICAL,
            f"VAST ad server returned an explicit error tracking beacon{code_hint}. "
            "The ad request reached the server but delivery failed.",
            err_url[:200] or "<Error/> present",
            "No VAST error nodes; a valid <Ad> response")

    if not ads:
        add("VAST.Ad", Severity.HIGH,
            "Empty VAST / No-Fill response - no <Ad> elements returned. "
            "The auction produced no winning creative.",
            "<VAST> with 0 <Ad> nodes",
            "At least one <Ad><InLine> or <Ad><Wrapper> element")
        return issues

    for ad in ads:
        inline = ad.find("InLine")
        if inline is None:
            continue

        impressions = inline.findall("Impression")
        has_impression = any((imp.text or "").strip() for imp in impressions)
        if not has_impression:
            add("InLine.Impression", Severity.CRITICAL,
                "Impression tracking URL missing or empty - billed impressions "
                "cannot be counted; revenue is unrecordable even if the ad plays.",
                "<Impression> absent or empty CDATA",
                "Non-empty <Impression><![CDATA[https://...]]></Impression>")

        media_files = inline.findall(".//MediaFile")
        valid_media = [mf for mf in media_files
                       if (mf.text or "").strip().startswith(("http", "//"))]
        if not valid_media:
            add("InLine.Creatives.MediaFile", Severity.CRITICAL,
                "No valid <MediaFile> with a playable video source URL. "
                "The player has nothing to render - guaranteed playback failure.",
                f"{len(media_files)} MediaFile node(s), none with valid URL",
                ">=1 <MediaFile> with valid video URL, width/height attributes")
        else:
            missing_dims = [mf for mf in valid_media
                            if not (mf.get("width") and mf.get("height"))]
            if len(missing_dims) == len(valid_media):
                add("InLine.Creatives.MediaFile.dimensions", Severity.MEDIUM,
                    "MediaFile nodes lack width/height attributes; some CTV "
                    "players reject dimensionless media files.",
                    "MediaFile without width/height",
                    "width and height attributes on MediaFile")

        tracking_events = {
            (t.get("event") or "").strip()
            for t in inline.findall(".//Tracking")
            if (t.text or "").strip()
        }
        missing_q = [q for q in QUARTILE_EVENTS if q not in tracking_events]
        if missing_q:
            add("Linear.TrackingEvents", Severity.HIGH,
                f"Missing quartile tracking events: {', '.join(missing_q)}. "
                "Completion-rate reporting and CPCV deals will under-count.",
                f"Present: {sorted(tracking_events) or 'none'}",
                "Tracking nodes for start, firstQuartile, midpoint, "
                "thirdQuartile, complete")

    return issues
