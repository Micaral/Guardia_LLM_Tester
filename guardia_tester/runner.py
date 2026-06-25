from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .api import CurlKarasenaClient, KarasenaAPIClient, RequestTemplateError
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ejecuta el set de evaluación contra el comprobador de Karasena."
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
    parser.add_argument("--url", default=BrowserConfig.url, help="URL del comprobador (modo browser)")
    parser.add_argument(
        "--base-url", default=KarasenaAPIClient.DEFAULT_BASE_URL,
        help=f"URL base de la API (predeterminado: {KarasenaAPIClient.DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--token", default=None,
        help=(
            "Bearer token JWT (opcional). "
            "Si se omite, se usa el cURL guardado en --request-file. "
            "Con token: activa gestión de prompts y enriquece el informe con datos de empresa."
        ),
    )
    parser.add_argument(
        "--platform", default=None,
        help="Código de plataforma enviado al comprobador (p. ej. CHATGPT). Requiere --token.",
    )
    parser.add_argument(
        "--list-platforms", action="store_true",
        help="Listar plataformas disponibles y terminar. Requiere --token.",
    )
    parser.add_argument(
        "--list-prompts", action="store_true",
        help="Listar prompts de la empresa y terminar. Requiere --token.",
    )
    parser.add_argument(
        "--enable-prompt", type=int, action="append", dest="enable_prompts", metavar="ID",
        help="Activar prompt por ID antes de ejecutar. Requiere --token. (repetible)",
    )
    parser.add_argument(
        "--disable-prompt", type=int, action="append", dest="disable_prompts", metavar="ID",
        help="Desactivar prompt por ID antes de ejecutar. Requiere --token. (repetible)",
    )
    parser.add_argument("--profile-dir", type=Path, default=BrowserConfig.profile_dir)
    parser.add_argument("--output-dir", type=Path, default=BrowserConfig.output_dir)
    parser.add_argument(
        "--adapter", choices=["api", "browser"], default="api",
        help="api = replay cURL (predeterminado); browser = Playwright",
    )
    parser.add_argument(
        "--request-file", type=Path, default=Path(".auth/karasena.fetch.js"),
        help=(
            "Archivo con la petición copiada como cURL (bash) desde DevTools. "
            "Predeterminado: .auth/karasena.fetch.js"
        ),
    )
    parser.add_argument("--timeout", type=int, default=30, help="Timeout por caso en segundos")
    parser.add_argument(
        "--repetitions", type=int, default=5,
        help="Número de pasadas completas del evalset (predeterminado: 5)",
    )
    parser.add_argument(
        "--token-expiry", choices=["pause", "stop"], default="pause",
        help="Al caducar el token: pausar para renovarlo o detenerse (predeterminado: pause)",
    )
    parser.add_argument("--case", action="append", dest="case_ids", help="Caso concreto (repetible)")
    parser.add_argument("--group", action="append", help="Grupo concreto (repetible)")
    parser.add_argument("--dry-run", action="store_true", help="Validar casos sin hacer peticiones")
    parser.add_argument("--headless", action="store_true", help="Ejecutar Chrome sin interfaz")
    parser.add_argument("--slow-mo", type=int, default=0, help="Pausa entre acciones en ms")
    parser.add_argument("--fail-fast", action="store_true", help="Detenerse tras el primer fallo")
    return parser


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        print("ERROR: --repetitions debe ser al menos 1", file=sys.stderr)
        return 2

    # --- Optional prompt / platform management (requires explicit --token) --
    needs_mgmt = (
        args.list_platforms or args.list_prompts
        or args.enable_prompts or args.disable_prompts
    )
    if needs_mgmt:
        if not args.token:
            print(
                "ERROR: la gestión de prompts requiere --token <jwt>.",
                file=sys.stderr,
            )
            return 2
        try:
            mgmt_client = KarasenaAPIClient(args.token, args.base_url, timeout_seconds=args.timeout)
        except RequestTemplateError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        if args.list_platforms:
            platforms = mgmt_client.list_platforms()
            if not platforms:
                print("No se encontraron plataformas.")
            else:
                print(f"{'Código':<22} {'Nombre':<30} ID")
                print("-" * 60)
                for p in platforms:
                    print(f"{p.get('codigo','?'):<22} {p.get('nombre','?'):<30} {p.get('id','?')}")
            return 0

        if args.list_prompts:
            bt = mgmt_client.get_blocking_types()
            prompts = mgmt_client.get_prompts()
            if not prompts:
                print("No se encontraron prompts en esta empresa.")
            else:
                print(f"{'ID':<6} {'Estado':<10} {'Tipo bloqueo':<24} Nombre")
                print("-" * 72)
                for p in prompts:
                    estado = "activo" if p.get("comprobable") else "inactivo"
                    tipo = bt.get(p.get("tipoBloqueoId", -1), "—")
                    print(f"{p.get('id','?'):<6} {estado:<10} {tipo:<24} {p.get('nombre','—')}")
            return 0

        errors = 0
        for pid in (args.enable_prompts or []):
            try:
                r = mgmt_client.set_prompt_active(pid, True)
                print(f"Prompt {pid} ({r.get('nombre','?')}) → activado")
            except RequestTemplateError as exc:
                print(f"Prompt {pid}: {exc}", file=sys.stderr)
                errors += 1
        for pid in (args.disable_prompts or []):
            try:
                r = mgmt_client.set_prompt_active(pid, False)
                print(f"Prompt {pid} ({r.get('nombre','?')}) → desactivado")
            except RequestTemplateError as exc:
                print(f"Prompt {pid}: {exc}", file=sys.stderr)
                errors += 1
        if errors:
            return 2
        if not args.cases and not args.evalset and not args.list_evalsets:
            return 0

    # --- Evalset selection --------------------------------------------------
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
    print(f"Casos cargados: {len(cases)} ({counts['block']} block, {counts['allow']} allow)")

    if args.dry_run:
        print("Markdown válido. No se han enviado peticiones.")
        return 0

    config = BrowserConfig(
        url=args.url,
        profile_dir=args.profile_dir,
        output_dir=args.output_dir / selected_evalset.id,
        timeout_ms=args.timeout * 1000,
        headed=not args.headless,
        slow_mo_ms=args.slow_mo,
    )

    # --- Build checker and optional run context from API --------------------
    run_context: dict | None = None

    if args.adapter == "api":
        checker = await load_curl_checker(args.request_file, args.timeout, args.token_expiry)
        if checker is None:
            return 2
        if checker.token_expires_at is not None:
            remaining = max(0, int(checker.token_expires_at - time.time()))
            print(f"Token válido aproximadamente {remaining} segundos.")

        # Extract the Bearer token from the cURL headers to enrich the report
        # with company/prompt data — no extra step needed from the user.
        bearer = next(
            (v for k, v in checker.headers.items() if k.lower() == "authorization"),
            args.token or "",
        )
        if bearer.lower().startswith("bearer "):
            bearer = bearer[7:]
        if bearer:
            try:
                api_client = KarasenaAPIClient(
                    bearer, args.base_url, args.platform, args.timeout
                )
                run_context = _fetch_run_context(api_client)
                _print_run_context(run_context)
            except RequestTemplateError:
                pass  # enrichment is optional, don't abort

        results, completed_all = await execute_cases(
            cases, checker, config.output_dir, args.fail_fast, False,
            args.repetitions, args.token_expiry,
        )
    else:
        try:
            async with KarasenaChecker(config) as checker:
                await checker.open()
                await checker.ensure_checker_ready()
                results, completed_all = await execute_cases(
                    cases, checker, config.output_dir, args.fail_fast, True,
                    args.repetitions, "stop",
                )
        except BrowserSetupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    html_path, summary_csv_path, csv_path, json_path = write_reports(
        results, config.output_dir, run_context
    )
    passed = sum(result.status == "PASS" for result in results)
    print(f"\nResultado: {passed}/{len(results)} PASS")
    print(f"Informe HTML:  {html_path}")
    print(f"Resumen CSV:   {summary_csv_path}")
    print(f"Detalle CSV:   {csv_path}")
    print(f"JSON:          {json_path}")
    if not completed_all:
        print("La batería no terminó; el informe contiene únicamente pasadas completas.")
        return 2
    return 0 if all(result.status == "PASS" for result in results) else 1


