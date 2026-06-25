from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import CaseAggregate, RunSummary, TestResult


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list[int], p: float) -> int:
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return int(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def aggregate_cases(results: list[TestResult]) -> list[CaseAggregate]:
    aggregates: list[CaseAggregate] = []
    case_ids = list(dict.fromkeys(result.case.id for result in results))
    for case_id in case_ids:
        case_results = [r for r in results if r.case.id == case_id]
        case = case_results[0].case
        completed = [r for r in case_results if r.actual is not None]
        errors = sum(r.status == "ERROR" for r in case_results)
        block_count = sum(r.actual == "block" for r in completed)
        allow_count = sum(r.actual == "allow" for r in completed)
        correct_count = sum(r.actual == case.expected for r in completed)
        if errors:
            classification = "INCOMPLETE"
        elif correct_count == len(completed):
            classification = "STABLE_PASS"
        elif correct_count == 0:
            classification = "STABLE_FAIL"
        else:
            classification = "FLAKY"
        denom = len(completed)
        durations = [r.duration_ms for r in completed if r.duration_ms > 0]
        avg_ms = sum(durations) / len(durations) if durations else 0.0
        aggregates.append(CaseAggregate(
            id=case.id, group=case.group, title=case.title, prompt=case.prompt,
            expected=case.expected, attempts=len(case_results), completed=denom,
            errors=errors, block_count=block_count, allow_count=allow_count,
            correct_count=correct_count,
            correct_rate=correct_count / denom if denom else 0.0,
            block_rate=block_count / denom if denom else 0.0,
            allow_rate=allow_count / denom if denom else 0.0,
            classification=classification,
            avg_duration_ms=avg_ms,
        ))
    return aggregates


def summarize(results: list[TestResult]) -> RunSummary:
    aggregates = aggregate_cases(results)
    completed = [r for r in results if r.actual is not None]
    tp = sum(r.case.expected == "block" and r.actual == "block" for r in completed)
    tn = sum(r.case.expected == "allow" and r.actual == "allow" for r in completed)
    fp = sum(r.case.expected == "allow" and r.actual == "block" for r in completed)
    fn = sum(r.case.expected == "block" and r.actual == "allow" for r in completed)
    passed = sum(r.status == "PASS" for r in results)
    failed = sum(r.status == "FAIL" for r in results)
    errors = sum(r.status == "ERROR" for r in results)
    expected_blocks = tp + fn
    expected_allows = tn + fp
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / expected_blocks if expected_blocks else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    by_group: dict[str, dict] = {}
    for group in sorted({r.case.group for r in results}):
        gr = [r for r in results if r.case.group == group]
        gc = [r for r in gr if r.actual is not None]
        gp = sum(r.status == "PASS" for r in gr)
        by_group[group] = {
            "total": len(gr), "passed": gp,
            "failed": sum(r.status == "FAIL" for r in gr),
            "errors": sum(r.status == "ERROR" for r in gr),
            "accuracy": gp / len(gc) if gc else 0.0,
        }

    all_durations = sorted(r.duration_ms for r in completed if r.duration_ms > 0)
    return RunSummary(
        total=len(results), passed=passed, failed=failed, errors=errors,
        false_positives=fp, false_negatives=fn, true_positives=tp, true_negatives=tn,
        accuracy=(tp + tn) / len(completed) if completed else 0.0,
        precision=precision, block_recall=recall, f1=f1,
        fp_rate=fp / expected_allows if expected_allows else 0.0,
        prompts=len(aggregates),
        stable_pass=sum(a.classification == "STABLE_PASS" for a in aggregates),
        flaky=sum(a.classification == "FLAKY" for a in aggregates),
        stable_fail=sum(a.classification == "STABLE_FAIL" for a in aggregates),
        incomplete=sum(a.classification == "INCOMPLETE" for a in aggregates),
        avg_duration_ms=sum(all_durations) / len(all_durations) if all_durations else 0.0,
        p50_duration_ms=_percentile(all_durations, 50),
        p95_duration_ms=_percentile(all_durations, 95),
        max_duration_ms=all_durations[-1] if all_durations else 0,
        by_group=by_group,
    )


def aggregate_by_signal(results: list[TestResult]) -> list[dict]:
    stats: dict[str, dict] = {}
    for r in results:
        if r.actual is None:
            continue
        for signal in r.case.signals:
            if signal not in stats:
                stats[signal] = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
            s = stats[signal]
            exp, act = r.case.expected, r.actual
            if exp == "block" and act == "block":
                s["tp"] += 1
            elif exp == "allow" and act == "block":
                s["fp"] += 1
            elif exp == "block" and act == "allow":
                s["fn"] += 1
            else:
                s["tn"] += 1
    rows = []
    for signal, s in sorted(stats.items()):
        tp, fp, fn, tn = s["tp"], s["fp"], s["fn"], s["tn"]
        total = tp + fp + fn + tn
        accuracy = (tp + tn) / total if total else 0.0
        recall = tp / (tp + fn) if (tp + fn) else None
        precision = tp / (tp + fp) if (tp + fp) else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and (precision + recall) else None
        )
        rows.append({"signal": signal, "total": total,
                     "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                     "accuracy": accuracy, "recall": recall,
                     "precision": precision, "f1": f1})
    return rows


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------

