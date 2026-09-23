"""Known ad-tech vendor domains, grouped by function.

This is what lets SignalIQ recognize DISPLAY / HEADER_BIDDING / IDENTITY_SYNC /
VERIFICATION traffic instead of only OpenRTB+VAST. It's intentionally a
plain data module (domain substring -> (category, vendor label)) so it's
cheap to extend - add a line, no logic changes needed.

Categories map onto signaliq.classifier.Category.
"""

from __future__ import annotations

# domain substring -> vendor display name
AD_SERVERS = {
    "securepubads.g.doubleclick.net": "Google Ad Manager (GAM)",
    "pubads.g.doubleclick.net": "Google Ad Manager (GAM, legacy)",
    "pagead2.googlesyndication.com": "Google AdSense/AdX",
    "googlesyndication.com": "Google AdSense/AdX",
    "adservice.google.com": "Google Ad Service",
    "gads.pubmatic.com": "PubMatic Ad Server",
}

HEADER_BIDDING = {
    "ib.adnxs.com": "AppNexus / Xandr",
    "prebid.adnxs.com": "Xandr Prebid Server",
    "fastlane.rubiconproject.com": "Magnite (Rubicon)",
    "prebid-server.rubiconproject.com": "Magnite Prebid Server",
    "hbopenbid.pubmatic.com": "PubMatic",
    "ads.pubmatic.com": "PubMatic",
    "rtb.openx.net": "OpenX",
    "openx.net": "OpenX",
    "htlb.casalemedia.com": "Index Exchange",
    "casalemedia.com": "Index Exchange",
    "bidder.criteo.com": "Criteo",
    "criteo.com": "Criteo",
    "aax.amazon-adsystem.com": "Amazon TAM/UAM",
    "c.amazon-adsystem.com": "Amazon Publisher Services",
    "amazon-adsystem.com": "Amazon Advertising",
    "ads.yieldmo.com": "Yieldmo",
    "sync.sharethrough.com": "Sharethrough",
    "btlr.sharethrough.com": "Sharethrough",
    "tlx.3lift.com": "TripleLift",
    "ib.3lift.com": "TripleLift",
    "sovrn.com": "Sovrn",
    "lijit.com": "Sovrn (legacy Lijit)",
    "unrulymedia.com": "Unruly / Tremor",
    "adform.net": "Adform",
    "adform.com": "Adform",
    "smartadserver.com": "Smart AdServer (Equativ)",
    "yahoo.com/ads": "Yahoo Advertising",
    "gemini.yahoo.com": "Yahoo Gemini",
    "kargo.com": "Kargo",
    "teads.tv": "Teads",
    "rhythmone.com": "RhythmOne / Yahoo",
    "spotx.tv": "SpotX (Magnite)",
    "spotxchange.com": "SpotX (Magnite)",
    "verizonmedia.com": "Verizon Media / Yahoo",
    "adsrvr.org": "The Trade Desk",
    "media.net": "Media.net",
}

IDENTITY_SYNC = {
    "id5-sync.com": "ID5 Universal ID",
    "cm.g.doubleclick.net": "Google Cookie Match",
    "idsync.rlcdn.com": "LiveRamp (RampID / IdentityLink)",
    "sync.crwdcntrl.net": "LiveRamp / Lotame",
    "usersync.gumgum.com": "GumGum User Sync",
    "match.adsrvr.org": "The Trade Desk ID Sync",
    "pixel.rubiconproject.com": "Magnite ID Sync",
    "sync.pubmatic.com": "PubMatic ID Sync",
    "eb2.3lift.com": "TripleLift ID Sync",
    "usersync.pubmatic.com": "PubMatic User Sync",
    "prebid.a-mo.net": "AudienceProject / Prebid Sync",
    "unifiedid.com": "Unified ID 2.0",
    "uidapi.com": "Unified ID 2.0",
    "liveintent.com": "LiveIntent Identity",
}

VERIFICATION = {
    "pixel.adsafeprotected.com": "Integral Ad Science (IAS)",
    "adsafeprotected.com": "Integral Ad Science (IAS)",
    "doubleverify.com": "DoubleVerify",
    "dvtps.com": "DoubleVerify",
    "z.moatads.com": "Moat (Oracle)",
    "moatads.com": "Moat (Oracle)",
    "scorecardresearch.com": "comScore",
    "geo.moatads.com": "Moat Geo",
    "grapeshot.com": "GrapeShot Brand Safety (Oracle)",
    "adalytics.io": "Adalytics",
}

CONSENT_CMP = {
    "cdn.consensu.org": "IAB TCF CMP",
    "sourcepoint.mgr.consensu.org": "Sourcepoint CMP",
    "cmp.quantcast.com": "Quantcast Choice CMP",
    "cookielaw.org": "OneTrust CMP",
    "cookiepro.com": "OneTrust CMP",
    "onetrust.com": "OneTrust CMP",
    "didomi.io": "Didomi CMP",
    "trustarc.com": "TrustArc CMP",
    "usercentrics.eu": "Usercentrics CMP",
}

ALL_VENDOR_MAPS = {
    "Display Ad Server": AD_SERVERS,
    "Header Bidding": HEADER_BIDDING,
    "Identity Sync": IDENTITY_SYNC,
    "Verification": VERIFICATION,
    "Consent/CMP": CONSENT_CMP,
}


def match_vendor(host: str) -> tuple[str, str] | None:
    """Return (group_label, vendor_name) for the first matching domain, or None."""
    host = (host or "").lower()
    for group, mapping in ALL_VENDOR_MAPS.items():
        for domain, name in mapping.items():
            if domain in host:
                return group, name
    return None
