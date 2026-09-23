"""Shared issue model for compliance findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


@dataclass
class Issue:
    field_path: str
    severity: Severity
    message: str
    actual: str = ""
    expected: str = ""
    request_ids: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.field_path

    def to_dict(self) -> dict:
        return {
            "field": self.field_path,
            "severity": self.severity.value,
            "message": self.message,
            "actual": self.actual,
            "expected": self.expected,
            "request_ids": self.request_ids,
        }
