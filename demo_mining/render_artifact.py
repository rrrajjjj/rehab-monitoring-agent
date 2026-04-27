"""
Render windows_ranked.jsonl into a single self-contained HTML inspection page.

One row per (patient, focal_week), sorted by interest_score descending.
Columns: score, patient, focal week, focal session count, focal adherence,
peak pain, rule disposition, alert chips, 5 inline SVG sparklines.
Click a row to expand and see the literal prompt_text and the raw metrics JSON.

Usage:
  python -m demo_mining.render_artifact \
    --in demo_mining/windows_ranked.jsonl \
    --out demo_mining/inspection.html
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("demo_mining.render_artifact")


# ---------- sparkline helpers -------------------------------------------------


def _sparkline_svg(
    points: list[float],
    *,
    width: int = 110,
    height: int = 28,
    stroke: str = "#2563eb",
    fill: str = "none",
    ymin: float | None = None,
    ymax: float | None = None,
) -> str:
    if not points:
        return f'<svg width="{width}" height="{height}"></svg>'
    if ymin is None:
        ymin = min(points)
    if ymax is None:
        ymax = max(points)
    if ymax - ymin < 1e-9:
        ymax = ymin + 1.0
    n = len(points)
    if n == 1:
        cx, cy = width / 2, height / 2
        return (
            f'<svg width="{width}" height="{height}">'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2" fill="{stroke}"/></svg>'
        )
    pad_x, pad_y = 2, 3
    step = (width - 2 * pad_x) / (n - 1)
    coords = []
    for i, v in enumerate(points):
        x = pad_x + i * step
        y = height - pad_y - ((v - ymin) / (ymax - ymin)) * (height - 2 * pad_y)
        coords.append(f"{x:.1f},{y:.1f}")
    path = "M " + " L ".join(coords)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<path d="{path}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/></svg>'
    )


def _daily_agg_from_sessions(metrics: dict, key_in_row: str, key_on_metric: str) -> list[tuple[str, float]]:
    """Build (date, daily mean) series from performance/difficulty + session date lookup."""
    sess_by_id = {s["session_id"]: s for s in metrics.get("sessions") or []}
    bucket: dict[str, list[float]] = defaultdict(list)
    for p in metrics.get(key_in_row) or []:
        sid = p.get("session_id")
        dt = str((sess_by_id.get(sid) or {}).get("start_time", ""))[:10]
        if dt:
            try:
                bucket[dt].append(float(p.get(key_on_metric) or 0))
            except (TypeError, ValueError):
                pass
    return sorted((d, sum(v) / len(v)) for d, v in bucket.items())


def _series_for_sparklines(metrics: dict) -> dict[str, list[float]]:
    """Extract the five daily series the artifact shows."""
    # Adherence per day (done/planned ratio, clipped at 2.0 for visual sanity)
    adh_days = (metrics.get("adherence") or {}).get("days") or []
    adh_series: list[tuple[str, float]] = []
    for d in adh_days:
        planned = float(d.get("planned_min") or 0)
        done = float(d.get("done_min") or 0)
        ratio = (done / planned) if planned > 0 else 0.0
        adh_series.append((str(d.get("date", ""))[:10], min(ratio, 2.0)))
    adh_series.sort()

    perf_series = _daily_agg_from_sessions(metrics, "performance", "performance_mean")
    diff_series = _daily_agg_from_sessions(metrics, "difficulty", "difficulty_mean")

    # Pain daily max
    pain_bucket: dict[str, float] = {}
    for r in metrics.get("self_reports") or []:
        if str(r.get("key", "")).lower() != "pain":
            continue
        dt = str(r.get("timestamp", ""))[:10]
        try:
            v = float(r.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if dt:
            pain_bucket[dt] = max(pain_bucket.get(dt, 0.0), v)
    pain_series = sorted(pain_bucket.items())

    # Learning rate: one bar per protocol
    lr_series = [float(lr.get("learning_rate") or 0) for lr in (metrics.get("learning_rates") or [])]

    return {
        "adherence": [v for _, v in adh_series],
        "performance": [v for _, v in perf_series],
        "difficulty": [v for _, v in diff_series],
        "pain": [v for _, v in pain_series],
        "learning_rate": lr_series,
    }


# ---------- chip rendering ----------------------------------------------------


ALERT_COLORS = {
    "ADHERENCE_DROP": "#dc2626",
    "HIGH_PAIN": "#ea580c",
    "LEARNING_SATURATION": "#9333ea",
    "PROTOCOL_AVOIDANCE": "#d97706",
    "PROTOCOL_PERFORMANCE": "#0891b2",
    "PROTOCOL_PLATEAU": "#7c3aed",
    "CHECKIN_SIGNAL": "#16a34a",
    "DATA_ANOMALY": "#6b7280",
}


def _chip(alert: dict) -> str:
    color = ALERT_COLORS.get(alert["tag"], "#374151")
    severity = alert.get("severity", "")
    label = alert["tag"].replace("_", " ").title()
    detail = html.escape(alert.get("detail") or "")
    if severity == "severe":
        label += " ●"
    title = f"{alert['tag']} ({severity}): {detail}"
    return (
        f'<span class="chip" style="background:{color}" title="{html.escape(title)}">{label}</span>'
    )


# ---------- row rendering -----------------------------------------------------


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.0%}"


def _fmt_num(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _row_html(row: dict, idx: int, include_raw_metrics: bool) -> str:
    metrics = row["metrics"]
    focal = row["focal_stats"]
    series = _series_for_sparklines(metrics)

    adh_spark = _sparkline_svg(series["adherence"], stroke="#dc2626", ymin=0.0, ymax=1.5)
    perf_spark = _sparkline_svg(series["performance"], stroke="#2563eb", ymin=0.0, ymax=1.0)
    diff_spark = _sparkline_svg(series["difficulty"], stroke="#0891b2", ymin=0.0, ymax=1.0)
    pain_spark = _sparkline_svg(series["pain"], stroke="#ea580c", ymin=0.0, ymax=10.0)
    lr_spark = _sparkline_svg(series["learning_rate"], stroke="#9333ea")

    chips = "".join(_chip(a) for a in row.get("alerts") or [])
    if not chips:
        chips = '<span class="chip-none">no alerts</span>'

    prompt = html.escape(row.get("prompt_text") or "")
    metrics_pane = ""
    if include_raw_metrics:
        metrics_json = html.escape(json.dumps(metrics, indent=2, default=str))
        metrics_pane = f"""
      <div class="detail-section">
        <h4>Full metrics JSON</h4>
        <pre class="metrics">{metrics_json}</pre>
      </div>"""

    return f"""
