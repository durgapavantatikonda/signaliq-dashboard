"""Video ad analysis: VAST / VMAP parsing and an ad-break timeline.

Turns the traffic of a capture into a readable picture of the video ads:
which ad requests were made, whether each one was filled, the wrapper chain
behind it, the creative (duration, media files, tracking), which tracking
events actually fired, and whether it was a pre-roll, mid-roll or post-roll.

Everything here is read from captured traffic, so it is best-effort and says
how it decided each answer (position_basis / confidence) instead of
presenting guesses as facts.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

_MACRO_RE = re.compile(r"(\[[^\]]*\]|\$\{[^}]*\}|%5B.*?%5D|\{\{.*?\}\}|__[A-Z_]+__)", re.I)
# Parameter names that change on every call (cache busters, random numbers,
# timestamps...) - never useful for deciding "is this the same URL".
_DYNAMIC_PARAMS = {
    "cb", "cachebuster", "cache", "rnd", "rand", "random", "ord", "ts", "t",
    "timestamp", "time", "correlator", "r", "_", "nocache", "bust",
}

_POS_RE = re.compile(r"(pre|mid|post)[\s_\-]?roll", re.I)
_POS_VALUE_RE = re.compile(r"^(pre|mid|post)(roll)?$", re.I)
_POS_PARAM_NAMES = {"vpos", "pos", "position", "adpos", "ad_pos", "roll", "ad_type",
                    "adtype", "slot", "placement", "break", "breaktype", "ad_position"}


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None and el.text else ""


def _local(tag: str) -> str:
    """Tag name without any XML namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el: ET.Element, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _descendants(el: ET.Element, name: str) -> list:
    return [c for c in el.iter() if _local(c.tag) == name]


def _to_seconds(value: str) -> Optional[float]:
    """'00:00:15.500' -> 15.5 ; '15' -> 15.0 ; anything else -> None."""
    if not value:
        return None
    value = value.strip()
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d+))?", value)
    if m:
        h, mi, s, frac = m.groups()
        return int(h) * 3600 + int(mi) * 60 + int(s) + (float("0." + frac) if frac else 0.0)
    try:
        return float(value)
    except ValueError:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_xml(text: str) -> Optional[ET.Element]:
    """Parse untrusted XML. VAST/VMAP never need a DTD, so any document that
    declares one is refused outright (this blocks entity-expansion attacks)."""
    if not text:
        return None
    head = text[:4000].lower()
    if "<!doctype" in head or "<!entity" in head or "<!entity" in text.lower():
        return None
    try:
        return ET.fromstring(text.strip().lstrip("\ufeff"))
    except ET.ParseError:
        return None


def looks_like_vast(text: str) -> bool:
    head = (text or "").lstrip()[:3000].lower()
    return "<vast" in head


def looks_like_vmap(text: str) -> bool:
    head = (text or "").lstrip()[:3000].lower()
    return "<vmap:vmap" in head or "<vmap " in head or "<vmap>" in head


def _is_macro(value: str) -> bool:
    return bool(_MACRO_RE.search(value or ""))


def _split(url: str):
    try:
        u = urlsplit(url)
    except ValueError:
        return "", "", []
    return (u.netloc or "").lower(), (u.path or ""), parse_qsl(u.query, keep_blank_values=True)


def _media_key(url: str) -> str:
    host, path, _ = _split(url)
    return f"{host}{path}"


def _url_matches(template_url: str, seen_url: str) -> bool:
    """Does a URL the player really requested correspond to a URL listed in
    the VAST? Same host and path, and every *static* parameter of the VAST
    URL present with the same value. Parameters holding macros or changing
    values (cache busters, timestamps) are ignored, because the player
    fills those in before it fires the request."""
    th, tp, tq = _split(template_url)
    sh, sp, sq = _split(seen_url)
    if not th or th != sh or tp != sp:
        return False
    seen = dict(sq)
    for k, v in tq:
        if _is_macro(v) or k.lower() in _DYNAMIC_PARAMS:
            continue
        if seen.get(k) != v:
            return False
    return True


# ----------------------------------------------------------------------------
# VAST
# ----------------------------------------------------------------------------

