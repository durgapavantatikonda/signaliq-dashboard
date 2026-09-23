"""Audit orchestration + Root Cause Analysis engine."""

from __future__ import annotations

import random
import string
from collections import Counter
from dataclasses import dataclass, field

from .parser import load_traffic, TrafficEntry
from .classifier import classify_all, Category
from .validators.issues import Issue, Severity
from .validators.openrtb import validate_openrtb
from .validators.vast import validate_vast
from .scoring import compute_scores, Scores, BASELINE_DAILY_REVENUE


@dataclass
class RootCause:
    primary_finding: str
    causal_evidence: str
    hypothesis: str
    recommended_actions: list = field(default_factory=list)


@dataclass
class AuditResult:
    audit_id: str
    target: str
    entries: list
    issues: list
    scores: Scores
    category_counts: dict
    rca: RootCause

    def to_dict(self) -> dict:
        return {
            "audit_id": self.audit_id,
            "target": self.target,
            "total_requests": len(self.entries),
            "category_counts": self.category_counts,
            "scores": {
                "signal_quality": self.scores.signal_quality.score,
                "monetization_readiness": self.scores.monetization.score,
                "daily_revenue_risk": self.scores.daily_revenue_risk,
                "risk_pct": self.scores.risk_pct,
                "has_ad_traffic": self.scores.has_ad_traffic,
            },
            "issues": [i.to_dict() for i in self.issues],
            "rca": {
                "primary_finding": self.rca.primary_finding,
                "causal_evidence": self.rca.causal_evidence,
                "hypothesis": self.rca.hypothesis,
                "recommended_actions": self.rca.recommended_actions,
            },
        }


def _gen_audit_id() -> str:
    return "SIQ-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _build_rca(issues: list, entries: list) -> RootCause:
    fields = {i.field_path: i for i in issues}

    def evid(*names):
        parts = []
        for n in names:
            if n in fields:
                i = fields[n]
                reqs = ", ".join(i.request_ids[:5]) or "n/a"
                parts.append(f"{n} [{i.severity.value}] on {reqs} - actual: "
                             f"{i.actual or '<empty>'}")
        return "; ".join(parts)

    if "user.ext.consent" in fields:
        return RootCause(
            primary_finding="Consent management platform (CMP) resolves after ad "
                            "request fire, sending GDPR-enforced bid requests with "
                            "an empty TCF consent string (IAB TCF Error 2.1a).",
            causal_evidence=evid("user.ext.consent", "regs.ext.gdpr"),
            hypothesis="The video player SDK initializes and fires its first ad "
                       "request before the CMP's __tcfapi callback returns. The "
                       "SDK reads an empty consent value at build time and never "
                       "refreshes it, so every subsequent request inherits the "
                       "empty string.",
            recommended_actions=[
                "Gate player/SDK initialization on the CMP ready event: wait for "
                "__tcfapi('addEventListener') to fire with eventStatus="
                "'tcloaded' or 'useractioncomplete' before calling player.init().",
                "In the ad request builder, read the consent string at request "
                "time (not SDK boot time) so late consent updates propagate.",
                "Add a pre-flight assertion: drop/queue ad requests where "
                "regs.ext.gdpr=1 and user.ext.consent is empty, and log a "
                "counter so regressions are visible in dashboards.",
            ],
        )

    if "device.ifa" in fields or "app.bundle" in fields:
        both = "device.ifa" in fields and "app.bundle" in fields
        which = ("device.ifa and app.bundle" if both
                 else ("device.ifa" if "device.ifa" in fields else "app.bundle"))
        return RootCause(
            primary_finding=(f"Ad request payloads are missing core CTV/app "
                             f"identity fields ({which}), causing automatic DSP "
                             f"bid rejection on affected traffic."),
            causal_evidence=evid("device.ifa", "app.bundle", "source.ext.schain"),
            hypothesis="The ad SDK is not being passed platform identity values "
                       "at integration time - either the app never calls the "
                       "SDK's setAdvertisingId()/setBundle() APIs, the platform "
                       "IFA lookup is executed asynchronously after the first "
                       "ad break, or limit-ad-tracking placeholders are being "
                       "forwarded unfiltered.",
            recommended_actions=[
                "Fetch the platform advertising ID during app cold-start and "
                "block the first ad request until it resolves (with a 2s timeout "
                "fallback to lmt=1 + ifa_type signaling).",
                "Hard-code app.bundle in the SDK config from the app manifest "
                "(e.g. BuildConfig.APPLICATION_ID on Android TV, "
                "Bundle.main.bundleIdentifier on tvOS).",
                "Add an outbound request interceptor in QA builds that fails CI "
                "if imp requests ship without device.ifa or app.bundle.",
            ],
        )

    if "VAST.Error" in fields or "InLine.Creatives.MediaFile" in fields:
        return RootCause(
            primary_finding="The ad server is returning explicit VAST errors or "
                            "creatives with no playable MediaFile, so delivered "
                            "impressions cannot render.",
            causal_evidence=evid("VAST.Error", "InLine.Creatives.MediaFile",
                                 "InLine.Impression"),
            hypothesis="Either the upstream ad server/wrapper chain is failing "
                       "(timeout or misconfigured line item returning an error "
                       "beacon) or transcoded creative assets are missing for "
                       "the requested MIME/bitrate profile, producing InLine "
                       "responses with empty MediaFile nodes.",
            recommended_actions=[
                "Replay the failing VAST tag directly (replay_request) and "
                "inspect the wrapper chain hop-by-hop to isolate which hop "
                "introduces the error/empty creative.",
                "Verify line-item creative transcodes exist for the player's "
                "requested MIME types (check the mimes[] sent in the bid "
                "request vs. MediaFile type attributes).",
                "Confirm ad server timeout budget: SSAI stitching windows under "
                "~4s frequently truncate wrapper resolution and surface Error 303.",
            ],
        )

    if "VAST.Ad" in fields:
        return RootCause(
            primary_finding="Ad requests complete but return empty VAST (no-fill); "
                            "demand is not matching the supply as described.",
            causal_evidence=evid("VAST.Ad", "source.ext.schain", "user.ext.eids"),
            hypothesis="Reduced bid density: with supply-chain and/or identity "
                       "signals missing, DSP filters exclude the inventory before "
                       "auction, and the ad server has no eligible demand to fill.",
            recommended_actions=[
                "Populate source.ext.schain with a complete=1 chain and verify "
                "the seller appears in the exchange's sellers.json.",
                "Enable at least one EID provider (UID2 / RampID) in the SDK.",
                "Check ad server delivery diagnostics for targeting exclusions "
                "(geo, content, key-values) on the affected placements.",
            ],
        )

    if fields:
        return RootCause(
            primary_finding="No hard delivery blockers, but bid-request signal "
                            "completeness gaps are suppressing bid density and CPMs.",
            causal_evidence=evid(*fields.keys()),
            hypothesis="The SDK's request builder omits optional-but-expected "
                       "fields (schain, eids, device specs, privacy signals); "
                       "buyers deprioritize under-described inventory.",
            recommended_actions=[
                "Populate all missing OpenRTB fields listed above in the SDK "
                "request builder configuration.",
                "Re-run this audit after deployment to confirm Signal Quality "
                "returns to >=90%.",
            ],
        )

    return RootCause(
        primary_finding="No compliance anomalies detected - ad delivery signals "
                        "are healthy on the captured traffic.",
        causal_evidence=f"All {len(entries)} captured requests passed OpenRTB and "
                        "VAST validation rules.",
        hypothesis="N/A - no failure to explain.",
        recommended_actions=["Schedule periodic re-audits to catch regressions "
                             "after SDK or CMP updates."],
    )


