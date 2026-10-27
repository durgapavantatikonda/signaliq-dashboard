"""Bidding analysis: for every ad slot and every refresh, who was asked to bid,
what each bidder answered, and who won.

Two sources, best first:

1. PAGE EVENTS (exact). The capture listens, from inside the page, to the
   events Prebid.js (header bidding) and Google Publisher Tag (the ad server)
   publish about themselves: every auction, every bidder request, every bid
   or no-bid or timeout, every win, and every slot render. Nothing is guessed.

2. NETWORK TRAFFIC (approximate). When the page doesn't expose those events
   (a custom wrapper, server-side auctions), the Ad Manager request's targeting
   (hb_bidder / hb_pb) and the bidder requests made just before it are used to
   reconstruct each refresh. It says plainly that it is a reconstruction.

Every result says how it knows (`basis`), so a guess is never shown as a fact.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

BIDDER_NAMES = {
    "appnexus": "AppNexus / Xandr", "rubicon": "Magnite (Rubicon)", "magnite": "Magnite",
    "pubmatic": "PubMatic", "ix": "Index Exchange", "openx": "OpenX", "criteo": "Criteo",
    "triplelift": "TripleLift", "sovrn": "Sovrn", "sharethrough": "Sharethrough", "33across": "33Across",
    "gumgum": "GumGum", "teads": "Teads", "smartadserver": "Smart AdServer (Equativ)", "adform": "Adform",
    "ttd": "The Trade Desk", "thetradedesk": "The Trade Desk", "amazon": "Amazon", "improvedigital": "Improve Digital",
    "conversant": "Epsilon Conversant", "medianet": "Media.net", "onetag": "OneTag", "yahoossp": "Yahoo SSP",
    "kargo": "Kargo", "unruly": "Unruly", "richaudience": "Rich Audience", "adyoulike": "Adyoulike",
    "sonobi": "Sonobi", "districtm": "District M", "emx_digital": "EMX Digital", "nextmillennium": "Next Millennium",
    "yieldmo": "Yieldmo", "mgid": "MGID", "taboola": "Taboola", "outbrain": "Outbrain", "seedtag": "Seedtag",
    "undertone": "Undertone", "lkqd": "LKQD", "spotx": "SpotX", "freewheel-ssp": "FreeWheel SSP", "ogury": "Ogury",
    "adagio": "Adagio", "connectad": "ConnectAd", "visx": "visx", "vidazoo": "Vidazoo", "minutemedia": "Minute Media",
}
AD_SERVER = "Google Ad Manager"


def friendly(code: Optional[str]) -> str:
    code = code or "unknown"
    return BIDDER_NAMES.get(code.lower(), code)


def _sec(ms: Any) -> Optional[float]:
    try:
        return float(ms) / 1000.0 if ms else None
    except (TypeError, ValueError):
        return None


def _off(t: Optional[float], rec_start: Optional[float]) -> Optional[float]:
    if t is None or not rec_start:
        return None
    return round(max(0.0, t - rec_start), 1)


def _num(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. from the page's own events
# ---------------------------------------------------------------------------

def _cycle_from_auction(a: dict, unit: str, gevs: list, rec_start: Optional[float]) -> dict:
    asked: dict = {}
    for r in a["requests"]:
        if unit in (r.get("units") or []):
            asked.setdefault(r.get("bidder") or "unknown", []).append(r)
    bid_requests = sum(len(v) for v in asked.values())

    resp: dict = {}
    for b in a["bids"]:
        if b.get("adUnitCode") == unit and b.get("bidder"):
            resp.setdefault(b["bidder"], []).append(b)
    no_bid = {x.get("bidder") for x in a["no_bids"] if x.get("adUnitCode") == unit}
    timeouts = {x.get("bidder") for x in a["timeouts"] if x.get("adUnitCode") == unit}
    rejected = {x.get("bidder"): x.get("reason") for x in a["rejected"] if x.get("adUnitCode") == unit}
    won = [w for w in a["won"] if w.get("adUnitCode") == unit]
    winner_bidder = won[0].get("bidder") if won else None

    codes = list(dict.fromkeys(list(asked) + list(resp) + list(no_bid) + list(timeouts) + list(rejected)))
    rows = []
    for code in codes:
        bs = resp.get(code) or []
        best = max(bs, key=lambda x: x.get("cpm") or 0) if bs else None
        if best is not None:
            status = "won" if code == winner_bidder else "bid"
        elif code in no_bid:
            status = "no bid"
        elif code in timeouts:
            status = "timeout"
        elif code in rejected:
            status = "rejected"
        else:
            status = "no answer recorded"
        rows.append({
            "bidder": code, "name": friendly(code), "status": status,
            "cpm": best.get("cpm") if best else None, "currency": (best or {}).get("currency"),
            "size": (best or {}).get("size"), "response_ms": (best or {}).get("ttr"),
            "deal": (best or {}).get("dealId"), "advertiser": (best or {}).get("advertiser"),
            "reason": rejected.get(code),
        })
    rows.sort(key=lambda r: (r["cpm"] is None, -(r["cpm"] or 0)))
    priced = [r for r in rows if r["cpm"] is not None]
    top = priced[0] if priced else None

    requested = next((g for g in gevs if g.get("e") == "slotRequested"), None)
    render = next((g for g in gevs if g.get("e") == "slotRenderEnded"), None)
    viewable = any(g.get("e") == "impressionViewable" for g in gevs)
    sent = ((requested or {}).get("targeting") or {}) or ((render or {}).get("targeting") or {})

    if won:
        w = won[0]
        winner = {"who": friendly(winner_bidder), "code": winner_bidder, "kind": "header_bidding",
                  "cpm": w.get("cpm"), "currency": w.get("currency"),
                  "detail": "The header-bidding bid was served in the slot.",
                  "basis": "Prebid reported this bid as the winner (its bidWon event)"}
    elif render is not None and not render.get("isEmpty"):
        who = AD_SERVER + (" (Open Bidding / AdX)" if render.get("yieldCompanyIds") else " (direct / other demand)")
        detail = f"Ad Manager served line item {render.get('lineItemId')}" if render.get("lineItemId") else "Ad Manager served an ad"
        if top:
            detail += (f". Header bidding's best bid was {top['name']} at {top['cpm']:.2f} "
                       f"{top.get('currency') or ''}".rstrip() + ", which did not win the slot")
        winner = {"who": who, "code": None, "kind": "ad_server", "cpm": None, "currency": None,
                  "detail": detail,
                  "basis": "Ad Manager rendered a creative but Prebid never reported a win"}
    elif render is not None:
        winner = {"who": "No ad served", "code": None, "kind": "none", "cpm": None, "currency": None,
                  "detail": "The slot came back empty." + (f" Header bidding's best bid was {top['name']} at {top['cpm']:.2f}." if top else ""),
                  "basis": "Ad Manager reported the slot as empty"}
    elif top:
        winner = {"who": top["name"], "code": top["bidder"], "kind": "highest_bid", "cpm": top["cpm"],
                  "currency": top.get("currency"),
                  "detail": "Highest bid in the auction. The ad server's result for this refresh was not visible, "
                            "so it is not confirmed that it was served.",
                  "basis": "highest bid in the auction (no ad-server result seen)"}
    else:
        winner = {"who": "No bids", "code": None, "kind": "none", "cpm": None, "currency": None,
                  "detail": "Nobody returned a bid.", "basis": "no bids were received"}

    t0 = a["t"]
    return {
        "auction_id": a["id"], "t": t0, "offset_s": _off(t0, rec_start), "page": a.get("page", ""),
        "bid_requests": bid_requests, "bidders_asked": len(asked) or len(codes),
        "bids": len(priced), "no_bids": sum(1 for r in rows if r["status"] == "no bid"),
        "timeouts": sum(1 for r in rows if r["status"] == "timeout"),
        "rejected": sum(1 for r in rows if r["status"] == "rejected"),
        "no_answer": sum(1 for r in rows if r["status"] == "no answer recorded"),
        "rows": rows, "winner": winner,
        "sent_to_ad_server": {k: v for k, v in sent.items() if k.startswith("hb_") or k.startswith("amzn")},
        "render": ({"line_item_id": render.get("lineItemId"), "creative_id": render.get("creativeId"),
                    "advertiser_id": render.get("advertiserId"), "is_empty": bool(render.get("isEmpty")),
                    "size": render.get("size"), "viewable": viewable} if render else None),
        "source": "page",
    }


def _from_events(events: list, rec_start: Optional[float]) -> dict:
    pb = [e for e in events if e.get("src") == "pb"]
    gpt = [e for e in events if e.get("src") == "gpt"]

    auctions: dict = {}
    for e in sorted(pb, key=lambda x: x.get("t") or 0):
        aid = e.get("auctionId")
        if not aid:
            continue
        a = auctions.setdefault(aid, {"id": aid, "t": None, "units": set(), "requests": [], "bids": [], "no_bids": [],
                                      "timeouts": [], "rejected": [], "won": [], "page": e.get("page", "")})
        t = _sec(e.get("t"))
        if t is not None:
            a["t"] = t if a["t"] is None else min(a["t"], t)
        et = e.get("e")
        if et in ("auctionInit", "auctionEnd"):
            a["units"] |= {u for u in (e.get("adUnitCodes") or []) if u}
        elif et == "bidRequested":
            a["requests"].append(e)
            a["units"] |= {u for u in (e.get("units") or []) if u}
        elif et == "bidResponse":
            a["bids"].append(e)
            if e.get("adUnitCode"):
                a["units"].add(e["adUnitCode"])
        elif et == "noBid":
            a["no_bids"].append(e)
        elif et == "bidTimeout":
            a["timeouts"].append(e)
        elif et == "bidRejected":
            a["rejected"].append(e)
        elif et == "bidWon":
            a["won"].append(e)
            if e.get("adUnitCode"):
                a["units"].add(e["adUnitCode"])

    gpt_sorted = sorted(gpt, key=lambda g: g.get("t") or 0)
    by_slot: dict = {}
    for g in gpt_sorted:
        for key in {g.get("slot"), g.get("adUnit")}:
            if key:
                by_slot.setdefault(key, []).append(g)

    # every auction, per ad unit, in time order (that order IS the refresh order)
    per_unit: dict = {}
    for a in auctions.values():
        for u in a["units"]:
            per_unit.setdefault(u, []).append(a)
    for u in per_unit:
        per_unit[u].sort(key=lambda a: a["t"] or 0)

    used_gpt: set = set()
    slots: dict = {}
    for unit, alist in per_unit.items():
        for i, a in enumerate(alist):
            start = (a["t"] or 0) - 1.0
            end = alist[i + 1]["t"] if i + 1 < len(alist) else float("inf")
            window = [g for g in by_slot.get(unit, []) if start <= (_sec(g.get("t")) or 0) < (end or float("inf"))]
            cyc = _cycle_from_auction(a, unit, window, rec_start)
            for g in window:
                used_gpt.add(id(g))
            slots.setdefault(unit, {"slot": unit, "ad_unit": None, "cycles": []})["cycles"].append(cyc)

    # Ad Manager renders with no header-bidding auction behind them
    for g in gpt_sorted:
        if g.get("e") != "slotRenderEnded" or id(g) in used_gpt:
            continue
        key = g.get("adUnit") if g.get("adUnit") in per_unit else (g.get("slot") or g.get("adUnit") or "unknown")
        t = _sec(g.get("t"))
        empty = bool(g.get("isEmpty"))
        who = "No ad served" if empty else AD_SERVER + (" (Open Bidding / AdX)" if g.get("yieldCompanyIds") else " (direct / other demand)")
        slots.setdefault(key, {"slot": key, "ad_unit": None, "cycles": []})["cycles"].append({
            "auction_id": None, "t": t, "offset_s": _off(t, rec_start), "page": g.get("page", ""),
            "bid_requests": 0, "bidders_asked": 0, "bids": 0, "no_bids": 0, "timeouts": 0, "rejected": 0,
            "no_answer": 0, "rows": [],
            "winner": {"who": who, "code": None, "kind": "none" if empty else "ad_server", "cpm": None, "currency": None,
                       "detail": ("The slot came back empty." if empty else
                                  f"Ad Manager served line item {g.get('lineItemId')}." if g.get("lineItemId") else "Ad Manager served an ad."),
                       "basis": "Ad Manager's render event; no header-bidding auction was recorded for this refresh"},
            "sent_to_ad_server": {k: v for k, v in (g.get("targeting") or {}).items()},
            "render": {"line_item_id": g.get("lineItemId"), "creative_id": g.get("creativeId"),
                       "advertiser_id": g.get("advertiserId"), "is_empty": empty, "size": g.get("size"), "viewable": False},
            "source": "page",
        })

    for key, sl in slots.items():
        sl["cycles"].sort(key=lambda c: (c["t"] is None, c["t"] or 0))
        path = next((g.get("adUnit") for g in by_slot.get(key, []) if g.get("adUnit")), None)
        sl["ad_unit"] = path if path != key else None
    return {"slots": list(slots.values())}


# ---------------------------------------------------------------------------
# 2. reconstructed from network traffic
# ---------------------------------------------------------------------------

_HB_KEYS = ("hb_bidder", "hb_pb", "hb_adid", "hb_size", "hb_source", "hb_deal")


def _max_price(obj: Any) -> Optional[float]:
    best = None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("price", "cpm", "bid_price", "bidprice") and isinstance(v, (int, float)) and not isinstance(v, bool):
                best = max(best if best is not None else 0.0, float(v))
            else:
                sub = _max_price(v)
                if sub is not None:
                    best = max(best if best is not None else 0.0, sub)
    elif isinstance(obj, list):
        for v in obj:
            sub = _max_price(v)
            if sub is not None:
                best = max(best if best is not None else 0.0, sub)
    return best


def _bid_from_body(body: str, status: Any) -> tuple[str, Optional[float]]:
    if status == 204 or not (body or "").strip():
        return "no bid", None
    try:
        price = _max_price(json.loads(body))
    except (ValueError, TypeError):
        return "no answer recorded", None
    if price is None or price <= 0:
        return "no bid", None
    return "bid", price


def _ts(entry: Any) -> Optional[float]:
    try:
        return float(getattr(entry, "started_at", None))
    except (TypeError, ValueError):
        return None


def _from_network(entries: list, rec_start: Optional[float]) -> dict:
    gam = []
    for e in entries:
        u = getattr(e, "url", "") or ""
        if "/gampad/ads" not in u.lower():
            continue
        t = _ts(e)
        try:
            q = parse_qsl(urlsplit(u).query, keep_blank_values=True)
        except ValueError:
            continue
        qd = dict(q)
        tg: dict = {}
        for key in ("scp", "cust_params"):
            if qd.get(key):
                for k, v in parse_qsl(qd[key], keep_blank_values=True):
                    tg.setdefault(k, v)
        gam.append({"t": t, "entry": e, "iu": qd.get("iu") or "unknown", "sz": qd.get("sz"), "targeting": tg})
    if not gam:
        return {"slots": []}
    gam.sort(key=lambda g: (g["t"] is None, g["t"] or 0))

    bidder_reqs = [e for e in entries if getattr(e, "category", "") in ("Header Bidding", "OpenRTB") and _ts(e) is not None]
    slots: dict = {}
    last_t: dict = {}
    for g in gam:
        iu = g["iu"]
        t = g["t"]
        win_start = max(last_t.get(iu, 0) or 0, (t or 0) - 10.0)
        last_t[iu] = t
        rows = []
        for r in bidder_reqs:
            rt = _ts(r)
            if rt is None or t is None or not (win_start <= rt <= t):
                continue
            status, price = _bid_from_body(getattr(r, "response_body", ""), getattr(r, "status", None))
            name = getattr(r, "vendor", "") or (urlsplit(r.url).netloc or "unknown")
            rows.append({"bidder": name, "name": name, "status": status, "cpm": price, "currency": None,
                         "size": None, "response_ms": None, "deal": None, "advertiser": None, "reason": None})
        rows.sort(key=lambda r: (r["cpm"] is None, -(r["cpm"] or 0)))
        tg = g["targeting"]
        hb = {k: tg[k] for k in _HB_KEYS if k in tg}
        body = getattr(g["entry"], "response_body", "") or ""
        hb_adid = tg.get("hb_adid")
        header_won = bool(hb.get("hb_bidder") and ((hb_adid and hb_adid in body) or "renderAd" in body))
        if header_won:
            code = hb["hb_bidder"]
            winner = {"who": friendly(code), "code": code, "kind": "header_bidding", "cpm": _num(hb.get("hb_pb")),
                      "currency": None,
                      "detail": "The Ad Manager response contains this header-bidding bid's creative.",
                      "basis": "reconstructed: Ad Manager's response renders the header-bidding bid (hb_adid / renderAd)"}
        elif hb.get("hb_bidder"):
            winner = {"who": AD_SERVER + " (direct / other demand)", "code": None, "kind": "ad_server", "cpm": None, "currency": None,
                      "detail": f"Header bidding sent {friendly(hb['hb_bidder'])} at {hb.get('hb_pb', '?')} to Ad Manager, "
                                "but the response does not contain that bid's creative.",
                      "basis": "reconstructed: a header-bidding bid was sent to Ad Manager but is not in its response"}
        else:
            winner = {"who": AD_SERVER + " (direct / other demand)", "code": None, "kind": "ad_server", "cpm": None,
                      "currency": None, "detail": "No header-bidding targeting was attached to this ad request.",
                      "basis": "reconstructed: the ad request carried no hb_ targeting"}
        priced = [r for r in rows if r["cpm"] is not None]
        slots.setdefault(iu, {"slot": iu, "ad_unit": iu, "cycles": []})["cycles"].append({
            "auction_id": None, "t": t, "offset_s": _off(t, rec_start), "page": getattr(g["entry"], "page", "") or "",
            "bid_requests": len(rows), "bidders_asked": len({r["bidder"] for r in rows}),
            "bids": len(priced), "no_bids": sum(1 for r in rows if r["status"] == "no bid"), "timeouts": 0,
            "rejected": 0, "no_answer": sum(1 for r in rows if r["status"] == "no answer recorded"),
            "rows": rows, "winner": winner, "sent_to_ad_server": hb, "render": None, "source": "network",
        })
    return {"slots": list(slots.values())}


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------

def _label(w: dict) -> str:
    return w["who"]


def _finish(slots: list) -> dict:
    overall_wins: Counter = Counter()
    kinds: dict = {}
    bidders: dict = {}
    total_cycles = 0
    auctions = 0
    asked_total = 0
    for sl in slots:
        wins: Counter = Counter()
        for n, c in enumerate(sl["cycles"], 1):
            c["n"] = n
            lab = _label(c["winner"])
            wins[lab] += 1
            overall_wins[lab] += 1
            kinds[lab] = c["winner"]["kind"]
            total_cycles += 1
            if c["bid_requests"] or c["rows"]:
                auctions += 1
                asked_total += c["bid_requests"] or len(c["rows"])
            for r in c["rows"]:
                b = bidders.setdefault(r["name"], {"name": r["name"], "asked": 0, "bid": 0, "no_bid": 0, "timeout": 0,
                                                   "rejected": 0, "wins": 0, "cpms": []})
                b["asked"] += 1
                if r["cpm"] is not None:
                    b["bid"] += 1
                    b["cpms"].append(r["cpm"])
                if r["status"] == "no bid":
                    b["no_bid"] += 1
                elif r["status"] == "timeout":
                    b["timeout"] += 1
                elif r["status"] == "rejected":
                    b["rejected"] += 1
                if r["status"] == "won":
                    b["wins"] += 1
        n_cycles = len(sl["cycles"])
        sl["summary"] = {
            "refreshes": n_cycles,
            "wins_by": [{"who": w, "kind": next((c["winner"]["kind"] for c in sl["cycles"] if c["winner"]["who"] == w), ""),
                         "wins": n, "share": round(100.0 * n / n_cycles)} for w, n in wins.most_common()],
        }
        sizes = []
        for c in sl["cycles"]:
            for r in c["rows"]:
                if r.get("size") and r["size"] not in sizes:
                    sizes.append(r["size"])
        sl["sizes"] = sizes[:6]
    slots.sort(key=lambda s: (-len(s["cycles"]), s["slot"]))
    bidder_table = []
    for b in bidders.values():
        cp = b.pop("cpms")
        b["avg_cpm"] = round(sum(cp) / len(cp), 3) if cp else None
        b["max_cpm"] = round(max(cp), 3) if cp else None
        bidder_table.append(b)
    bidder_table.sort(key=lambda b: (-b["wins"], -b["bid"], b["name"]))
    return {
        "slots": slots,
        "overall": {
            "refreshes": total_cycles, "auctions": auctions, "slots": len(slots),
            "bidders_seen": len(bidders), "avg_bidders_per_auction": round(asked_total / auctions, 1) if auctions else 0,
            "wins_by": [{"who": w, "kind": kinds.get(w, ""), "wins": n,
                         "share": round(100.0 * n / total_cycles) if total_cycles else 0}
                        for w, n in overall_wins.most_common()],
            "header_bidding_wins": sum(n for w, n in overall_wins.items() if kinds.get(w) == "header_bidding"),
            "ad_server_wins": sum(n for w, n in overall_wins.items() if kinds.get(w) == "ad_server"),
            "empty": sum(n for w, n in overall_wins.items() if kinds.get(w) == "none"),
        },
        "bidders": bidder_table,
    }


def analyze_bidding(entries: list, meta: Optional[dict] = None) -> dict:
    """Build the bidding report for a captured session."""
    meta = meta or {}
    rec_start = _num(meta.get("recording_started_at"))
    events = [e for e in (meta.get("auction_events") or []) if isinstance(e, dict)]

    source = "none"
    base: dict = {"slots": []}
    if any(e.get("src") in ("pb", "gpt") for e in events):
        base = _from_events(events, rec_start)
        source = "page"
    if not base["slots"]:
        base = _from_network(entries, rec_start)
        source = "network" if base["slots"] else "none"

    if not base["slots"]:
        return {"available": False, "source": "none", "slots": [], "overall": {}, "bidders": [],
                "notes": ["No header-bidding auctions or Ad Manager slot renders were recorded."]}

    out = _finish(base["slots"])
    notes = []
    if source == "page":
        notes.append("Read from the page's own Prebid and Google Publisher Tag events, so the requests, bids, "
                     "no-bids, timeouts and winners are exactly what the page reported.")
    else:
        notes.append("This page does not expose Prebid / Google Publisher Tag events, so each refresh was "
                     "reconstructed from network traffic: the bidder requests made just before each Ad Manager "
                     "request, and the header-bidding targeting (hb_bidder, hb_pb) it carried. Treat winners as "
                     "likely rather than certain, and prices as approximate.")
    notes.append("Auctions run entirely on a server (server-side header bidding) cannot be seen from the browser.")
    if out["overall"]["auctions"] == 0:
        notes.append("Only Ad Manager renders were seen - no header-bidding auction ran, so there are no bid requests to count.")
    return {"available": True, "source": source, **out, "notes": notes}
