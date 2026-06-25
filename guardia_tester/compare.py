"""Comparison report between two GuardIA eval runs.

Usage:
    python run_compare.py results/datos-medicos/20240101-120000 results/datos-medicos/20240102-093000
    python run_compare.py <dir_a> <dir_b> --output comparison.html
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_run(path: Path) -> dict:
    """Load a results.json from a run directory or file path."""
    json_path = path / "results.json" if path.is_dir() else path
    if not json_path.is_file():
        raise FileNotFoundError(f"No se encontró results.json en {path}")
    return json.loads(json_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _prompt_names(run: dict) -> dict[int, str]:
    """Return {id: nombre} for prompts in run_context, if present."""
    prompts = (run.get("run_context") or {}).get("prompts", [])
    return {p["id"]: p.get("nombre", str(p["id"])) for p in prompts if "id" in p}


def _active_prompt_ids(run: dict) -> set[int]:
    prompts = (run.get("run_context") or {}).get("prompts", [])
    return {p["id"] for p in prompts if p.get("comprobable")}


def _prompt_diff(run_a: dict, run_b: dict) -> list[dict]:
    """Which prompts changed activation status between A and B."""
    names_a = _prompt_names(run_a)
    names_b = _prompt_names(run_b)
    active_a = _active_prompt_ids(run_a)
    active_b = _active_prompt_ids(run_b)
    all_ids = set(names_a) | set(names_b)
    changes = []
    for pid in sorted(all_ids):
        in_a = pid in active_a
        in_b = pid in active_b
        if in_a != in_b:
            changes.append({
                "id": pid,
                "nombre": names_b.get(pid) or names_a.get(pid) or str(pid),
                "estado_a": "activo" if in_a else "inactivo",
                "estado_b": "activo" if in_b else "inactivo",
            })
    return changes


def _kpi_table(run_a: dict, run_b: dict) -> list[dict]:
    sa = run_a["summary"]
    sb = run_b["summary"]
    rows = []
    metrics = [
        ("Accuracy",      "accuracy",     True,  "De cada 100 textos analizados, ¿cuántos clasificó correctamente?"),
        ("F1 Score",       "f1",           True,  "Puntuación equilibrada entre detectar lo peligroso y no bloquear lo legítimo."),
        ("Recall",         "block_recall", True,  "De cada 100 textos peligrosos, ¿cuántos detectó y bloqueó?"),
        ("Precision",      "precision",    True,  "De cada 100 textos bloqueados, ¿cuántos eran realmente peligrosos?"),
        ("Falsos negativos","false_negatives", False, "Textos peligrosos que se escaparon sin ser detectados."),
        ("Falsos positivos","false_positives", False, "Textos legítimos bloqueados por error."),
        ("Errores técnicos","errors",      False, "Intentos sin respuesta del sistema."),
    ]
    for label, key, higher_is_better, desc in metrics:
        va = sa.get(key, 0)
        vb = sb.get(key, 0)
        delta = vb - va
        if delta == 0:
            direction = "neutral"
        elif (delta > 0) == higher_is_better:
            direction = "better"
        else:
            direction = "worse"
        rows.append({
            "label": label, "desc": desc,
            "a": va, "b": vb, "delta": delta,
            "pct": isinstance(va, float),
            "direction": direction,
        })
    return rows


def _signal_diff(run_a: dict, run_b: dict) -> list[dict]:
    def index(run: dict) -> dict[str, dict]:
        return {s["signal"]: s for s in run.get("signal_aggregates", [])}

    idx_a = index(run_a)
    idx_b = index(run_b)
    all_signals = sorted(set(idx_a) | set(idx_b))
    rows = []
    for sig in all_signals:
        sa = idx_a.get(sig, {})
        sb = idx_b.get(sig, {})
        rows.append({
            "signal": sig,
            "acc_a": sa.get("accuracy"), "acc_b": sb.get("accuracy"),
            "rec_a": sa.get("recall"),   "rec_b": sb.get("recall"),
            "f1_a":  sa.get("f1"),       "f1_b":  sb.get("f1"),
            "fn_a":  sa.get("fn", 0),    "fn_b":  sb.get("fn", 0),
        })
    return rows


def _case_diff(run_a: dict, run_b: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (regressions, improvements, unchanged_fail)."""
    def index(run: dict) -> dict[str, dict]:
        return {c["id"]: c for c in run.get("case_aggregates", [])}

    idx_a = index(run_a)
    idx_b = index(run_b)
    all_ids = sorted(set(idx_a) | set(idx_b))

    regressions, improvements, unchanged_fail = [], [], []
    for cid in all_ids:
        ca = idx_a.get(cid)
        cb = idx_b.get(cid)
        cls_a = ca["classification"] if ca else "—"
        cls_b = cb["classification"] if cb else "—"
        ok_a = cls_a == "STABLE_PASS"
        ok_b = cls_b == "STABLE_PASS"
        entry = {
            "id": cid,
            "title": (cb or ca or {}).get("title", ""),
            "expected": (cb or ca or {}).get("expected", ""),
            "cls_a": cls_a, "cls_b": cls_b,
            "acc_a": (ca or {}).get("correct_rate", 0),
            "acc_b": (cb or {}).get("correct_rate", 0),
        }
        if ok_a and not ok_b:
            regressions.append(entry)
        elif not ok_a and ok_b:
            improvements.append(entry)
        elif not ok_a and not ok_b:
            unchanged_fail.append(entry)
    return regressions, improvements, unchanged_fail


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _fmt_pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


