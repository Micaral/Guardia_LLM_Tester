from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrowserConfig:
    url: str = "https://guardia.karasena.com/guardia/verificaciones"
    profile_dir: Path = Path(".auth/guardia-profile")
    output_dir: Path = Path("results")
    timeout_ms: int = 30_000
    headed: bool = True
    slow_mo_ms: int = 0