def write_reports(
    results: list[TestResult],
    output_root: Path,
    run_context: dict | None = None,
) -> tuple[Path, Path, Path, Path]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = output_root / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    evalset_name = output_root.name
    summary = summarize(results)
    case_aggregates = aggregate_cases(results)
    signal_aggregates = aggregate_by_signal(results)

    json_payload: dict = {
        "generated_at": datetime.now().isoformat(),
        "evalset": evalset_name,
        "summary": asdict(summary),
        "case_aggregates": [asdict(a) for a in case_aggregates],
        "signal_aggregates": signal_aggregates,
        "results": [r.to_dict() for r in results],
    }
    if run_context:
        json_payload["run_context"] = run_context
    json_path = run_dir / "results.json"
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = run_dir / "results.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "group", "title", "prompt", "attempt",
            "expected", "actual", "status", "reason", "duration_ms", "screenshot", "error",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "id": r.case.id, "group": r.case.group, "title": r.case.title,
                "prompt": r.case.prompt, "attempt": r.attempt,
                "expected": r.case.expected, "actual": r.actual or "",
                "status": r.status, "reason": r.reason,
                "duration_ms": r.duration_ms, "screenshot": r.screenshot or "",
                "error": r.error or "",
            })

    summary_csv_path = run_dir / "summary.csv"
    with summary_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "group", "title", "prompt", "expected", "attempts", "completed", "errors",
            "block_count", "allow_count", "correct_count", "correct_rate",
            "block_rate", "allow_rate", "classification", "avg_duration_ms",
        ])
        writer.writeheader()
        for a in case_aggregates:
            row = asdict(a)
            for k in ("correct_rate", "block_rate", "allow_rate"):
                row[k] = round(row[k], 4)
            writer.writerow(row)

    html_path = run_dir / "report.html"
    write_html_report(results, html_path, evalset_name, run_context)
    return html_path, summary_csv_path, csv_path, json_path