# ---------------------------------------------------------------------------
# cURL checker loader
# ---------------------------------------------------------------------------

async def load_curl_checker(
    request_file: Path, timeout: int, token_expiry: str
) -> CurlKarasenaClient | None:
    while True:
        try:
            return CurlKarasenaClient(request_file, timeout)
        except RequestTemplateError as exc:
            if token_expiry != "pause" or not _is_token_expiry(exc):
                print(f"ERROR: {exc}", file=sys.stderr)
                return None
            print(f"\nTOKEN CADUCADO: {exc}")
            print(f"1. En Chrome DevTools copia la petición al comprobador como 'Copy as cURL (bash)'")
            print(f"2. Reemplaza el contenido de {request_file} con ese cURL")
            try:
                answer = await asyncio.to_thread(
                    input, "Pulsa ENTER cuando hayas guardado el nuevo cURL (o S para terminar): "
                )
            except (EOFError, KeyboardInterrupt):
                answer = "s"
            if answer.strip().lower() == "s":
                return None


def _is_token_expiry(exc: Exception) -> bool:
    if not isinstance(exc, RequestTemplateError):
        return False
    msg = str(exc).lower()
    return "token" in msg or "sesión ha caducado" in msg


# ---------------------------------------------------------------------------
# Optional report enrichment (only when --token is provided)
# ---------------------------------------------------------------------------

