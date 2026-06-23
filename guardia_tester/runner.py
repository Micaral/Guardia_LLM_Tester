from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .api import CurlKarasenaClient, RequestTemplateError
from .browser import BrowserSetupError, KarasenaChecker
from .config import BrowserConfig
from .evalsets import (
    EvalsetFile,
    EvalsetSelectionError,
    discover_evalsets,
    evalset_id,
    select_evalset,
)
from .models import TestResult
from .parser import CaseParseError, parse_cases, select_cases
from .report import write_reports

def find_system_chrome() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BrowserSetupError("No encuentro Google Chrome instalado en las rutas habituales")


async def manual_login(config: BrowserConfig) -> None:
    """Authenticate in ordinary Chrome so Google never sees a browser under automation."""
    chrome = find_system_chrome()
    profile = config.profile_dir.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            config.url,
        ]
    )
    print("\nChrome se ha abierto de forma normal, sin Playwright.")
    print("Inicia sesión con Google y comprueba que puedes entrar en Karasena.")
    print("Después CIERRA esa ventana de Chrome para liberar el perfil.")
    await asyncio.to_thread(input, "Cuando Chrome esté cerrado, pulsa ENTER... ")
    if process.poll() is None:
        print("AVISO: Chrome parece seguir abierto. Ciérralo antes de ejecutar las pruebas.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ejecuta el set de evaluación contra el comprobador web de Karasena."
    )
    parser.add_argument("--cases", type=Path, default=None, help="Ruta explícita a un evalset Markdown")
    parser.add_argument("--evalset", help="Identificador del evalset que se desea ejecutar")
    parser.add_argument(
        "--evalsets-dir", type=Path, default=Path("evalsets"),
        help="Carpeta donde se descubren los evalsets (predeterminado: evalsets)",
    )
    parser.add_argument(
        "--list-evalsets", action="store_true", help="Mostrar evalsets disponibles y terminar"
    )
    parser.add_argument("--url", default=BrowserConfig.url, help="URL del comprobador")
    parser.add_argument("--profile-dir", type=Path, default=BrowserConfig.profile_dir)
    parser.add_argument("--output-dir", type=Path, default=BrowserConfig.output_dir)
    parser.add_argument(
        "--adapter", choices=["api", "browser"], default="api",
        help="Método de ejecución; api no necesita navegador (predeterminado)",
    )
    parser.add_argument(
        "--request-file", type=Path, default=Path(".auth/karasena.fetch.js"),
        help="Petición Copy as cURL guardada localmente",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Timeout por caso en segundos")
    parser.add_argument(
        "--repetitions", type=int, default=5,
        help="Número de pasadas completas por caso (predeterminado: 5)",
    )
    parser.add_argument(
        "--token-expiry", choices=["pause", "stop"], default="pause",
        help="Al caducar el token: pausar para renovarlo o detenerse (predeterminado: pause)",
    )
    parser.add_argument("--case", action="append", dest="case_ids", help="Caso concreto, repetible")
    parser.add_argument("--group", action="append", help="Grupo concreto, repetible")
    parser.add_argument("--login", action="store_true", help="Abrir Chrome para iniciar sesión manualmente")
    parser.add_argument("--dry-run", action="store_true", help="Validar casos sin abrir Chrome")
    parser.add_argument("--headless", action="store_true", help="Ejecutar Chrome sin interfaz visible")
    parser.add_argument("--slow-mo", type=int, default=0, help="Pausa entre acciones, en ms")
    parser.add_argument("--fail-fast", action="store_true", help="Detenerse tras el primer fallo")
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        print("ERROR: --repetitions debe ser al menos 1", file=sys.stderr)
        return 2
    try:
        selected_evalset = await resolve_evalset(args)
    except EvalsetSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.list_evalsets:
        return 0

    try:
        all_cases = parse_cases(selected_evalset.path)
        cases = select_cases(all_cases, args.case_ids, args.group)
    except (OSError, CaseParseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    counts = Counter(case.expected for case in cases)
    print(f"Evalset: {selected_evalset.id} ({selected_evalset.path})")
    print(
        f"Casos cargados: {len(cases)} "
        f"({counts['block']} block, {counts['allow']} allow)"
    )
    if args.dry_run:
        print("Markdown válido. No se ha abierto el navegador.")
        return 0

    config = BrowserConfig(
        url=args.url,
        profile_dir=args.profile_dir,
        output_dir=args.output_dir / selected_evalset.id,
        timeout_ms=args.timeout * 1000,
        headed=not args.headless,
        slow_mo_ms=args.slow_mo,
    )

    if args.login:
        try:
            await manual_login(config)
        except BrowserSetupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Sesión guardada en {config.profile_dir}")
        return 0

    if args.adapter == "api":
        checker = await load_api_checker(args.request_file, args.timeout, args.token_expiry)
        if checker is None:
            return 2
        if checker.token_expires_at is not None:
            remaining = max(0, int(checker.token_expires_at - time.time()))
            print(f"Token de sesión válido durante aproximadamente {remaining} segundos.")
        results, completed_all = await execute_cases(
            cases, checker, config.output_dir, args.fail_fast, False, args.repetitions,
            args.token_expiry,
        )
    else:
        try:
            async with KarasenaChecker(config) as checker:
                await checker.open()
                await checker.ensure_checker_ready()
                results, completed_all = await execute_cases(
                    cases, checker, config.output_dir, args.fail_fast, True, args.repetitions,
                    "stop",
                )
        except BrowserSetupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    html_path, summary_csv_path, csv_path, json_path = write_reports(results, config.output_dir)
    passed = sum(result.status == "PASS" for result in results)
    print(f"\nResultado: {passed}/{len(results)} PASS")
    print(f"Informe HTML: {html_path}")
    print(f"Resumen por prompt: {summary_csv_path}")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")
    if not completed_all:
        print("La batería no terminó; el informe contiene únicamente pasadas completas.")
        return 2
    return 0 if all(result.status == "PASS" for result in results) else 1


async def execute_cases(
    cases: list[Any], checker: Any, output_dir: Path, fail_fast: bool, screenshots: bool,
    repetitions: int = 1, token_expiry: str = "pause",
) -> tuple[list[TestResult], bool]:
    results: list[TestResult] = []
    screenshot_dir = output_dir / "screenshots"
    total = len(cases) * repetitions
    sequence = 0
    stop = False
    completed_all = True
    for attempt in range(1, repetitions + 1):
        round_results: list[TestResult] = []
        discard_round = False
        if repetitions > 1:
            print(f"\n--- Pasada {attempt}/{repetitions} ---")
        for case in cases:
            sequence += 1
            while True:
                started = time.perf_counter()
                print(f"[{sequence:03}/{total:03}] {case.id} ", end="", flush=True)
                try:
                    actual, reason = await checker.check(case.prompt)
                    status = "PASS" if actual == case.expected else "FAIL"
                    result = TestResult(
                        case=case,
                        actual=actual,
                        status=status,
                        attempt=attempt,
                        reason=reason,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    )
                    if screenshots and status == "FAIL":
                        screenshot = screenshot_dir / f"{case.id}-r{attempt:02}.png"
                        await checker.screenshot(screenshot)
                        result.screenshot = str(screenshot)
                    round_results.append(result)
                    suffix = f" ({reason})" if reason else ""
                    print(f"{status}: expected={case.expected}, actual={actual}{suffix}")
                    if fail_fast and status != "PASS":
                        stop = True
                        completed_all = False
                    break
                except Exception as exc:
                    if _is_token_expiry(exc):
                        print(f"TOKEN CADUCADO: {exc}")
                        if token_expiry == "pause" and hasattr(checker, "reload_from_file"):
                            print("1. Copia una petición cURL nueva desde Karasena.")
                            print(f"2. Reemplaza y guarda {checker.request_file}.")
                            try:
                                answer = await asyncio.to_thread(
                                    input, "Pulsa ENTER para reintentar este caso o escribe S para terminar: "
                                )
                            except (EOFError, KeyboardInterrupt):
                                answer = "s"
                            if answer.strip().lower() != "s":
                                try:
                                    checker.reload_from_file()
                                    print("Credenciales renovadas. Reintentando sin perder resultados...")
                                    continue
                                except RequestTemplateError as reload_error:
                                    print(f"Credenciales no válidas: {reload_error}")
                                    continue
                        stop = True
                        completed_all = False
                        discard_round = True
                        print(f"La pasada {attempt} se descartará de las estadísticas.")
                        break

                    screenshot_value: str | None = None
                    if screenshots:
                        screenshot = screenshot_dir / f"{case.id}-r{attempt:02}-error.png"
                        try:
                            await checker.screenshot(screenshot)
                            screenshot_value = str(screenshot)
                        except Exception:
                            pass
                    round_results.append(TestResult(
                        case=case,
                        actual=None,
                        status="ERROR",
                        attempt=attempt,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        screenshot=screenshot_value,
                        error=f"{type(exc).__name__}: {exc}",
                    ))
                    print(f"ERROR: {type(exc).__name__}: {exc}")
                    break
            if stop:
                break
        if not discard_round:
            results.extend(round_results)
        if stop:
            break
    return results, completed_all


def _is_token_expiry(exc: Exception) -> bool:
    if not isinstance(exc, RequestTemplateError):
        return False
    message = str(exc).lower()
    return "token" in message or "sesión ha caducado" in message


async def load_api_checker(
    request_file: Path, timeout: int, token_expiry: str
) -> CurlKarasenaClient | None:
    while True:
        try:
            return CurlKarasenaClient(request_file, timeout)
        except RequestTemplateError as exc:
            if token_expiry != "pause" or not _is_token_expiry(exc):
                print(f"ERROR: {exc}", file=sys.stderr)
                return None
            print(f"TOKEN CADUCADO: {exc}")
            print(f"Reemplaza y guarda {request_file} con un cURL nuevo.")
            try:
                answer = await asyncio.to_thread(
                    input, "Pulsa ENTER para continuar o escribe S para terminar: "
                )
            except (EOFError, KeyboardInterrupt):
                answer = "s"
            if answer.strip().lower() == "s":
                return None


async def resolve_evalset(args: argparse.Namespace) -> EvalsetFile:
    workspace = Path.cwd()
    if args.cases is not None:
        path = args.cases if args.cases.is_absolute() else workspace / args.cases
        if not path.is_file():
            raise EvalsetSelectionError(f"No existe el evalset: {path}")
        selected = EvalsetFile(evalset_id(path), path)
        if args.list_evalsets:
            print(f"{selected.id:24} {selected.path}")
        return selected

    evalsets = discover_evalsets(workspace, args.evalsets_dir)
    if args.list_evalsets:
        if not evalsets:
            print("No se encontraron evalsets.")
        for item in evalsets:
            try:
                count = len(parse_cases(item.path))
                detail = f"{count} casos"
            except Exception as exc:
                detail = f"ERROR: {exc}"
            print(f"{item.id:24} {detail:12} {item.path}")
        return evalsets[0] if evalsets else EvalsetFile("none", Path("."))

    if args.evalset:
        return select_evalset(evalsets, args.evalset)
    if not evalsets:
        raise EvalsetSelectionError(
            f"No hay evalsets. Añade archivos .md a {args.evalsets_dir}."
        )
    if len(evalsets) == 1:
        return evalsets[0]
    if not sys.stdin.isatty():
        available = ", ".join(item.id for item in evalsets)
        raise EvalsetSelectionError(
            f"Hay varios evalsets ({available}). Indica uno con --evalset."
        )

    print("Evalsets disponibles:")
    for index, item in enumerate(evalsets, start=1):
        print(f"  {index}. {item.id} — {item.path.name}")
    while True:
        answer = await asyncio.to_thread(input, "Selecciona un evalset por número: ")
        try:
            return evalsets[int(answer) - 1]
        except (ValueError, IndexError):
            print("Selección no válida.")


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nEjecución cancelada.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
