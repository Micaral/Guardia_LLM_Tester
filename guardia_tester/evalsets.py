from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class EvalsetSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class EvalsetFile:
    id: str
    path: Path


def evalset_id(path: str | Path) -> str:
    stem = Path(path).stem
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    for prefix in ("guardia-evalset-", "evalset-"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    return slug or "evalset"


def discover_evalsets(workspace: Path, evalsets_dir: Path) -> list[EvalsetFile]:
    candidates: set[Path] = set()
    directory = evalsets_dir if evalsets_dir.is_absolute() else workspace / evalsets_dir
    if directory.is_dir():
        candidates.update(
            path for path in directory.glob("*.md") if path.name.lower() != "readme.md"
        )
    # Backwards compatibility with the original evalset stored at project root.
    candidates.update(workspace.glob("*EvalSet*.md"))

    found: list[EvalsetFile] = []
    ids: dict[str, Path] = {}
    for path in sorted(candidates, key=lambda item: item.name.lower()):
        identifier = evalset_id(path)
        if identifier in ids and ids[identifier].resolve() != path.resolve():
            raise EvalsetSelectionError(
                f"Dos evalsets generan el mismo identificador '{identifier}': "
                f"{ids[identifier]} y {path}"
            )
        ids[identifier] = path
        found.append(EvalsetFile(identifier, path))
    return found


def select_evalset(evalsets: list[EvalsetFile], requested: str) -> EvalsetFile:
    normalized = evalset_id(requested)
    matches = [item for item in evalsets if item.id == normalized]
    if not matches:
        available = ", ".join(item.id for item in evalsets) or "ninguno"
        raise EvalsetSelectionError(
            f"Evalset desconocido '{requested}'. Disponibles: {available}"
        )
    return matches[0]

