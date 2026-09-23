"""Markdown report renderer."""

from __future__ import annotations

from .audit import AuditResult


def render_markdown_report(result: AuditResult) -> str:
    s = result.scores
    lines = []
    add = lines.append

    add(f"# SignalIQ Audit Report: {result.target}")
    add(f"**Session Status**: Completed | **Audit ID**: #{result.audit_id}")
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
    add(f"*   **Signal Quality Score**: {s.signal_quality.score:.0f}%")
    add(f"*   **Monetization Readiness**: {s.monetization.score:.0f}%")
    add(f"*   **Daily Revenue Risk**: ${s.daily_revenue_risk:,.0f} "
        f"({s.risk_pct}% - {s.risk_reason})")
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
