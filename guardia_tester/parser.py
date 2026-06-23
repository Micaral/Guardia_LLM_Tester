from __future__ import annotations

import re
from pathlib import Path

from .models import TestCase

CASE_HEADER = re.compile(
    r"^###\s+(?P<id>[A-Z]\d{2})\s+[\N{EM DASH}-]\s+(?P<title>.+?)\s*$",
    re.MULTILINE,
)
EXPECTED = re.compile(
    r"^EXPECTED:\s*(?P<decision>block|allow)"
    r"(?:\s*·\s*subtype:\s*(?P<subtype>[^·\r\n]+))?"
    r"(?:\s*·\s*signals:\s*\[(?P<signals>[^\]]*)\])?"
    r"(?:\s*·\s*nota:\s*(?P<note>[^\r\n]*))?",
    re.IGNORECASE | re.MULTILINE,
)
INPUT = re.compile(
    r"^INPUT:\s*\r?\n(?:\"\"\"|```(?:text)?)\s*\r?\n"
    r"(?P<prompt>.*?)\r?\n(?:\"\"\"|```)\s*$",
    re.MULTILINE | re.DOTALL,
)


class CaseParseError(ValueError):
    pass


def parse_cases(path: str | Path) -> list[TestCase]:
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    headers = list(CASE_HEADER.finditer(text))
    if not headers:
        raise CaseParseError(f"No se encontraron casos en {source}")

    cases: list[TestCase] = []
    seen: set[str] = set()
    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        section = text[start:end]
        input_match = INPUT.search(section)
        expected_match = EXPECTED.search(section)
        case_id = header.group("id")
        if not input_match:
            raise CaseParseError(f"{case_id}: falta un bloque INPUT válido")
        if not expected_match:
            raise CaseParseError(f"{case_id}: falta EXPECTED: block/allow")
        if case_id in seen:
            raise CaseParseError(f"Identificador duplicado: {case_id}")
        seen.add(case_id)

        signals_raw = expected_match.group("signals") or ""
        signals = tuple(item.strip() for item in signals_raw.split(",") if item.strip())
        cases.append(
            TestCase(
                id=case_id,
                group=case_id[0],
                title=header.group("title").strip(),
                prompt=input_match.group("prompt").strip(),
                expected=expected_match.group("decision").lower(),  # type: ignore[arg-type]
                subtype=(expected_match.group("subtype") or "none").strip(),
                signals=signals,
                note=(expected_match.group("note") or "").strip(),
            )
        )
    return cases


def select_cases(
    cases: list[TestCase], case_ids: list[str] | None = None, groups: list[str] | None = None
) -> list[TestCase]:
    ids = {value.upper() for value in case_ids or []}
    selected_groups = {value.upper() for value in groups or []}
    if not ids and not selected_groups:
        return cases
    selected = [case for case in cases if case.id in ids or case.group in selected_groups]
    missing = ids - {case.id for case in selected}
    if missing:
        raise CaseParseError(f"Casos inexistentes: {', '.join(sorted(missing))}")
    return selected

