"""
run_all.py — Ejecuta todos los evalsets disponibles de forma secuencial.

Cada evalset genera su propio informe independiente en results/<evalset>/<timestamp>/.
Los argumentos extra se reenvían a run_tests.py (p. ej. --repetitions 3 --timeout 60).

Uso:
    python run_all.py
    python run_all.py --repetitions 3
    python run_all.py --evalsets-dir evalsets --request-file .auth/karasena.fetch.js
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from guardia_tester.evalsets import discover_evalsets

_WORKSPACE = Path(__file__).parent
_RUNNER = _WORKSPACE / "run_tests.py"


def _discover(evalsets_dir: Path) -> list[str]:
    found = discover_evalsets(_WORKSPACE, evalsets_dir)
    if not found:
        print("No se encontraron evalsets en:", evalsets_dir)
        sys.exit(1)
    return [ef.id for ef in found]


def main() -> int:
    # Split args: --evalsets-dir is consumed here; everything else goes to run_tests.py
    raw = sys.argv[1:]
    evalsets_dir = Path("evalsets")
    passthrough: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "--evalsets-dir" and i + 1 < len(raw):
            evalsets_dir = Path(raw[i + 1])
            i += 2
        else:
            passthrough.append(raw[i])
            i += 1

    evalset_ids = _discover(evalsets_dir)
    total = len(evalset_ids)

    print(f"\n{'='*60}")
    print(f"  GuardIA — Batida completa ({total} evalsets)")
    print(f"{'='*60}\n")

    results: list[tuple[str, int, float]] = []
    for idx, eid in enumerate(evalset_ids, 1):
        print(f"[{idx}/{total}] Iniciando evalset: {eid}")
        print("-" * 60)
        t0 = time.monotonic()
        cmd = [
            sys.executable, str(_RUNNER),
            "--evalset", eid,
            "--evalsets-dir", str(evalsets_dir),
            *passthrough,
        ]
        ret = subprocess.run(cmd).returncode
        elapsed = time.monotonic() - t0
        results.append((eid, ret, elapsed))
        status = "OK" if ret == 0 else f"FALLO (código {ret})"
        print(f"\n[{idx}/{total}] {eid} — {status} en {elapsed:.0f}s\n")

    print(f"\n{'='*60}")
    print(f"  Resumen de la batida")
    print(f"{'='*60}")
    ok = sum(1 for _, r, _ in results if r == 0)
    for eid, ret, elapsed in results:
        icon = "✓" if ret == 0 else "✗"
        print(f"  {icon} {eid:<35} {elapsed:>6.0f}s")
    print(f"{'='*60}")
    print(f"  {ok}/{total} evalsets completados correctamente")
    print(f"{'='*60}\n")

    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
