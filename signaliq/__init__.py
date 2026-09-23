"""SignalIQ — programmatic ad-tech diagnostics & network intelligence."""

__version__ = "1.0.0"

from .parser import load_traffic, TrafficEntry
from .classifier import classify_entry, Category
from .audit import run_audit, AuditResult
from .report import render_markdown_report

__all__ = [
    "load_traffic", "TrafficEntry", "classify_entry", "Category",
    "run_audit", "AuditResult", "render_markdown_report",
]