def run_audit(source, target: str = "",
              baseline_revenue: float = BASELINE_DAILY_REVENUE) -> AuditResult:
    entries = load_traffic(source)
    classify_all(entries)

    issues = []
    for e in entries:
        if e.category == Category.OPENRTB.value:
            issues.extend(validate_openrtb(e))
        if e.category in (Category.VAST.value, Category.SSAI.value):
            issues.extend(validate_vast(e))

    merged = {}
    for i in issues:
        if i.key in merged:
            merged[i.key].request_ids.extend(
                r for r in i.request_ids if r not in merged[i.key].request_ids)
        else:
            merged[i.key] = i
    order = ["Critical", "High", "Medium", "Low"]
    unique_issues = sorted(merged.values(),
                           key=lambda i: order.index(i.severity.value))

    counts = Counter(e.category for e in entries)
    ad_categories = {
        Category.OPENRTB.value, Category.VAST.value, Category.SSAI.value,
        Category.DISPLAY.value, Category.HEADER_BIDDING.value,
    }
    has_ad_traffic = sum(counts.get(c, 0) for c in ad_categories) > 0

    scores = compute_scores(unique_issues, baseline_revenue, has_ad_traffic=has_ad_traffic)
    if not target:
        first = next((e.url for e in entries if e.url), "unknown-target")
        target = first.split("?")[0][:80]

    rca = (_build_rca(unique_issues, entries) if has_ad_traffic
           else _build_no_traffic_rca(entries, counts))

    return AuditResult(
        audit_id=_gen_audit_id(), target=target, entries=entries,
        issues=unique_issues, scores=scores, category_counts=dict(counts),
        rca=rca,
    )


def _build_no_traffic_rca(entries: list, counts: dict) -> RootCause:
    breakdown = ", ".join(f"{cat}: {n}" for cat, n in
                          sorted(counts.items(), key=lambda kv: -kv[1])) or "no requests"
    return RootCause(
        primary_finding="No OpenRTB, VAST, SSAI, Display Ad, or Header Bidding "
                        "traffic was captured on this page - there is nothing "
                        "to audit. This is NOT the same as a clean audit.",
        causal_evidence=f"{len(entries)} total requests captured. Category "
                        f"breakdown: {breakdown}.",
        hypothesis="One of: (1) this page genuinely doesn't run programmatic "
                  "display/video ads (analytics-only, e.g. comScore/GA); (2) "
                  "the ad vendor(s) used aren't yet in SignalIQ's vendor "
                  "database (signaliq/vendors.py) so their calls fell into "
                  "'Other'/'API' uncategorized; (3) ads are gated behind a "
                  "consent banner or interaction the capture didn't fully "
                  "resolve; or (4) the capture window ended before any ad "
                  "unit resolved.",
        recommended_actions=[
            "Check the Raw Traffic tab for domains that look ad-related but "
            "show as 'Other' - if you spot one, it's a vendors.py gap, not "
            "an empty page.",
            "Re-run with a longer duration_seconds if the page is long or "
            "ad units are far below the fold.",
            "Confirm consent_accepted was true in the capture status - if "
            "false, the site's cookie banner wasn't recognized and may be "
            "blocking ad-tag initialization entirely.",
            "If none of the above explain it, this page likely just isn't "
            "monetized with programmatic ads.",
        ],
    )