def parse_vast(text: str) -> Optional[dict]:
    """Parse a VAST 2/3/4 document into plain data. None if it isn't VAST."""
    root = _safe_xml(text)
    if root is None or _local(root.tag).lower() != "vast":
        return None
    out: dict = {
        "version": root.attrib.get("version", ""),
        "ads": [],
        "errors": [_text(e) for e in _children(root, "Error") if _text(e)],
    }
    for ad_el in _children(root, "Ad"):
        ad: dict = {"id": ad_el.attrib.get("id", ""), "sequence": ad_el.attrib.get("sequence", ""),
                    "type": "", "ad_system": "", "ad_title": "", "advertiser": "",
                    "description": "", "wrapper_url": "", "impressions": [], "errors": [],
                    "creatives": []}
        body = _child(ad_el, "InLine")
        if body is not None:
            ad["type"] = "InLine"
        else:
            body = _child(ad_el, "Wrapper")
            if body is not None:
                ad["type"] = "Wrapper"
        if body is None:
            continue
        ad["ad_system"] = _text(_child(body, "AdSystem"))
        ad["ad_title"] = _text(_child(body, "AdTitle"))
        ad["advertiser"] = _text(_child(body, "Advertiser"))
        ad["description"] = _text(_child(body, "Description"))
        ad["wrapper_url"] = _text(_child(body, "VASTAdTagURI"))
        ad["impressions"] = [_text(i) for i in _children(body, "Impression") if _text(i)]
        ad["errors"] = [_text(e) for e in _children(body, "Error") if _text(e)]

        creatives_el = _child(body, "Creatives")
        for cr in (_children(creatives_el, "Creative") if creatives_el is not None else []):
            for linear in _children(cr, "Linear"):
                media = []
                mf_parent = _child(linear, "MediaFiles")
                for mf in (_children(mf_parent, "MediaFile") if mf_parent is not None else []):
                    url = _text(mf)
                    if url:
                        media.append({
                            "url": url, "type": mf.attrib.get("type", ""),
                            "delivery": mf.attrib.get("delivery", ""),
                            "bitrate": _to_float(mf.attrib.get("bitrate")),
                            "width": _to_float(mf.attrib.get("width")),
                            "height": _to_float(mf.attrib.get("height")),
                        })
                tracking: dict = {}
                te = _child(linear, "TrackingEvents")
                for t in (_children(te, "Tracking") if te is not None else []):
                    if _text(t):
                        tracking.setdefault(t.attrib.get("event", "unknown"), []).append(_text(t))
                vc = _child(linear, "VideoClicks")
                click_through = _text(_child(vc, "ClickThrough")) if vc is not None else ""
                click_tracking = ([_text(c) for c in _children(vc, "ClickTracking") if _text(c)]
                                  if vc is not None else [])
                uid = _child(cr, "UniversalAdId")
                ad["creatives"].append({
                    "kind": "linear",
                    "creative_id": cr.attrib.get("id", "") or cr.attrib.get("adId", ""),
                    "universal_ad_id": _text(uid),
                    "duration_s": _to_seconds(_text(_child(linear, "Duration"))),
                    "skip_offset": linear.attrib.get("skipoffset", ""),
                    "media_files": media,
                    "tracking": tracking,
                    "click_through": click_through,
                    "click_tracking": click_tracking,
                })
            for kind, tag in (("nonlinear", "NonLinearAds"), ("companion", "CompanionAds")):
                for node in _children(cr, tag):
                    tracking = {}
                    for t in _descendants(node, "Tracking"):
                        if _text(t):
                            tracking.setdefault(t.attrib.get("event", "unknown"), []).append(_text(t))
                    ad["creatives"].append({"kind": kind, "creative_id": cr.attrib.get("id", ""),
                                            "duration_s": None, "skip_offset": "",
                                            "media_files": [], "tracking": tracking,
                                            "click_through": "", "click_tracking": []})
        out["ads"].append(ad)
    return out


# ----------------------------------------------------------------------------
# VMAP
# ----------------------------------------------------------------------------

def _vmap_position(offset: str, break_type: str) -> str:
    o = (offset or "").strip().lower()
    if o == "start":
        return "preroll"
    if o == "end":
        return "postroll"
    secs = _to_seconds(o)
    if secs is not None and ":" in o and secs == 0:
        return "preroll"
    return "midroll"


def parse_vmap(text: str) -> Optional[list]:
    """The ad-break schedule inside a VMAP document, or None if not VMAP."""
    root = _safe_xml(text)
    if root is None or _local(root.tag).lower() != "vmap":
        return None
    breaks = []
    for b in _children(root, "AdBreak"):
        offset = b.attrib.get("timeOffset", "")
        src = _child(b, "AdSource")
        tag_uri = ""
        inline_vast = False
        if src is not None:
            tag_uri = _text(_child(src, "AdTagURI"))
            if not tag_uri:
                for name in ("VASTAdData", "VASTData"):
                    node = _child(src, name)
                    if node is not None:
                        inline_vast = True
        breaks.append({
            "break_id": b.attrib.get("breakId", ""),
            "break_type": b.attrib.get("breakType", ""),
            "time_offset": offset,
            "position": _vmap_position(offset, b.attrib.get("breakType", "")),
            "adtag_uri": tag_uri,
            "has_inline_vast": inline_vast,
        })
    return breaks