def _fetch_run_context(client: KarasenaAPIClient) -> dict:
    context: dict = {}
    try:
        company = client.get_my_company()
        if company:
            context["company_name"] = company.get("name", "")
            context["company_domain"] = company.get("dominio", "")
    except Exception:
        pass
    try:
        user_ctx = client.get_user_context()
        if user_ctx:
            context["user_role"] = user_ctx.get("tipoUsuario", "")
    except Exception:
        pass
    try:
        bt = client.get_blocking_types()
        prompts = client.get_prompts()
        if prompts:
            context["prompts"] = [
                {
                    "id": p.get("id"),
                    "nombre": p.get("nombre", ""),
                    "explicacion": p.get("explicacion", ""),
                    "comprobable": p.get("comprobable", False),
                    "tipo_bloqueo": bt.get(p.get("tipoBloqueoId", -1), ""),
                }
                for p in prompts
            ]
    except Exception:
        pass
    try:
        topics = client.get_prohibited_topics()
        if topics:
            context["temas_prohibidos"] = [
                {
                    "nombre": t.get("nombre", ""),
                    "codigo": t.get("codigo", ""),
                    "activado": t.get("activado", False),
                    "tipo_bloqueo": t.get("tipoBloqueoNombre", ""),
                }
                for t in topics
            ]
    except Exception:
        pass
    if client.platform_code:
        context["platform_code"] = client.platform_code
    context["base_url"] = client.base_url
    return context


def _print_run_context(context: dict) -> None:
    if context.get("company_name"):
        print(f"Empresa:    {context['company_name']}")
    if context.get("user_role"):
        print(f"Rol:        {context['user_role']}")
    if context.get("platform_code"):
        print(f"Plataforma: {context['platform_code']}")


# ---------------------------------------------------------------------------
# Case execution
# ---------------------------------------------------------------------------

async def execute_cases(
    cases: list[Any],
    checker: Any,
    output_dir: Path,
    fail_fast: bool,
    screenshots: bool,
    repetitions: int = 1,
    token_expiry: str = "pause",
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
                        print(f"\nTOKEN CADUCADO: {exc}")
                        if token_expiry == "pause" and hasattr(checker, "reload_from_file"):
                            print(f"Actualiza {checker.request_file} con un cURL nuevo.")
                            try:
                                answer = await asyncio.to_thread(
                                    input,
                                    "ENTER para reintentar este caso, S para terminar: ",
                                )
                            except (EOFError, KeyboardInterrupt):
                                answer = "s"
                            if answer.strip().lower() != "s":
                                try:
                                    checker.reload_from_file()
                                    print("cURL renovado. Reintentando...")
                                    continue
                                except RequestTemplateError as reload_exc:
                                    print(f"cURL no válido: {reload_exc}")
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


# ---------------------------------------------------------------------------
# Evalset resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nEjecución cancelada.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
