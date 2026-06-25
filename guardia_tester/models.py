from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Decision = Literal["block", "allow"]


@dataclass(frozen=True)
class TestCase:
    id: str
    group: str
    title: str
    prompt: str
    expected: Decision
    subtype: str = "none"
    signals: tuple[str, ...] = ()
    note: str = ""


@dataclass
class TestResult:
    case: TestCase
    actual: Decision | None
    status: Literal["PASS", "FAIL", "ERROR"]
    attempt: int = 1
    reason: str = ""
    duration_ms: int = 0
    screenshot: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["case"]["signals"] = list(self.case.signals)
        return value


@dataclass
class RunSummary:
    total: int
    passed: int
    failed: int
    errors: int
    false_positives: int
    false_negatives: int
    true_positives: int
    true_negatives: int
    accuracy: float
    precision: float
    block_recall: float
    f1: float
    fp_rate: float
    prompts: int
    stable_pass: int
    flaky: int
    stable_fail: int
    incomplete: int
    # Response-time percentiles (ms) — computed from completed attempts only
    avg_duration_ms: float = 0.0
    p50_duration_ms: int = 0
    p95_duration_ms: int = 0
    max_duration_ms: int = 0
    by_group: dict[str, dict[str, int | float]] = field(default_factory=dict)


@dataclass
class CaseAggregate:
    id: str
    group: str
    title: str
    prompt: str
    expected: Decision
    attempts: int
    completed: int
    errors: int
    block_count: int
    allow_count: int
    correct_count: int
    correct_rate: float
    block_rate: float
    allow_rate: float
    classification: Literal["STABLE_PASS", "FLAKY", "STABLE_FAIL", "INCOMPLETE"]
    avg_duration_ms: float = 0.0