# ----------------------------------------------------------------------------
# position of an ad request
# ----------------------------------------------------------------------------

def _position_from_url(url: str) -> Optional[str]:
    """pre/mid/post-roll when the request URL itself says so (for example
    vpos=preroll, ad_type=midroll, or /postroll/ in the path)."""
    host, path, query = _split(url)
    m = _POS_RE.search(path)
    if m:
        return m.group(1).lower() + "roll"
    for k, v in query:
        vv = v.strip().lower()
        if k.lower() in _POS_PARAM_NAMES and _POS_VALUE_RE.match(vv):
            return _normalise_pos(vv)
        m = _POS_RE.search(vv)
        if m:
            return m.group(1).lower() + "roll"
    return None


def _normalise_pos(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    p = p.lower()
    if p.startswith("pre"):
        return "preroll"
    if p.startswith("mid"):
        return "midroll"
    if p.startswith("post"):
        return "postroll"
    return None


def _clock(sec: float) -> str:
    sec = max(0, int(sec))
    m, s_ = divmod(sec, 60)
    return f"{m}:{s_:02d}"


def _position_from_timeline(t: Optional[float], points: list):
    """Label an ad request by where the video was when it fired.

    `points` are readings of the video's position over time (sampled about
    once a second while watching, plus the moment of every jump the capture
    made). The latest reading before the request tells us whether the video
    was still at the start (pre-roll), part-way through (mid-roll) or at the
    end (post-roll)."""
    if t is None:
        return None, None
    pts = sorted((p for p in points if p.get("t") and p.get("content_time_s") is not None),
                 key=lambda p: p["t"])
    if not pts:
        return None, None
    last = None
    for p in pts:
        if p["t"] <= t:
            last = p
    if last is None:
        return "preroll", "it fired before the capture started playing the video"
    c = float(last["content_time_s"] or 0)
    d = last.get("duration_s")
    where = _clock(c) + (f" of {_clock(d)}" if d and d > 0 else "")
    gap = t - last["t"]
    caveat = (f" (the last position reading was {int(gap)}s earlier, so this is less certain)"
              if gap > 6 else "")
    if last.get("ended") or (d and d > 0 and c >= d - 3):
        return "postroll", f"the video was at its end ({where}) when this was requested{caveat}"
    if c < 1.5:
        return "preroll", f"the video was still at the very start ({where}) when this was requested{caveat}"
    return "midroll", f"the video was at {where} when this was requested{caveat}"


# ----------------------------------------------------------------------------
# the analysis
# ----------------------------------------------------------------------------

def _entry_time(entry) -> Optional[float]:
    return _to_float(getattr(entry, "started_at", None))


def _rel(t: Optional[float], start: Optional[float]) -> Optional[float]:
    if t is None or start is None:
        return None
    return round(max(0.0, t - start), 2)


def _no_fill_reason(parsed_final: Optional[dict], chain_recs: list) -> str:
    if parsed_final is None:
        return "the response was empty or unreadable (for example HTTP 204)"
    if not parsed_final["ads"]:
        return "empty VAST: the ad server had no ad to show (no fill)"
    if any(ad["type"] == "Wrapper" for ad in parsed_final["ads"]):
        return "the wrapper chain did not lead to an ad (the next hop was never seen or returned nothing)"
    return "the VAST had no playable video creative"


def analyze_video_ads(entries: list, meta: Optional[dict] = None) -> dict:
    """Build the video-ad report for a captured session."""
    meta = meta or {}
    rec_start = _to_float(meta.get("recording_started_at"))
    phases = meta.get("video_phases") or []
    # Readings of the video's position over time: the ~1/second samples plus
    # the exact moment of every jump the capture made.
    timeline_points = list(meta.get("video_samples") or []) + [
        {"t": ph.get("t"), "content_time_s": ph.get("content_time_s"), "duration_s": ph.get("duration_s")}
        for ph in phases if ph.get("content_time_s") is not None]

    # --- 1. find every VAST / VMAP document in the traffic ---------------
    records: list = []
    vmaps: list = []
    for e in entries:
        body = getattr(e, "response_body", "") or ""
        url = getattr(e, "url", "") or ""
        t = _entry_time(e)
        base = {"request_id": e.id, "url": url, "vendor": getattr(e, "vendor", "") or "",
                "status": getattr(e, "status", None), "page": getattr(e, "page", "") or "",
                "t": t, "offset_s": _rel(t, rec_start)}
        if body and looks_like_vmap(body):
            br = parse_vmap(body)
            if br is not None:
                vmaps.append({**base, "breaks": br})
                continue
        if body and looks_like_vast(body):
            parsed = parse_vast(body)
            if parsed is not None:
                records.append({**base, "kind": "vast", "parsed": parsed})
                continue
        if getattr(e, "category", "") == "VAST" and not body:
            # A VAST request that returned nothing readable (HTTP 204 / empty).
            records.append({**base, "kind": "vast", "parsed": None})

    # --- 2. link wrappers to the requests they point at -------------------
    for r in records:
        r["child"] = None
        r["parent"] = None
    for r in records:
        p = r["parsed"]
        if not p:
            continue
        for ad in p["ads"]:
            if ad["type"] == "Wrapper" and ad["wrapper_url"]:
                for other in records:
                    if other is r or other["parent"] is not None:
                        continue
                    if _url_matches(ad["wrapper_url"], other["url"]) or _url_matches(other["url"], ad["wrapper_url"]):
                        if other["t"] is None or r["t"] is None or other["t"] >= r["t"] - 0.01:
                            r["child"] = other
                            other["parent"] = r
                            break

    roots = [r for r in records if r["parent"] is None]

    # --- 3. every URL the page actually requested (for "did it fire?") ----
    seen = [(getattr(e, "url", ""), _entry_time(e)) for e in entries]

    def first_fired(template: str, after: Optional[float]):
        for u, t in seen:
            if _url_matches(template, u) and (after is None or t is None or t >= after - 0.5):
                return t if t is not None else 0.0
        return None

    # --- 4. VMAP schedule lookups -----------------------------------------
    vmap_tags = []
    for v in vmaps:
        for b in v["breaks"]:
            if b["adtag_uri"]:
                vmap_tags.append((b["adtag_uri"], b))

    # --- 5. build one result per root ad request --------------------------
    ad_requests: list = []
    first_vast_request = min((r["t"] for r in roots if r["t"] is not None), default=None)
    for r in roots:
        chain_recs = []
        cur = r
        while cur is not None and len(chain_recs) < 10:
            chain_recs.append(cur)
            cur = cur["child"]
        final = chain_recs[-1]
        parsed_final = final["parsed"]

        chain = []
        impressions: list = []
        errors: list = []
        tracking: dict = {}
        for c in chain_recs:
            cp = c["parsed"]
            ads = cp["ads"] if cp else []
            chain.append({
                "request_id": c["request_id"], "url": c["url"], "status": c["status"],
                "type": ads[0]["type"] if ads else "Empty",
                "ad_system": ads[0]["ad_system"] if ads else "",
            })
            for ad in ads:
                impressions += ad["impressions"]
                errors += ad["errors"]
                for cr in ad["creatives"]:
                    for ev, urls in cr["tracking"].items():
                        tracking.setdefault(ev, []).extend(urls)
            if cp:
                errors += cp["errors"]

        inline_ads = [ad for ad in (parsed_final["ads"] if parsed_final else []) if ad["type"] == "InLine"]
        filled = bool(inline_ads and any(cr["kind"] == "linear" and cr["media_files"]
                                         for ad in inline_ads for cr in ad["creatives"])
                      or any(cr["kind"] != "linear" for ad in inline_ads for cr in ad["creatives"]))

        # events: which VAST-listed URLs really fired
        events = []
        t0 = r["t"]
        names = ["impression", "start", "firstQuartile", "midpoint", "thirdQuartile",
                 "complete", "skip", "mute", "unmute", "pause", "resume", "fullscreen", "error"]
        all_events = {"impression": impressions, **tracking}
        if "error" not in all_events and errors:
            all_events["error"] = errors
        for name in names + sorted(k for k in all_events if k not in names):
            urls = all_events.get(name) or []
            if not urls:
                continue
            fired_t = None
            for u in urls:
                ft = first_fired(u, t0)
                if ft is not None and (fired_t is None or ft < fired_t):
                    fired_t = ft
            events.append({"event": name, "listed": len(urls), "fired": fired_t is not None,
                           "fired_offset_s": _rel(fired_t, rec_start) if fired_t else None})

        # creatives with "was the media file actually requested?"
        creatives_out = []
        for ad in inline_ads:
            for cr in ad["creatives"]:
                if cr["kind"] != "linear":
                    continue
                mfs = []
                for mf in cr["media_files"]:
                    key = _media_key(mf["url"])
                    req_t = None
                    for u, t in seen:
                        if _media_key(u) == key:
                            req_t = t if t is not None else 0.0
                            break
                    mfs.append({**mf, "requested": req_t is not None,
                                "requested_offset_s": _rel(req_t, rec_start) if req_t else None})
                creatives_out.append({
                    "ad_system": ad["ad_system"], "ad_title": ad["ad_title"], "advertiser": ad["advertiser"],
                    "ad_id": ad["id"], "creative_id": cr["creative_id"], "universal_ad_id": cr["universal_ad_id"],
                    "duration_s": cr["duration_s"], "skip_offset": cr["skip_offset"],
                    "click_through": cr["click_through"], "media_files": mfs,
                })

        # --- position -----------------------------------------------------
        position, basis, confidence = None, "", ""
        for tag, b in vmap_tags:
            if _url_matches(tag, r["url"]) or _url_matches(r["url"], tag):
                position, basis, confidence = b["position"], f"listed in the VMAP schedule (offset {b['time_offset']})", "certain"
                break
        if position is None:
            pu = _normalise_pos(_position_from_url(r["url"]))
            if pu:
                position, basis, confidence = pu, "named in the ad request URL", "high"
        if position is None:
            pp, why = _position_from_timeline(r["t"], timeline_points)
            if pp:
                position, basis, confidence = pp, why, "medium"
        if position is None:
            if len(roots) == 1 or (first_vast_request is not None and r["t"] == first_vast_request):
                position, basis, confidence = "preroll", "first video ad request of the session (a guess)", "low"
            else:
                position, basis, confidence = "unknown", "nothing in the request or timing says where it plays", "low"

        ad_requests.append({
            "request_id": r["request_id"], "url": r["url"], "vendor": r["vendor"], "status": r["status"],
            "page": r["page"], "offset_s": r["offset_s"], "t": r["t"],
            "position": position, "position_basis": basis, "confidence": confidence,
            "filled": filled, "chain": chain, "creatives": creatives_out, "events": events,
            "errors": errors[:10],
            "note": "" if filled else _no_fill_reason(parsed_final, chain_recs),
        })

    ad_requests.sort(key=lambda a: (a["t"] is None, a["t"] or 0))
    by_position: dict = {}
    for a in ad_requests:
        by_position[a["position"]] = by_position.get(a["position"], 0) + 1

    timeline = [{"offset_s": a["offset_s"], "position": a["position"], "filled": a["filled"],
                 "request_id": a["request_id"]} for a in ad_requests]
    phases_out = [{"phase": p.get("phase"), "offset_s": _rel(_to_float(p.get("t")), rec_start),
                   "content_time_s": p.get("content_time_s")} for p in phases]

    notes = []
    if ad_requests and not timeline_points:
        notes.append("The capture did not play the page's video, so pre/mid/post-roll labels come from the "
                     "request URLs and VMAP only, or are guesses.")
    # A short watch time can only test some cue points of the video.
    per_page: dict = {}
    for ph in phases:
        if ph.get("phase") != "start":
            per_page[ph.get("page")] = per_page.get(ph.get("page"), 0) + 1
    short = [n for n in per_page.values() if 0 < n < 4]
    if ad_requests and short:
        notes.append(f"Only {min(short)} cue point(s) of the video were tested because the watch time was short, so "
                     "mid-roll breaks at other points may have been missed. Use 90 seconds or more of "
                     "Video ads watch for a full check.")
    if ad_requests:
        notes.append("'Fired' means a request matching a URL listed in the VAST was seen from the browser. "
                     "URLs with changing values are matched on their stable parts, so treat it as strong "
                     "evidence rather than proof.")

    return {
        "has_video_ads": bool(ad_requests or vmaps),
        "summary": {
            "ad_requests": len(ad_requests),
            "filled": sum(1 for a in ad_requests if a["filled"]),
            "no_fill": sum(1 for a in ad_requests if not a["filled"]),
            "by_position": by_position,
            "creatives": sum(len(a["creatives"]) for a in ad_requests),
            "vmap_breaks": sum(len(v["breaks"]) for v in vmaps),
        },
        "vmaps": [{"request_id": v["request_id"], "url": v["url"], "offset_s": v["offset_s"],
                   "breaks": v["breaks"]} for v in vmaps],
        "ad_requests": ad_requests,
        "timeline": timeline,
        "phases": phases_out,
        "recording_started_at": rec_start,
        "notes": notes,
    }