from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import TestCase, TestResult
from .report import write_html_report


def load_results(path: Path) -> tuple[list[TestResult], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results: list[TestResult] = []
    for raw in payload.get("results", []):
        case_raw = raw["case"]
        case = TestCase(
            id=case_raw["id"],
            group=case_raw["group"],
            title=case_raw["title"],
            prompt=case_raw["prompt"],
            expected=case_raw["expected"],
            subtype=case_raw.get("subtype", "none"),
            signals=tuple(case_raw.get("signals", [])),
            note=case_raw.get("note", ""),
        )
        results.append(TestResult(
            case=case,
            actual=raw.get("actual"),
            status=raw["status"],
            attempt=int(raw.get("attempt", 1)),
            reason=raw.get("reason", ""),
            duration_ms=int(raw.get("duration_ms", 0)),
            screenshot=raw.get("screenshot"),
            error=raw.get("error"),
        ))
    evalset_name = payload.get("evalset") or "legacy"
    return results, evalset_name


def regenerate(root: Path) -> list[Path]:
    regenerated: list[Path] = []
    for json_path in sorted(root.rglob("results.json")):
        results, evalset_name = load_results(json_path)
        report_path = json_path.with_name("report.html")
        write_html_report(results, report_path, evalset_name)
        regenerated.append(report_path)
    return regenerated


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenera informes HTML desde results.json")
    parser.add_argument("root", nargs="?", type=Path, default=Path("results"))
    args = parser.parse_args()
    reports = regenerate(args.root)
    for report in reports:
        print(f"Regenerado: {report}")
    print(f"Total: {len(reports)} informe(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

