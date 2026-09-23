"""Scoring: Signal Quality, Monetization Readiness, Revenue Risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .validators.issues import Issue, Severity

BASELINE_DAILY_REVENUE = 5_000.0

SIGNAL_DEDUCTIONS = {
    "device.ifa": 30, "source.ext.schain": 20, "user.ext.eids": 15,
    "app.bundle": 25, "device.ua/device.ip": 10,
    "regs.ext.us_privacy/regs.ext.gdpr": 5,
    "InLine.Impression": 20, "InLine.Creatives.MediaFile": 25,
}
SIGNAL_FALLBACK = {Severity.CRITICAL: 15, Severity.HIGH: 8, Severity.MEDIUM: 3, Severity.LOW: 1}

MONETIZATION_DEDUCTIONS = {
    "device.ifa": 40, "app.bundle": 40, "InLine.Impression": 30,
    "InLine.Creatives.MediaFile": 40, "VAST.Ad": 20,
}
MONETIZATION_FALLBACK = {Severity.CRITICAL: 20, Severity.HIGH: 10, Severity.MEDIUM: 4, Severity.LOW: 2}


@dataclass
class ScoreBreakdown:
    score: Optional[float]
    deductions: list = field(default_factory=list)


@dataclass
class Scores:
    signal_quality: ScoreBreakdown
    monetization: ScoreBreakdown
    daily_revenue_risk: float
    risk_pct: int
    risk_reason: str
    has_ad_traffic: bool = True


def _dedupe(issues: list) -> list:
    seen = {}
    for i in issues:
        if i.key in seen:
            seen[i.key].request_ids.extend(
                r for r in i.request_ids if r not in seen[i.key].request_ids)
        else:
            seen[i.key] = i
    return list(seen.values())


def _score(issues, specific, fallback) -> ScoreBreakdown:
    deductions = []
    for issue in issues:
        if issue.field_path in specific:
            deductions.append((issue.field_path, specific[issue.field_path]))
        else:
            deductions.append((issue.field_path, fallback.get(issue.severity, 0)))
    total = sum(d for _, d in deductions)
    return ScoreBreakdown(score=max(0.0, 100.0 - total), deductions=deductions)


def _revenue_risk(issues, baseline):
    severities = {i.severity for i in issues}
    if Severity.CRITICAL in severities:
        return baseline * 0.75, 75, "Critical issues present - monetization blocked on affected traffic"
    if Severity.HIGH in severities:
        return baseline * 0.35, 35, "High-severity gaps - bid density reduced (missing schain/identity signals)"
    if Severity.MEDIUM in severities:
        return baseline * 0.10, 10, "Medium-severity signal gaps only"
    return 0.0, 0, "No compliance issues detected"


def compute_scores(issues: list, baseline_revenue: float = BASELINE_DAILY_REVENUE,
                    has_ad_traffic: bool = True) -> Scores:
    if not has_ad_traffic:
        # No OpenRTB/VAST/SSAI/Display/Header-Bidding traffic was captured at
        # all - there's nothing to score. Returning 100%/$0 here would read
        # as "healthy" when it actually means "no data" - report it as such
        # instead of silently faking a clean bill of health.
        return Scores(
            signal_quality=ScoreBreakdown(score=None),
            monetization=ScoreBreakdown(score=None),
            daily_revenue_risk=0.0, risk_pct=0,
            risk_reason="No ad traffic captured - nothing to score.",
            has_ad_traffic=False,
        )
    unique = _dedupe(issues)
    risk, pct, reason = _revenue_risk(unique, baseline_revenue)
    return Scores(
        signal_quality=_score(unique, SIGNAL_DEDUCTIONS, SIGNAL_FALLBACK),
        monetization=_score(unique, MONETIZATION_DEDUCTIONS, MONETIZATION_FALLBACK),
        daily_revenue_risk=risk, risk_pct=pct, risk_reason=reason,
        has_ad_traffic=True,
    )