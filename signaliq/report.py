"""Markdown report renderer."""

from __future__ import annotations

from .audit import AuditResult
from .audience import build_audience_profile, build_display_signals, build_plain_summary


def _pct(score) -> str:
    return f"{score:.0f}%" if score is not None else "N/A"


def render_markdown_report(result: AuditResult) -> str:
    s = result.scores
    audience = build_audience_profile(result.entries)
    display = build_display_signals(result.entries)
    plain_summary = build_plain_summary(audience, display)

    lines = []
    add = lines.append

    add(f"# SignalIQ Audit Report: {result.target}")
    add(f"**Session Status**: Completed | **Audit ID**: #{result.audit_id}")
    add("")

    if plain_summary:
        add("### In Plain English")
        for line in plain_summary:
            add(f"-   {line}")
        add("")

    add("### Traffic Classification")
    add(f"*{len(result.entries)} requests captured*")
    add("")
    add("| Category | Requests |")
    add("|---|---|")
    for cat, n in sorted(result.category_counts.items(), key=lambda kv: -kv[1]):
        add(f"| {cat} | {n} |")
    add("")

    add("### Health Scorecards")
    if not s.has_ad_traffic:
        add("**No OpenRTB/VAST/Display/Header-Bidding traffic was captured - "
            "these scores are unscored (N/A), not a health measurement.** "
            "See the Root Cause Analysis section below for likely reasons.")
    add(f"*   **Signal Quality Score**: {_pct(s.signal_quality.score)}")
    add(f"*   **Monetization Readiness**: {_pct(s.monetization.score)}")
    add(f"*   **Daily Revenue Risk**: ${s.daily_revenue_risk:,.0f} "
        f"({s.risk_pct}% - {s.risk_reason})")
    add("")
    add("---")

    if display.get("vendors_detected"):
        add("### Vendors Detected")
        add(f"*{len(display['vendors_detected'])} ad-tech vendors fired during this capture*")
        add("")
        for cat, vendors in sorted((display.get("vendors_by_category") or {}).items()):
            add(f"*   **{cat}**: {', '.join(vendors)}")
        add("")
        if display.get("ad_units") or display.get("sizes_requested"):
            add("**Ad units & sizes requested:**")
            if display.get("ad_units"):
                add(f"*   Ad unit paths: {', '.join(display['ad_units'])}")
            if display.get("sizes_requested"):
                add(f"*   Sizes: {', '.join(display['sizes_requested'])}")
            add("")
        cs = display.get("consent_signals_seen") or {}
        if any(cs.values()):
            add(f"**Consent signals sent:** GDPR consent x{cs.get('gdpr_consent', 0)}, "
                f"US privacy x{cs.get('us_privacy', 0)}, GPP x{cs.get('gpp', 0)}, "
                f"NPA flag x{cs.get('npa', 0)}")
            add("")
        add("---")

    if audience.get("requests_with_bid_data"):
        p = audience["profile"]
        add("### Audience & Identity Profile (OpenRTB)")
        add(f"*From {audience['requests_with_bid_data']} OpenRTB bid request(s)*")
        add("")
        add(f"*   **App/Site**: {p.get('app_bundle') or p.get('site_domain') or '-'}")
        add(f"*   **Device**: {p.get('device_type', 'Unknown')}"
            + (f" ({p['device_make']} {p['device_model']})" if p.get('device_make') else ""))
        add(f"*   **OS**: {p.get('os') or '-'} {p.get('os_version') or ''}".strip())
        add(f"*   **Geo**: {', '.join(x for x in (p.get('geo_city'), p.get('geo_country')) if x) or '-'}")
        add(f"*   **Advertising ID**: "
            + ("missing/zeroed" if p.get("advertising_id_is_zeroed") else (p.get("advertising_id") or "-")))
        add(f"*   **Identity providers**: "
            + (', '.join(p['identity_providers']) if p.get('identity_providers') else 'none detected'))
        add(f"*   **Consent string present**: {'yes' if p.get('consent_string_present') else 'no'}")
        add(f"*   **Supply chain (schain) declared**: {'yes' if p.get('supply_chain_declared') else 'no'}")
        add("")
        add("---")

    add("### Compliance Anomalies & Diagnostics")
    if not result.issues:
        add("*No compliance anomalies detected.*")
    for n, issue in enumerate(result.issues, 1):
        reqs = ", ".join(issue.request_ids[:6])
        add(f"{n}.  **{issue.field_path}** [{issue.severity.value}]")
        add(f"    *   *Message*: {issue.message}")
        add(f"    *   *Actual*: \"{issue.actual}\"" if issue.actual
            else "    *   *Actual*: (missing)")
        add(f"    *   *Expected*: {issue.expected}")
        if reqs:
            add(f"    *   *Requests*: {reqs}")
    add("")
    add("---")

    add("### AI Root Cause Analysis (RCA)")
    add(f"*   **Primary Finding**: {result.rca.primary_finding}")
    add(f"*   **Causal Evidence**: {result.rca.causal_evidence}")
    add(f"*   **Root Cause Hypothesis**: {result.rca.hypothesis}")
    add("*   **Recommended Action Steps**:")
    for step in result.rca.recommended_actions:
        add(f"    1.  {step}")
    add("")
    return "\n".join(lines)