def _delta_cell(delta: float, pct: bool, direction: str) -> str:
    sign = "+" if delta >= 0 else ""
    val = f"{sign}{delta:.1%}" if pct else f"{sign}{delta:+.0f}"
    color = {"better": "#087f23", "worse": "#c62828", "neutral": "#6b7280"}[direction]
    arrow = {"better": "▲", "worse": "▼", "neutral": ""}[direction]
    return f"<td style='color:{color};font-weight:700'>{arrow} {val}</td>"


def _cls_badge(cls: str) -> str:
    colors = {
        "STABLE_PASS": "#087f23", "FLAKY": "#b26a00",
        "STABLE_FAIL": "#c62828", "INCOMPLETE": "#9ca3af", "—": "#9ca3af",
    }
    return f"<span style='color:{colors.get(cls,\"#374151\")};font-weight:600'>{cls}</span>"


def generate_html(run_a: dict, run_b: dict, label_a: str, label_b: str) -> str:
    prompt_changes = _prompt_diff(run_a, run_b)
    kpi_rows = _kpi_table(run_a, run_b)
    signal_rows = _signal_diff(run_a, run_b)
    regressions, improvements, unchanged_fail = _case_diff(run_a, run_b)

    evalset = run_a.get("evalset") or run_b.get("evalset") or "—"

    # -- prompt diff section --
    if prompt_changes:
        pdiff_rows = "".join(
            f"<tr><td>{p['id']}</td><td>{html.escape(p['nombre'])}</td>"
            f"<td>{p['estado_a']}</td><td>→</td><td>{p['estado_b']}</td></tr>"
            for p in prompt_changes
        )
        prompt_section = (
            "<h2>Cambios en configuración de prompts</h2>"
            "<p class='desc'>Prompts cuyo estado activo/inactivo difiere entre las dos ejecuciones. "
            "Estos cambios son la causa más probable de las diferencias en los KPIs.</p>"
            "<table><thead><tr><th>ID</th><th>Nombre</th>"
            f"<th>{html.escape(label_a)}</th><th></th><th>{html.escape(label_b)}</th></tr></thead>"
            f"<tbody>{pdiff_rows}</tbody></table>"
        )
    else:
        prompt_section = (
            "<h2>Cambios en configuración de prompts</h2>"
            "<p class='desc' style='color:#6b7280'>No se detectaron cambios en los prompts activos entre las dos ejecuciones "
            "(o no hay información de contexto disponible en los resultados).</p>"
        )

    # -- kpi section --
    kpi_rows_html = "".join(
        f"<tr><td><strong>{html.escape(r['label'])}</strong><br>"
        f"<span class='desc'>{html.escape(r['desc'])}</span></td>"
        f"<td class='num'>{_fmt_pct(r['a']) if r['pct'] else r['a']}</td>"
        f"<td class='num'>{_fmt_pct(r['b']) if r['pct'] else r['b']}</td>"
        f"{_delta_cell(r['delta'], r['pct'], r['direction'])}"
        f"</tr>"
        for r in kpi_rows
    )

    # -- signal section --
    def _sig_delta(a: float | None, b: float | None) -> str:
        if a is None or b is None:
            return "<td class='num' style='color:#9ca3af'>—</td>"
        d = b - a
        color = "#087f23" if d > 0.01 else ("#c62828" if d < -0.01 else "#6b7280")
        sign = "+" if d >= 0 else ""
        return f"<td class='num' style='color:{color};font-weight:600'>{sign}{d:.0%}</td>"

    sig_rows_html = "".join(
        f"<tr><td><code>{html.escape(s['signal'])}</code></td>"
        f"<td class='num'>{_fmt_pct(s['acc_a'])}</td>"
        f"<td class='num'>{_fmt_pct(s['acc_b'])}</td>"
        f"{_sig_delta(s['acc_a'], s['acc_b'])}"
        f"<td class='num'>{_fmt_pct(s['rec_a'])}</td>"
        f"<td class='num'>{_fmt_pct(s['rec_b'])}</td>"
        f"{_sig_delta(s['rec_a'], s['rec_b'])}"
        f"<td class='num'>{_fmt_pct(s['f1_a'])}</td>"
        f"<td class='num'>{_fmt_pct(s['f1_b'])}</td>"
        f"{_sig_delta(s['f1_a'], s['f1_b'])}"
        f"<td class='num fn'>{s['fn_a']}</td>"
        f"<td class='num fn'>{s['fn_b']}</td>"
        f"</tr>"
        for s in signal_rows
    )

    # -- case diff sections --
    def _case_rows(cases: list[dict]) -> str:
        return "".join(
            f"<tr><td>{html.escape(c['id'])}</td>"
            f"<td>{html.escape(c['title'])}</td>"
            f"<td>{c['expected']}</td>"
            f"<td>{_cls_badge(c['cls_a'])}</td>"
            f"<td>{_cls_badge(c['cls_b'])}</td>"
            f"<td class='num'>{c['acc_a']:.0%}</td>"
            f"<td class='num'>{c['acc_b']:.0%}</td>"
            f"</tr>"
            for c in cases
        )

    case_header = (
        f"<thead><tr><th>ID</th><th>Caso</th><th>Esperado</th>"
        f"<th>{html.escape(label_a)}</th><th>{html.escape(label_b)}</th>"
        f"<th>Acc A</th><th>Acc B</th></tr></thead>"
    )

    regression_section = (
        f"<h3 style='color:#c62828'>Regresiones — {len(regressions)} caso(s) que empeoraron</h3>"
        f"<p class='desc'>Estos casos eran correctos en la configuración A y han dejado de serlo en B.</p>"
        + (f"<table>{case_header}<tbody>{_case_rows(regressions)}</tbody></table>" if regressions
           else "<p style='color:#6b7280'>Ninguna regresión detectada.</p>")
    )
    improvement_section = (
        f"<h3 style='color:#087f23'>Mejoras — {len(improvements)} caso(s) que mejoraron</h3>"
        f"<p class='desc'>Estos casos fallaban en la configuración A y ahora son correctos en B.</p>"
        + (f"<table>{case_header}<tbody>{_case_rows(improvements)}</tbody></table>" if improvements
           else "<p style='color:#6b7280'>Ninguna mejora detectada.</p>")
    )
    persistent_section = (
        f"<h3 style='color:#b26a00'>Fallos persistentes — {len(unchanged_fail)} caso(s)</h3>"
        f"<p class='desc'>Estos casos fallaron en ambas configuraciones. Son puntos ciegos del sistema independientes de los prompts modificados.</p>"
        + (f"<table>{case_header}<tbody>{_case_rows(unchanged_fail)}</tbody></table>" if unchanged_fail
           else "<p style='color:#6b7280'>No hay fallos persistentes.</p>")
    )

    return f"""<!doctype html>
<html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
<title>Comparación GuardIA — {html.escape(evalset)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1f2937}}
h1,h2,h3{{margin-bottom:.4rem}} h2{{margin-top:2rem;border-bottom:2px solid #e5e7eb;padding-bottom:.3rem}}
table{{border-collapse:collapse;width:100%;margin-bottom:1.5rem}}
th,td{{padding:.55rem .7rem;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
th{{background:#f9fafb;font-size:.82rem;text-transform:uppercase;letter-spacing:.03em}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.fn{{color:#c62828;font-weight:700}}
.desc{{font-size:.8rem;color:#6b7280;margin:.2rem 0 0;line-height:1.4}}
.header-grid{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:1rem 0 2rem;
  border:1px solid #e5e7eb;border-radius:.5rem;padding:1rem;background:#f9fafb}}
.run-label{{font-weight:700;font-size:1.05rem}} .run-meta{{font-size:.82rem;color:#6b7280;margin-top:.2rem}}
.table-scroll{{overflow-x:auto}}
code{{background:#f3f4f6;padding:.1rem .3rem;border-radius:.25rem;font-size:.85rem}}
</style></head>
<body>
<h1>Comparación de configuraciones — {html.escape(evalset)}</h1>
<div class='header-grid'>
  <div>
    <div class='run-label'>A: {html.escape(label_a)}</div>
    <div class='run-meta'>Generado: {run_a.get('generated_at','—')}</div>
    <div class='run-meta'>Casos: {run_a.get('summary',{{}}).get('total','—')} intentos</div>
  </div>
  <div>
    <div class='run-label'>B: {html.escape(label_b)}</div>
    <div class='run-meta'>Generado: {run_b.get('generated_at','—')}</div>
    <div class='run-meta'>Casos: {run_b.get('summary',{{}}).get('total','—')} intentos</div>
  </div>
</div>

{prompt_section}

<h2>Comparación de KPIs</h2>
<p class='desc'>▲ verde = mejora · ▼ rojo = regresión · columna delta referenciada a la configuración B.</p>
<table>
<thead><tr><th>KPI</th><th class='num'>A</th><th class='num'>B</th><th class='num'>Δ B−A</th></tr></thead>
<tbody>{kpi_rows_html}</tbody>
</table>

<h2>Comparación por tipo de señal</h2>
<p class='desc'>FN = falsos negativos (fugas). Cuanto más bajos, mejor. El delta en verde indica mejora.</p>
<div class='table-scroll'><table>
<thead><tr>
  <th>Señal</th>
  <th class='num'>Acc A</th><th class='num'>Acc B</th><th class='num'>Δ Acc</th>
  <th class='num'>Rec A</th><th class='num'>Rec B</th><th class='num'>Δ Rec</th>
  <th class='num'>F1 A</th><th class='num'>F1 B</th><th class='num'>Δ F1</th>
  <th class='num'>FN A</th><th class='num'>FN B</th>
</tr></thead>
<tbody>{sig_rows_html}</tbody>
</table></div>

<h2>Análisis caso a caso</h2>
{regression_section}
{improvement_section}
{persistent_section}

</body></html>"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compara dos ejecuciones del evalset de GuardIA y genera un informe HTML."
    )
    parser.add_argument("run_a", type=Path, help="Directorio o results.json de la configuración A")
    parser.add_argument("run_b", type=Path, help="Directorio o results.json de la configuración B")
    parser.add_argument("--label-a", default=None, help="Etiqueta para la configuración A")
    parser.add_argument("--label-b", default=None, help="Etiqueta para la configuración B")
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Ruta del informe de salida (predeterminado: comparison.html junto a run_b)",
    )
    args = parser.parse_args()

    try:
        run_a = load_run(args.run_a)
        run_b = load_run(args.run_b)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2

    label_a = args.label_a or args.run_a.name
    label_b = args.label_b or args.run_b.name

    output = args.output or (
        (args.run_b if args.run_b.is_dir() else args.run_b.parent) / "comparison.html"
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate_html(run_a, run_b, label_a, label_b), encoding="utf-8")
    print(f"Informe de comparación generado: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