def write_html_report(
    results: list[TestResult],
    path: Path,
    evalset_name: str,
    run_context: dict | None = None,
) -> None:
    summary = summarize(results)
    case_aggregates = aggregate_cases(results)
    path.write_text(
        _render_html(results, summary, case_aggregates, evalset_name, run_context),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _kpi_color(value: float, *, good: float = 0.9, warn: float = 0.7) -> str:
    return "good" if value >= good else ("warn" if value >= warn else "bad")


def _pct(v: float) -> str:
    return f"{v:.1%}"


def _render_html(
    results: list[TestResult],
    summary: RunSummary,
    case_aggregates: list[CaseAggregate],
    evalset_name: str,
    run_context: dict | None = None,
) -> str:
    signal_aggregates = aggregate_by_signal(results)
    ts = datetime.now().strftime("%d %b %Y — %H:%M")

    # ---- Header meta chips ----
    ctx = run_context or {}
    chips = [f"<span class='meta-chip'>{html.escape(ts)}</span>"]
    if ctx.get("company_name"):
        chips.append(f"<span class='meta-chip'>{html.escape(ctx['company_name'])}</span>")
    if ctx.get("platform_code"):
        chips.append(f"<span class='meta-chip'>{html.escape(ctx['platform_code'])}</span>")
    if ctx.get("user_role"):
        chips.append(f"<span class='meta-chip'>{html.escape(ctx['user_role'])}</span>")

    # ---- Hero KPIs (3 main metrics) ----
    pass_rate = summary.passed / summary.total if summary.total else 0.0
    hero = f"""
<div class="section-label">Rendimiento general</div>
<div class="kpi-grid kpi-hero">
  {_kpi_card("Accuracy", _pct(summary.accuracy), "De cada 100 textos, ¿cuántos clasificó correctamente?", _kpi_color(summary.accuracy), hero=True)}
  {_kpi_card("F1 Score", _pct(summary.f1), "Equilibrio entre detectar amenazas y evitar falsos bloqueos", _kpi_color(summary.f1), hero=True)}
  {_kpi_card("Pruebas superadas", f"{summary.passed}<span class='kpi-denom'>&thinsp;/&thinsp;{summary.total}</span>", "Intentos correctos sobre el total ejecutado", _kpi_color(pass_rate), hero=True)}
</div>"""

    # ---- Classification KPIs (4 cards) ----
    fn_cls = "bad" if summary.false_negatives > 0 else "good"
    fp_cls = "good" if summary.false_positives == 0 else "warn"
    clf = f"""
<div class="section-label">Clasificación</div>
<div class="kpi-grid">
  {_kpi_card("Precision", _pct(summary.precision), "De los bloqueos, ¿qué % eran realmente peligrosos?", _kpi_color(summary.precision))}
  {_kpi_card("Recall", _pct(summary.block_recall), "De los textos peligrosos, ¿qué % fueron detectados?", _kpi_color(summary.block_recall))}
  {_kpi_card("Falsos negativos", str(summary.false_negatives), "Textos peligrosos que pasaron sin ser bloqueados — riesgo crítico", fn_cls)}
  {_kpi_card("Falsos positivos", str(summary.false_positives), "Textos legítimos bloqueados por error — fricción innecesaria", fp_cls)}
</div>"""

    # ---- Stability KPIs (4 cards) ----
    sp_cls = "good" if summary.stable_pass == summary.prompts else "neutral"
    stab = f"""
<div class="section-label">Estabilidad del modelo</div>
<div class="kpi-grid">
  {_kpi_card("Estables correctos", f"{summary.stable_pass}<span class='kpi-denom'>&thinsp;/&thinsp;{summary.prompts}</span>", "Prompts en que acertó en todas las repeticiones", sp_cls)}
  {_kpi_card("Inestables (FLAKY)", str(summary.flaky), "Decisiones contradictorias entre repeticiones — impredecible en producción", "bad" if summary.flaky > 0 else "good")}
  {_kpi_card("Estables incorrectos", str(summary.stable_fail), "Puntos ciegos estructurales: siempre falla en estos casos", "bad" if summary.stable_fail > 0 else "good")}
  {_kpi_card("Incompletos", str(summary.incomplete), "Prompts con errores técnicos — resultados no fiables", "warn" if summary.incomplete > 0 else "neutral")}
</div>"""

    # ---- Timing KPIs (4 cards) ----
    def _ms(v: float) -> str:
        return f"{v/1000:.2f}s" if v >= 1000 else f"{v:.0f}ms"

    def _timing_color(ms: float) -> str:
        return "good" if ms < 1000 else ("warn" if ms < 3000 else "bad")

    timing = f"""
<div class="section-label">Tiempos de respuesta</div>
<div class="kpi-grid">
  {_kpi_card("Promedio", _ms(summary.avg_duration_ms), "Tiempo medio de respuesta por petición al comprobador", _timing_color(summary.avg_duration_ms))}
  {_kpi_card("Mediana (P50)", _ms(summary.p50_duration_ms), "El 50% de las peticiones se resolvió en menos de este tiempo", _timing_color(summary.p50_duration_ms))}
  {_kpi_card("Percentil 95 (P95)", _ms(summary.p95_duration_ms), "El 95% de las peticiones se resolvió en menos de este tiempo — indica el peor caso habitual", _timing_color(summary.p95_duration_ms))}
  {_kpi_card("Máximo", _ms(summary.max_duration_ms), "Petición más lenta registrada durante toda la sesión de pruebas", _timing_color(summary.max_duration_ms))}
</div>"""

    # ---- Signal section ----
    signal_section = _render_signal_cards(signal_aggregates)

    # ---- Config (prompts + topics) ----
    config_section = _render_prompt_config(run_context) if run_context else ""

    # ---- Group breakdown (collapsible, open by default) ----
    group_rows = "".join(
        f"<tr><td>{html.escape(g)}</td><td class='num'>{s['total']}</td>"
        f"<td class='num pass'>{s['passed']}</td><td class='num fail'>{s['failed']}</td>"
        f"<td class='num'>{s['errors']}</td><td class='num'>{s['accuracy']:.1%}</td></tr>"
        for g, s in summary.by_group.items()
    )
    group_section = f"""
<details class="detail-section" open>
  <summary>Resultados por grupo</summary>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Grupo</th><th>Total</th><th>PASS</th><th>FAIL</th><th>Error</th><th>Accuracy</th></tr></thead>
      <tbody>{group_rows}</tbody>
    </table>
  </div>
</details>"""

    # ---- Case aggregate (collapsible) ----
    def _fmt_ms(v: float) -> str:
        return f"{v/1000:.2f}s" if v >= 1000 else f"{v:.0f}ms"

    agg_rows = "".join(
        f"<tr class='cls-{a.classification.lower()}'>"
        f"<td><code>{html.escape(a.id)}</code></td>"
        f"<td>{html.escape(a.title)}</td>"
        f"<td class='prompt-cell'>{html.escape(a.prompt)}</td>"
        f"<td class='num'>{html.escape(a.expected)}</td>"
        f"<td class='num'>{a.completed}</td>"
        f"<td class='num'>{a.block_rate:.0%}</td>"
        f"<td class='num'>{a.allow_rate:.0%}</td>"
        f"<td class='num'>{a.correct_rate:.0%}</td>"
        f"<td class='num timing'>{_fmt_ms(a.avg_duration_ms) if a.avg_duration_ms else '—'}</td>"
        f"<td><span class='badge badge-{a.classification.lower()}'>{a.classification}</span></td>"
        f"</tr>"
        for a in case_aggregates
    )
    agg_section = f"""
<details class="detail-section">
  <summary>Detalle por caso ({len(case_aggregates)} prompts)</summary>
  <div class="table-wrap">
    <table>
      <thead><tr><th>ID</th><th>Caso</th><th>Prompt</th><th>Esperado</th>
      <th>Muestras</th><th>Block%</th><th>Allow%</th><th>Acierto</th><th>T. medio</th><th>Clasificación</th></tr></thead>
      <tbody>{agg_rows}</tbody>
    </table>
  </div>
</details>"""

    # ---- Evolution table (collapsible) ----
    attempts = sorted({r.attempt for r in results})
    attempt_headers = "".join(f"<th>R{a}</th>" for a in attempts)
    evo_rows = []
    for a in case_aggregates:
        by_att = {r.attempt: r for r in results if r.case.id == a.id}
        cells = []
        for att in attempts:
            r = by_att.get(att)
            if r is None:
                cells.append("<td class='dec dec-missing'>—</td>")
            elif r.actual in {"block", "allow"}:
                ok = r.actual == r.case.expected
                tip = html.escape(r.reason or r.error or "", quote=True)
                ttl = f" title='{tip}'" if tip else ""
                cells.append(
                    f"<td class='dec dec-{'pass' if ok else 'fail'}'{ttl}>"
                    f"{'✓' if ok else '✗'} {r.actual}</td>"
                )
            else:
                tip = html.escape(r.error or "", quote=True)
                cells.append(f"<td class='dec dec-error' title='{tip}'>ERR</td>")
        evo_rows.append(
            f"<tr><td><code>{html.escape(a.id)}</code></td>"
            f"<td>{html.escape(a.title)}</td>"
            f"<td class='prompt-cell'>{html.escape(a.prompt)}</td>"
            f"<td class='num'>{html.escape(a.expected)}</td>"
            f"{''.join(cells)}</tr>"
        )
    evo_section = f"""
<details class="detail-section">
  <summary>Evolución por repetición ({len(attempts)} pasadas)</summary>
  <div class="table-wrap">
    <table>
      <thead><tr><th>ID</th><th>Caso</th><th>Prompt</th><th>Esperado</th>
      {attempt_headers}</tr></thead>
      <tbody>{''.join(evo_rows)}</tbody>
    </table>
  </div>
</details>"""

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GuardIA — {html.escape(evalset_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <div class="container">
    <div class="header-top">
      <div>
        <div class="header-eyebrow">GuardIA LLM Tester</div>
        <div class="header-title">{html.escape(evalset_name)}</div>
      </div>
      <div class="meta-chips">{"".join(chips)}</div>
    </div>
  </div>
</header>
<main class="container">
  {hero}
  {clf}
  {stab}
  {timing}
  {signal_section}
  {config_section}
  {group_section}
  {agg_section}
  {evo_section}
</main>
</body>
</html>"""


def _kpi_card(label: str, value: str, desc: str, cls: str, *, hero: bool = False) -> str:
    return (
        f"<div class='kpi-card {cls}{'  kpi-hero-card' if hero else ''}'>"
        f"<div class='kpi-label'>{label}</div>"
        f"<div class='kpi-value'>{value}</div>"
        f"<div class='kpi-sub'>{desc}</div>"
        f"</div>"
    )


def _render_signal_cards(signal_aggregates: list[dict]) -> str:
    if not signal_aggregates:
        return ""
    cards = []
    for s in signal_aggregates:
        tp, fp, fn, tn = s["tp"], s["fp"], s["fn"], s["tn"]
        total = s["total"] or 1
        acc = s["accuracy"]
        acc_color = "#15803d" if acc >= 0.9 else ("#b45309" if acc >= 0.7 else "#b91c1c")
        rec = f"{s['recall']:.0%}" if s["recall"] is not None else "—"
        prec = f"{s['precision']:.0%}" if s["precision"] is not None else "—"
        f1 = f"{s['f1']:.0%}" if s["f1"] is not None else "—"
        bar = (
            f"<div class='signal-bar'>"
            f"<div class='bar-tp' style='width:{tp/total*100:.1f}%' title='TP={tp}'></div>"
            f"<div class='bar-fp' style='width:{fp/total*100:.1f}%' title='FP={fp}'></div>"
            f"<div class='bar-fn' style='width:{fn/total*100:.1f}%' title='FN={fn}'></div>"
            f"<div class='bar-tn' style='width:{tn/total*100:.1f}%' title='TN={tn}'></div>"
            f"</div>"
        )
        cards.append(f"""
<div class="signal-card">
  <div class="signal-name">{html.escape(s['signal'])}</div>
  <div class="signal-metrics">
    <div class="signal-metric">
      <span class="smv" style="color:{acc_color}">{acc:.0%}</span>
      <span class="sml">Accuracy</span>
    </div>
    <div class="signal-metric">
      <span class="smv">{rec}</span>
      <span class="sml">Recall</span>
    </div>
    <div class="signal-metric">
      <span class="smv">{prec}</span>
      <span class="sml">Precision</span>
    </div>
    <div class="signal-metric">
      <span class="smv">{f1}</span>
      <span class="sml">F1</span>
    </div>
  </div>
  {bar}
  <div class="signal-counts">
    <span class="cnt-tp">TP {tp}</span>
    <span class="cnt-fp">FP {fp}</span>
    <span class="cnt-fn">FN {fn}</span>
    <span class="cnt-tn">TN {tn}</span>
  </div>
</div>""")

    legend = (
        "<div class='signal-legend'>"
        "<span class='cnt-tp'>■ TP detectado correctamente</span>"
        "<span class='cnt-fp'>■ FP bloqueado por error</span>"
        "<span class='cnt-fn'>■ FN escapado sin detectar</span>"
        "<span class='cnt-tn'>■ TN permitido correctamente</span>"
        "</div>"
    )
    return (
        "<div class='section-label'>Rendimiento por tipo de señal</div>"
        f"{legend}"
        f"<div class='signal-grid'>{''.join(cards)}</div>"
    )


def _render_prompt_config(run_context: dict | None) -> str:
    if not run_context:
        return ""
    prompts: list[dict] = run_context.get("prompts", [])
    topics: list[dict] = run_context.get("temas_prohibidos", [])
    if not prompts and not topics:
        return ""

    panels = []

    if prompts:
        active = [p for p in prompts if p.get("comprobable")]
        rows = ""
        for p in sorted(prompts, key=lambda x: not x.get("comprobable")):
            on = p.get("comprobable")
            badge = f"<span class='badge {'badge-on' if on else 'badge-off'}'>{'activo' if on else 'inactivo'}</span>"
            nombre = html.escape(p.get("nombre") or "—")
            tipo = html.escape(p.get("tipo_bloqueo") or "—")
            rows += f"<tr><td>{badge}</td><td>{nombre}</td><td>{tipo}</td></tr>"
        panels.append(
            f"<div class='config-panel'>"
            f"<div class='config-panel-header'>Prompts de guardia"
            f"<span class='config-count'>{len(active)} activos / {len(prompts)} total</span></div>"
            f"<table><thead><tr><th>Estado</th><th>Nombre</th><th>Tipo bloqueo</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    if topics:
        active_t = [t for t in topics if t.get("activado")]
        rows_t = ""
        for t in sorted(topics, key=lambda x: not x.get("activado")):
            on = t.get("activado")
            badge = f"<span class='badge {'badge-on' if on else 'badge-off'}'>{'activo' if on else 'inactivo'}</span>"
            rows_t += (
                f"<tr><td>{badge}</td>"
                f"<td>{html.escape(t.get('nombre') or '—')}</td>"
                f"<td>{html.escape(t.get('codigo') or '—')}</td>"
                f"<td>{html.escape(t.get('tipo_bloqueo') or '—')}</td></tr>"
            )
        panels.append(
            f"<div class='config-panel'>"
            f"<div class='config-panel-header'>Temas prohibidos"
            f"<span class='config-count'>{len(active_t)} activos / {len(topics)} total</span></div>"
            f"<table><thead><tr><th>Estado</th><th>Nombre</th><th>Código</th><th>Tipo</th></tr></thead>"
            f"<tbody>{rows_t}</tbody></table></div>"
        )

    return (
        "<div class='section-label'>Configuración activa en Karasena</div>"
        f"<div class='config-grid'>{''.join(panels)}</div>"
    )


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#f1f5f9;color:#334155;font-size:14px;line-height:1.5}

/* ---- Header ---- */
header{background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 60%,#1d4ed8 100%);color:#fff;padding:1.75rem 0}
.header-top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:1.25rem}
.header-eyebrow{font-size:.75rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;opacity:.6;margin-bottom:.3rem}
.header-title{font-size:1.6rem;font-weight:700;letter-spacing:-.02em}
.meta-chips{display:flex;flex-wrap:wrap;gap:.4rem;align-items:flex-start;padding-top:.25rem}
.meta-chip{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.2);border-radius:2rem;padding:.2rem .75rem;font-size:.75rem;white-space:nowrap}

/* ---- Layout ---- */
.container{max-width:1440px;margin:0 auto;padding:0 2rem}
main.container{padding-top:2rem;padding-bottom:4rem}

/* ---- Section labels ---- */
.section-label{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#94a3b8;margin:2rem 0 .75rem}

/* ---- KPI grid & cards ---- */
.kpi-grid{display:grid;gap:.875rem}
.kpi-hero{grid-template-columns:repeat(3,1fr)}
.kpi-grid:not(.kpi-hero){grid-template-columns:repeat(4,1fr)}
@media(max-width:1100px){.kpi-grid:not(.kpi-hero){grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.kpi-grid,.kpi-hero{grid-template-columns:1fr!important}}

.kpi-card{background:#fff;border-radius:.75rem;padding:1.25rem 1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.07),0 1px 2px rgba(0,0,0,.04);border-left:4px solid #cbd5e1;display:flex;flex-direction:column;gap:.4rem}
.kpi-hero-card{padding:1.75rem 2rem}
.kpi-label{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b}
.kpi-value{font-size:2rem;font-weight:700;color:#0f172a;line-height:1}
.kpi-hero-card .kpi-value{font-size:2.75rem}
.kpi-denom{font-size:55%;font-weight:400;color:#94a3b8}
.kpi-sub{font-size:.72rem;color:#94a3b8;line-height:1.4;margin-top:.1rem}

.kpi-card.good{border-left-color:#22c55e}.kpi-card.good .kpi-value{color:#15803d}
.kpi-card.warn{border-left-color:#f59e0b}.kpi-card.warn .kpi-value{color:#b45309}
.kpi-card.bad{border-left-color:#ef4444}.kpi-card.bad .kpi-value{color:#b91c1c}
.kpi-card.neutral{border-left-color:#94a3b8}.kpi-card.neutral .kpi-value{color:#334155}

/* ---- Signal cards ---- */
.signal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:.875rem;margin-top:.25rem}
.signal-card{background:#fff;border-radius:.75rem;padding:1.25rem;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.signal-name{font-family:'SFMono-Regular','Consolas',monospace;font-size:.82rem;font-weight:700;color:#1e40af;margin-bottom:.875rem;word-break:break-all}
.signal-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:.25rem;margin-bottom:.875rem}
.signal-metric{text-align:center}
.smv{display:block;font-size:1.1rem;font-weight:700;color:#0f172a}
.sml{display:block;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;margin-top:.1rem}
.signal-bar{display:flex;height:8px;border-radius:4px;overflow:hidden;background:#f1f5f9}
.bar-tp{background:#22c55e}.bar-fp{background:#f59e0b}.bar-fn{background:#ef4444}.bar-tn{background:#cbd5e1}
.signal-counts{display:flex;gap:.75rem;margin-top:.5rem;font-size:.7rem;font-weight:600}
.cnt-tp{color:#15803d}.cnt-fp{color:#b45309}.cnt-fn{color:#b91c1c}.cnt-tn{color:#94a3b8}
.signal-legend{display:flex;flex-wrap:wrap;gap:1rem;margin-bottom:.75rem;font-size:.72rem;font-weight:600}

/* ---- Config panels ---- */
.config-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.25rem}
@media(max-width:900px){.config-grid{grid-template-columns:1fr}}
.config-panel{background:#fff;border-radius:.75rem;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.config-panel-header{padding:.75rem 1rem;background:#f8fafc;border-bottom:1px solid #e2e8f0;font-size:.8rem;font-weight:600;color:#475569;display:flex;justify-content:space-between;align-items:center}
.config-count{font-size:.72rem;font-weight:400;color:#94a3b8}

/* ---- Tables ---- */
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:#f8fafc;font-weight:600;color:#475569;text-align:left;padding:.6rem .875rem;border-bottom:2px solid #e2e8f0;position:sticky;top:0;white-space:nowrap}
td{padding:.55rem .875rem;border-bottom:1px solid #f1f5f9;vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#f8fafc}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.pass{color:#15803d;font-weight:600}
td.fail{color:#b91c1c;font-weight:600}
td.timing{color:#64748b;font-size:.75rem}
.prompt-cell{white-space:pre-wrap;max-width:34rem;line-height:1.45;color:#475569;font-size:.75rem}

/* ---- Badges ---- */
.badge{display:inline-block;padding:.15rem .55rem;border-radius:.3rem;font-size:.68rem;font-weight:700;text-transform:uppercase;white-space:nowrap}
.badge-stable_pass{background:#dcfce7;color:#15803d}
.badge-flaky{background:#fef3c7;color:#b45309}
.badge-stable_fail,.badge-incomplete{background:#fee2e2;color:#b91c1c}
.badge-on{background:#dcfce7;color:#15803d}
.badge-off{background:#f1f5f9;color:#94a3b8}

/* ---- Row classification tints ---- */
tr.cls-stable_fail td{background:#fff5f5}
tr.cls-flaky td{background:#fffbeb}

/* ---- Decision cells (evolution table) ---- */
.dec{text-align:center;white-space:nowrap;font-weight:600;font-size:.75rem;padding:.45rem .6rem!important}
.dec-pass{background:#dcfce7;color:#15803d}
.dec-fail{background:#fee2e2;color:#b91c1c}
.dec-error{background:#fef3c7;color:#b45309}
.dec-missing{text-align:center;color:#cbd5e1}

/* ---- Collapsible detail sections ---- */
.detail-section{margin-top:1.25rem;background:#fff;border-radius:.75rem;box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}
.detail-section summary{padding:.875rem 1.25rem;font-weight:600;font-size:.875rem;color:#334155;cursor:pointer;background:#f8fafc;border-bottom:1px solid #e2e8f0;list-style:none;display:flex;align-items:center;gap:.6rem;user-select:none}
.detail-section summary::before{content:'▶';font-size:.6rem;color:#94a3b8;transition:transform .2s;flex-shrink:0}
.detail-section[open] summary::before{transform:rotate(90deg)}
.detail-section summary::-webkit-details-marker{display:none}
.table-wrap{overflow-x:auto;max-height:65vh;overflow-y:auto}
.detail-section .table-wrap table{margin:0}
"""