<tr class="main-row" data-score="{row['interest_score']}" onclick="toggleRow({idx})">
  <td class="score">{row['interest_score']:.2f}</td>
  <td class="pid">{row['patient_id']}</td>
  <td class="week">{row['focal_week_start']}</td>
  <td class="num">{focal.get('focal_session_count', 0)}</td>
  <td class="num">{_fmt_pct(focal.get('focal_adherence'))}</td>
  <td class="num">{_fmt_num(focal.get('focal_max_pain'), 0)}</td>
  <td class="rule">{html.escape(row.get('rule_disposition') or '')}</td>
  <td class="chips">{chips}</td>
  <td class="spark">{adh_spark}</td>
  <td class="spark">{perf_spark}</td>
  <td class="spark">{diff_spark}</td>
  <td class="spark">{pain_spark}</td>
  <td class="spark">{lr_spark}</td>
</tr>
<tr class="detail-row" id="detail-{idx}" style="display:none">
  <td colspan="13">
    <div class="detail-wrap">
      <div class="detail-section">
        <h4>Prompt text (literal input the LLM would see)</h4>
        <pre class="prompt">{prompt}</pre>
      </div>{metrics_pane}
    </div>
  </td>
</tr>
"""


# ---------- page -------------------------------------------------------------


_PAGE_CSS = """
* { box-sizing: border-box; }
body { font: 13px -apple-system,Segoe UI,Arial,sans-serif; margin: 16px; color: #111; }
h1 { margin: 0 0 8px 0; font-size: 18px; }
.meta { color: #666; font-size: 12px; margin-bottom: 12px; }
.controls { margin: 8px 0; }
.controls input, .controls select { font-size: 12px; padding: 3px 6px; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; font-weight: 600; background: #f3f4f6; padding: 6px 6px; font-size: 11px; text-transform: uppercase; color: #374151; cursor: pointer; position: sticky; top: 0; }
td { padding: 6px 6px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
tr.main-row { cursor: pointer; }
tr.main-row:hover td { background: #f9fafb; }
.score { font-weight: 600; font-variant-numeric: tabular-nums; }
.pid { font-variant-numeric: tabular-nums; }
.week { font-variant-numeric: tabular-nums; color: #374151; }
.num { font-variant-numeric: tabular-nums; text-align: right; width: 55px; }
.rule { font-size: 11px; color: #6b7280; }
.chips { max-width: 360px; }
.spark { padding: 2px 2px; }
.chip { display: inline-block; color: white; font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 10px; margin: 1px 2px; white-space: nowrap; }
.chip-none { color: #9ca3af; font-size: 10px; font-style: italic; }
.detail-row td { background: #fafbfc; padding: 0; }
.detail-wrap { padding: 14px 24px; }
.detail-wrap:has(.detail-section + .detail-section) { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.detail-section h4 { margin: 0 0 6px 0; font-size: 11px; text-transform: uppercase; color: #6b7280; }
pre.prompt, pre.metrics { background: white; border: 1px solid #e5e7eb; border-radius: 4px; padding: 10px; font-size: 11px; max-height: 420px; overflow: auto; white-space: pre-wrap; word-wrap: break-word; }
pre.metrics { white-space: pre; }
"""

_PAGE_JS = """
function toggleRow(idx) {
  var d = document.getElementById('detail-' + idx);
  if (!d) return;
  d.style.display = (d.style.display === 'none') ? 'table-row' : 'none';
}
function filterRows() {
  var q = document.getElementById('filter').value.toLowerCase();
  var rows = document.querySelectorAll('tr.main-row');
  rows.forEach(function(r, i) {
    var txt = r.textContent.toLowerCase();
    var match = !q || txt.indexOf(q) >= 0;
    r.style.display = match ? '' : 'none';
    var d = document.getElementById('detail-' + i);
    if (d && !match) d.style.display = 'none';
  });
}
function sortBy(col) {
  var tbody = document.querySelector('tbody');
  var mains = Array.from(tbody.querySelectorAll('tr.main-row'));
  var details = {};
  mains.forEach(function(r, i) { details[i] = document.getElementById('detail-' + i); });
  var asc = tbody.getAttribute('data-sort') !== col + '-asc';
  mains.sort(function(a, b) {
    var av = a.children[col].textContent.trim();
    var bv = b.children[col].textContent.trim();
    var af = parseFloat(av.replace('%','')); var bf = parseFloat(bv.replace('%',''));
    if (!isNaN(af) && !isNaN(bf)) { return asc ? af - bf : bf - af; }
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  });
  tbody.innerHTML = '';
  mains.forEach(function(r, i) {
    tbody.appendChild(r);
    var d = document.getElementById('detail-' + r.getAttribute('data-detail-idx'));
    if (d) tbody.appendChild(d);
  });
  tbody.setAttribute('data-sort', col + (asc ? '-asc' : '-desc'));
}
"""


def render(in_path: Path, out_path: Path, include_raw_metrics: bool = False) -> int:
    rows = [json.loads(line) for line in in_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    logger.info("Rendering %d rows (raw_metrics=%s)", len(rows), include_raw_metrics)

    body_rows = "".join(_row_html(r, i, include_raw_metrics) for i, r in enumerate(rows))

    headers = [
        "Score", "Patient", "Focal week", "Sess", "Adh %", "Max pain",
        "Rule", "Alerts", "Adh", "Perf", "Diff", "Pain", "LR",
    ]
    header_html = "".join(f"<th>{html.escape(h)}</th>" for h in headers)

    html_doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Demo mining inspection ({len(rows)} windows)</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<h1>Demo mining inspection — {len(rows)} windows</h1>
<div class="meta">
  Ranked by interest score (desc). Click a row to expand the literal prompt text
  and raw metrics JSON. Use the filter to narrow by patient id, alert tag, or week.
  Sparklines (left→right by date): <b>Adh</b> red (planned-vs-done ratio, 0–1.5),
  <b>Perf</b> blue (0–1), <b>Diff</b> cyan (0–1), <b>Pain</b> orange (0–10),
  <b>LR</b> purple (per-protocol learning rate bars).
</div>
<div class="controls">
  <input id="filter" type="text" placeholder="filter: patient id, alert tag, date…" oninput="filterRows()" style="width: 380px;">
</div>
<table>
  <thead><tr>{header_html}</tr></thead>
  <tbody>
{body_rows}
  </tbody>
</table>
<script>{_PAGE_JS}</script>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    logger.info("Wrote %s (%d bytes)", out_path, out_path.stat().st_size)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render demo mining HTML inspection page")
    p.add_argument("--in", dest="in_path", default="demo_mining/windows_ranked.jsonl")
    p.add_argument("--out", dest="out_path", default="demo_mining/inspection.html")
    p.add_argument("--include-raw-metrics", action="store_true", help="Embed full metrics JSON in each detail pane (large output)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    render(Path(args.in_path), Path(args.out_path), include_raw_metrics=args.include_raw_metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
