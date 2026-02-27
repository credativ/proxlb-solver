"""
HTML report generator for ProxLB shadow-mode JSONL logs.

Reads all ``solver_run_*.jsonl`` files from a log directory and writes a
self-contained HTML report (no CDN, no external assets) to an output directory:

    output-dir/
        index.html          — overview table of all runs
        run_YYYYMMDD_HHmmSS.html  — detail page per run

CLI:
    proxlb-solver-report --log-dir /var/log/proxlb/solver --output-dir /var/www/solver

Python API:
    from proxlb_solver.shadow_reporter import generate_report
    generate_report(log_dir=Path("/var/log/proxlb/solver"),
                    output_dir=Path("/var/www/solver"))
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from html import escape as h
from pathlib import Path
from typing import Any

__all__ = ["generate_report"]


# ---------------------------------------------------------------------------
# Shared CSS (dark-first, respects prefers-color-scheme for light mode)
# ---------------------------------------------------------------------------

_CSS = """
:root {
    --bg:        #0f172a;
    --surface:   #1e293b;
    --surface2:  #273549;
    --border:    #334155;
    --text:      #e2e8f0;
    --muted:     #94a3b8;
    --accent:    #3b82f6;
    --green:     #22c55e;
    --yellow:    #eab308;
    --red:       #ef4444;
    --orange:    #f97316;
    --blue:      #60a5fa;
    --purple:    #a78bfa;
    --radius:    8px;
    --shadow:    0 1px 3px rgba(0,0,0,.4);
}
@media (prefers-color-scheme: light) {
    :root {
        --bg:       #f1f5f9;
        --surface:  #ffffff;
        --surface2: #f8fafc;
        --border:   #e2e8f0;
        --text:     #0f172a;
        --muted:    #64748b;
        --shadow:   0 1px 3px rgba(0,0,0,.1);
    }
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    font-size: 14px;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code {
    font-family: "SF Mono", "Fira Code", "Cascadia Code", Consolas, monospace;
    font-size: 12px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
}

/* ── Layout ────────────────────────────────────────────────────────────── */
.container { max-width: 1100px; margin: 0 auto; padding: 28px 20px; }

/* ── Page header ───────────────────────────────────────────────────────── */
.page-header { margin-bottom: 24px; }
.page-header h1 {
    font-size: 20px; font-weight: 700; display: flex; align-items: center; gap: 10px;
}
.page-header .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }

/* ── Breadcrumb ────────────────────────────────────────────────────────── */
.breadcrumb {
    font-size: 12px; color: var(--muted); margin-bottom: 12px;
}
.breadcrumb a { color: var(--muted); }

/* ── Summary cards ─────────────────────────────────────────────────────── */
.cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
}
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 14px 16px;
}
.card .label {
    font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .6px; color: var(--muted);
}
.card .value {
    font-size: 26px; font-weight: 700; margin-top: 4px; line-height: 1;
}

/* ── Badges ────────────────────────────────────────────────────────────── */
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .4px;
}
.b-green  { background: #14532d44; color: var(--green);  border: 1px solid #14532d; }
.b-yellow { background: #713f1244; color: var(--yellow); border: 1px solid #713f12; }
.b-red    { background: #7f1d1d44; color: var(--red);    border: 1px solid #7f1d1d; }
.b-orange { background: #7c2d1244; color: var(--orange); border: 1px solid #7c2d12; }
.b-blue   { background: #1e3a5f44; color: var(--blue);   border: 1px solid #1e3a5f; }
.b-muted  { background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }

/* ── Sections ──────────────────────────────────────────────────────────── */
.section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 20px;
    overflow: hidden;
}
.section-title {
    display: flex; align-items: center; gap: 8px;
    padding: 11px 16px;
    border-bottom: 1px solid var(--border);
    font-weight: 600; font-size: 13px;
    background: var(--surface2);
}
.section-title .count {
    margin-left: auto;
    font-weight: 400; font-size: 12px; color: var(--muted);
}

/* ── Tables ────────────────────────────────────────────────────────────── */
.tbl-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
    text-align: left; padding: 7px 14px;
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .5px; color: var(--muted);
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
}
td { padding: 9px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: rgba(255,255,255,.03); }

/* ── Comparison result colours ─────────────────────────────────────────── */
.cmp-agree  { color: var(--green); }
.cmp-differ { color: var(--orange); }
.cmp-solver { color: var(--blue); }
.cmp-proxlb { color: var(--purple); }

/* ── Origin chips ──────────────────────────────────────────────────────── */
.chip {
    display: inline-block; padding: 1px 7px; border-radius: 4px;
    font-size: 10px; font-weight: 600; margin: 1px 2px 1px 0;
}
.chip-pve  { background: #1e3a5f55; color: #93c5fd; border: 1px solid #1e3a5f; }
.chip-plb  { background: #14532d55; color: #86efac; border: 1px solid #14532d; }
.chip-tag  { background: #2d1b6955; color: #c4b5fd; border: 1px solid #2d1b69; }
.chip-pool { background: #27272a55; color: #a1a1aa; border: 1px solid #3f3f46; }

/* ── Error block ───────────────────────────────────────────────────────── */
.errbox {
    background: #7f1d1d22; border: 1px solid #7f1d1d88;
    border-radius: var(--radius); padding: 14px;
    font-family: monospace; font-size: 12px;
    white-space: pre-wrap; word-break: break-all;
    color: #fca5a5;
}

/* ── Misc ──────────────────────────────────────────────────────────────── */
.empty { padding: 28px; text-align: center; color: var(--muted); }
.mono  { font-family: "SF Mono", "Fira Code", Consolas, monospace; font-size: 12px; }
.sub-heading {
    padding: 8px 16px 3px;
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .5px; color: var(--muted);
}

/* ── Load bars ─────────────────────────────────────────────────────────── */
.load-cell { display: flex; align-items: center; gap: 8px; min-width: 180px; }
.bar-track {
    flex: 1; min-width: 80px; height: 6px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 3px; overflow: hidden;
}
.bar-fill          { height: 100%; border-radius: 3px; transition: width .2s; }
.bar-fill-neutral  { background: var(--muted); }
.bar-fill-better   { background: var(--green); }
.bar-fill-worse    { background: var(--orange); }
"""


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{h(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        f"<body>\n{body}\n</body>\n</html>"
    )


def _status_badge(status: str) -> str:
    s = (status or "").upper()
    if "OPTIMAL" in s:
        cls = "b-green"
    elif "FEASIBLE" in s and "IN" not in s:
        cls = "b-yellow"
    elif "INFEASIBLE" in s or "CONFLICT" in s:
        cls = "b-red"
    else:
        cls = "b-orange"
    return f'<span class="badge {cls}">{h(status)}</span>'


def _origin_chip(origin: str, source: str = "") -> str:
    label = h(source or origin)
    if origin == "pve":
        return f'<span class="chip chip-pve">PVE HA: {label}</span>'
    if origin == "tag":
        return f'<span class="chip chip-tag">tag: {label}</span>'
    if origin == "pool":
        return f'<span class="chip chip-pool">pool: {label}</span>'
    return f'<span class="chip chip-plb">plb: {label}</span>'


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ts


def _card(label: str, value: str, color: str = "") -> str:
    style = f' style="color:{color}"' if color else ""
    return (
        f'<div class="card">'
        f'<div class="label">{h(label)}</div>'
        f'<div class="value mono"{style}>{value}</div>'
        f"</div>"
    )


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _parse_run(path: Path) -> dict[str, Any]:
    events: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    run: dict[str, Any] = {
        "filename": path.name,
        "events": events,
        "solver_run": None,
        "proxlb_actions": [],
        "proxlb_executed": None,
        "constraints": [],
        "plan_steps": [],
        "pve_deferred": [],
        "unbreakable_cycle": [],
        "compare": [],
        "infeasible": None,
        "error": None,
        "ts": None,
        # Active-mode execution events
        "active_step_results": [],
        "active_step_retries": [],
        "active_resolves": [],
        "active_complete": None,
        # Cluster state snapshot (for before/after load display)
        "cluster_state": None,
    }
    for ev in events:
        t = ev.get("event")
        if t == "solver_run":
            run["solver_run"] = ev
            run["ts"] = ev.get("ts")
        elif t == "proxlb_action":
            run["proxlb_actions"].append(ev)
        elif t == "proxlb_executed":
            run["proxlb_executed"] = ev
        elif t == "constraint":
            run["constraints"].append(ev)
        elif t == "plan_step":
            run["plan_steps"].append(ev)
        elif t == "pve_deferred":
            run["pve_deferred"] = ev.get("vms", [])
        elif t == "unbreakable_cycle":
            run["unbreakable_cycle"] = ev.get("vms", [])
        elif t == "compare":
            run["compare"].append(ev)
        elif t == "infeasible":
            run["infeasible"] = ev
        elif t == "error":
            run["error"] = ev
        elif t == "active_step_result":
            run["active_step_results"].append(ev)
        elif t == "active_step_retry":
            run["active_step_retries"].append(ev)
        elif t == "active_resolve":
            run["active_resolves"].append(ev)
        elif t == "active_complete":
            run["active_complete"] = ev
        elif t == "cluster_state":
            run["cluster_state"] = ev
        elif t is None:
            # first event may lack a 'ts' if emitted before solver_run
            if run["ts"] is None:
                run["ts"] = ev.get("ts")
    # Fall back to timestamp of first event if solver_run never arrived
    if run["ts"] is None and events:
        run["ts"] = events[0].get("ts")
    # Build a quick lookup: (step_number, vm_name) → plan_step event
    run["plan_step_map"] = {
        (ps.get("step"), ps.get("vm")): ps
        for ps in run["plan_steps"]
    }
    return run


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

def _render_index(runs: list[dict[str, Any]], output_dir: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    total = len(runs)
    n_optimal    = sum(1 for r in runs if r["solver_run"] and
                       "OPTIMAL" in (r["solver_run"].get("status") or "").upper())
    n_feasible   = sum(1 for r in runs if r["solver_run"] and r["solver_run"].get("feasible"))
    n_infeasible = sum(1 for r in runs if r["solver_run"] and not r["solver_run"].get("feasible"))
    n_errors     = sum(1 for r in runs if r["error"])

    gaps = [r["solver_run"]["gap"] for r in runs
            if r["solver_run"] and r["solver_run"].get("gap") is not None]
    avg_gap = f"{sum(gaps)/len(gaps):.4f}" if gaps else "—"
    total_solver_mig = sum(r["solver_run"].get("migrations", 0) for r in runs if r["solver_run"])
    total_proxlb_mig = sum(len(r["proxlb_actions"]) for r in runs)

    cards_html = (
        '<div class="cards">'
        + _card("Total runs",          str(total))
        + _card("Optimal",             str(n_optimal),    "var(--green)")
        + _card("Feasible",            str(n_feasible),   "var(--green)" if n_feasible == total else "")
        + _card("Infeasible",          str(n_infeasible), "var(--red)" if n_infeasible else "")
        + _card("Errors",              str(n_errors),     "var(--orange)" if n_errors else "")
        + _card("Avg gap",             avg_gap)
        + _card("Solver migrations",   str(total_solver_mig))
        + _card("ProxLB migrations",   str(total_proxlb_mig))
        + "</div>"
    )

    rows: list[str] = []
    for r in runs:
        sr = r["solver_run"]
        ts_cell = h(_fmt_ts(r["ts"])) if r["ts"] else "—"
        if sr:
            status_cell  = _status_badge(sr.get("status", "?"))
            feas_cell    = ('<span style="color:var(--green)">✓</span>' if sr.get("feasible")
                            else '<span style="color:var(--red)">✗</span>')
            solver_mig   = str(sr.get("migrations", 0))
            gap_cell     = f'{sr.get("gap", 0):.4f}'
            ms_cell      = f'{sr.get("wall_time_ms", 0):.0f}'
        else:
            status_cell = '<span class="badge b-orange">NO DATA</span>'
            feas_cell = solver_mig = gap_cell = ms_cell = "—"

        proxlb_mig = str(len(r["proxlb_actions"]))
        executed   = r["proxlb_executed"]
        if executed is not None:
            exec_badge = (' <span class="badge b-muted">dry run</span>' if executed.get("dry_run")
                          else ' <span class="badge b-green">executed</span>')
        else:
            exec_badge = ""

        warn = (' <span style="color:var(--orange)" title="error occurred">⚠</span>'
                if r["error"] else "")
        c_count = len(r["constraints"])
        detail  = r["filename"].replace(".jsonl", ".html")
        rows.append(
            "<tr>"
            f'<td class="mono">{ts_cell}</td>'
            f"<td>{status_cell}{warn}</td>"
            f"<td>{feas_cell}</td>"
            f'<td class="mono">{proxlb_mig}{exec_badge}</td>'
            f'<td class="mono">{solver_mig}</td>'
            f'<td class="mono">{gap_cell}</td>'
            f'<td class="mono">{ms_cell}</td>'
            f"<td>{c_count}</td>"
            f'<td><a href="{h(detail)}">view →</a></td>'
            "</tr>"
        )

    tbody = "\n".join(rows) if rows else '<tr><td colspan="9" class="empty">No runs found.</td></tr>'

    table_html = (
        '<div class="section">'
        '<div class="section-title">📋 All runs'
        f'<span class="count">newest first · {total} total</span></div>'
        '<div class="tbl-wrap"><table>'
        "<thead><tr>"
        "<th>Timestamp</th><th>Status</th><th>Feasible</th>"
        "<th>ProxLB</th><th>Solver</th><th>Load gap</th><th>Time&nbsp;(ms)</th>"
        "<th>Constraints</th><th></th>"
        "</tr></thead>"
        f"<tbody>{tbody}</tbody>"
        "</table></div></div>"
    )

    body = (
        '<div class="container">'
        '<div class="page-header">'
        "<h1>🖥 ProxLB Solver — Shadow Run Report</h1>"
        f'<div class="meta">Generated: {h(now)}</div>'
        "</div>"
        f"{cards_html}"
        f"{table_html}"
        "</div>"
    )
    (_output := output_dir / "index.html").write_text(
        _page("ProxLB Solver Report", body), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Run detail page
# ---------------------------------------------------------------------------

def _render_run(run: dict[str, Any], output_dir: Path) -> None:
    sr       = run["solver_run"]
    filename = run["filename"]
    ts_str   = h(_fmt_ts(run["ts"])) if run["ts"] else "—"
    out_name = filename.replace(".jsonl", ".html")

    # ── Header cards ────────────────────────────────────────────────────────
    if sr:
        feas_html = (
            '<span style="color:var(--green);font-size:16px;font-weight:700">✓ Feasible</span>'
            if sr.get("feasible") else
            '<span style="color:var(--red);font-size:16px;font-weight:700">✗ Infeasible</span>'
        )
        mode = (sr.get("mode") or "shadow").lower()
        mode_cls = "b-blue" if mode == "active" else "b-muted"
        mode_card = (
            f'<div class="card"><div class="label">Mode</div>'
            f'<div style="margin-top:8px">'
            f'<span class="badge {mode_cls}">{h(mode.upper())}</span>'
            f'</div></div>'
        )
        header_cards = (
            '<div class="cards">'
            f'<div class="card"><div class="label">Status</div>'
            f'<div style="margin-top:8px">{_status_badge(sr.get("status","?"))}</div></div>'
            f'<div class="card"><div class="label">Feasibility</div>'
            f'<div style="margin-top:6px">{feas_html}</div></div>'
            + mode_card
            + _card("ProxLB mig.",    str(len(run["proxlb_actions"])))
            + _card("Solver mig.",    str(sr.get("migrations", 0)))
            + _card("Mig. cost",      f'{sr.get("migration_cost_gib", 0)}&thinsp;GiB')
            + _card("Load gap",       f'{sr.get("gap", 0):.4f}')
            + _card("Solver time",    f'{sr.get("wall_time_ms", 0):.0f} ms')
            + "</div>"
        )
    else:
        header_cards = '<div class="cards"><div class="card"><div class="label">No solver_run event in this file</div></div></div>'

    # ── Constraints ──────────────────────────────────────────────────────────
    constraints = run["constraints"]
    if constraints:
        aff  = [c for c in constraints if c["type"] == "affinity"]
        aa   = [c for c in constraints if c["type"] == "anti_affinity"]
        pins = [c for c in constraints if c["type"] == "pin"]
        ign  = [c for c in constraints if c["type"] == "ignore"]

        def _group_table(items: list, heading: str, cols: list[str], row_fn) -> str:
            if not items:
                return ""
            hdr  = "".join(f"<th>{c}</th>" for c in cols)
            rows = "".join(row_fn(c) for c in items)
            return (
                f'<div class="sub-heading">{heading}</div>'
                '<div class="tbl-wrap"><table>'
                f"<thead><tr>{hdr}</tr></thead>"
                f"<tbody>{rows}</tbody>"
                "</table></div>"
            )

        def _aff_row(c):
            orig  = _origin_chip(c.get("origin", "plb"))
            vms   = "  ".join(h(v) for v in c.get("vms", []))
            mode  = "hard" if c.get("hard", True) else '<span style="color:var(--muted)">soft</span>'
            return f"<tr><td class='mono'>{h(c.get('name',''))}</td><td>{orig}</td><td>{vms}</td><td>{mode}</td></tr>"

        def _pin_row(c):
            nodes   = ", ".join(h(n) for n in c.get("nodes", []))
            origins = "".join(
                _origin_chip(o.get("origin", "?"), o.get("source", ""))
                for o in c.get("origins", [])
            ) or '<span class="badge b-muted">—</span>'
            return f"<tr><td class='mono'>{h(c.get('vm',''))}</td><td class='mono'>{nodes}</td><td>{origins}</td></tr>"

        ign_list = "  ".join(f"<code>{h(c.get('vm',''))}</code>" for c in ign)

        c_body = (
            _group_table(aff,  "Affinity groups",      ["Rule / Tag / Pool", "Origin", "VMs", "Mode"], _aff_row)
            + _group_table(aa, "Anti-affinity groups", ["Rule / Tag / Pool", "Origin", "VMs", "Mode"], _aff_row)
            + _group_table(pins, "Pin constraints",    ["VM", "Allowed nodes", "Sources"], _pin_row)
            + (f'<div style="padding:10px 16px;font-size:13px">Ignored: {ign_list}</div>' if ign else "")
        )
        c_section = (
            '<div class="section">'
            f'<div class="section-title">🔒 Constraints'
            f'<span class="count">{len(constraints)} recognized</span></div>'
            f'<div>{c_body}</div>'
            "</div>"
        )
    else:
        c_section = ""

    # Determine mode early — needed for ProxLB plan badge and compare suppression
    is_active = (sr or {}).get("mode") == "active"

    # ── ProxLB migration plan ─────────────────────────────────────────────────
    proxlb_actions  = run["proxlb_actions"]
    proxlb_executed = run["proxlb_executed"]

    if proxlb_executed is not None:
        if proxlb_executed.get("dry_run"):
            exec_badge = ' <span class="badge b-muted">dry run — not executed</span>'
        elif is_active:
            exec_badge = ' <span class="badge b-blue">solver overrides</span>'
        else:
            exec_badge = ' <span class="badge b-green">executed</span>'
    else:
        exec_badge = ""

    if proxlb_actions:
        plb_rows: list[str] = []
        for a in proxlb_actions:
            plb_rows.append(
                "<tr>"
                f'<td class="mono">{h(a.get("vm",""))}</td>'
                f'<td><span class="badge b-muted">{h(a.get("type","vm"))}</span></td>'
                f'<td class="mono">{h(a.get("source",""))} → {h(a.get("target",""))}</td>'
                "</tr>"
            )
        proxlb_plan_section = (
            '<div class="section">'
            f'<div class="section-title">📦 ProxLB migration plan{exec_badge}'
            f'<span class="count">{len(proxlb_actions)} migrations</span></div>'
            '<div class="tbl-wrap"><table>'
            "<thead><tr><th>VM</th><th>Type</th><th>Move</th></tr></thead>"
            f"<tbody>{''.join(plb_rows)}</tbody>"
            "</table></div></div>"
        )
    elif proxlb_executed is not None:
        proxlb_plan_section = (
            '<div class="section">'
            f'<div class="section-title">📦 ProxLB migration plan{exec_badge}</div>'
            '<div class="empty">No migrations planned by ProxLB this run.</div>'
            "</div>"
        )
    else:
        proxlb_plan_section = ""

    # ── Solver migration plan ─────────────────────────────────────────────────
    plan_steps = run["plan_steps"]
    _cs_guests = (run.get("cluster_state") or {}).get("guests", {})
    _GiB_plan  = 1024 ** 3
    if plan_steps:
        by_step: dict[int, list] = {}
        for ps in plan_steps:
            by_step.setdefault(ps.get("step", 0), []).append(ps)

        show_cost_col = bool(_cs_guests)
        rows: list[str] = []
        for step_num in sorted(by_step):
            migs = by_step[step_num]
            par  = migs[0].get("parallel", False)
            par_badge = ' <span class="badge b-muted">parallel</span>' if par else ""
            for i, m in enumerate(migs):
                step_cell = f"Step&nbsp;{step_num}{par_badge}" if i == 0 else ""
                vm_name = m.get("vm", "")
                cost_cell = ""
                if show_cost_col:
                    gd_vm  = _cs_guests.get(vm_name, {})
                    ram_b  = int(gd_vm.get("memory", 0))
                    ram_gib = max(1, ram_b // _GiB_plan)
                    cost_cell = (
                        f'<td class="mono" style="color:var(--muted);font-size:12px">'
                        f'{ram_gib}&thinsp;GiB</td>'
                    )
                rows.append(
                    "<tr>"
                    f"<td>{step_cell}</td>"
                    f'<td class="mono">{h(vm_name)}</td>'
                    f'<td class="mono">{h(m.get("source",""))} → {h(m.get("target",""))}</td>'
                    f"{cost_cell}"
                    "</tr>"
                )

        if show_cost_col:
            plan_thead = (
                "<thead><tr>"
                "<th>Step</th><th>VM</th><th>Move</th>"
                '<th title="Configured RAM used as solver migration weight (min 1 GiB). Add 4× local disk GiB for VMs with local storage.">RAM weight</th>'
                "</tr></thead>"
            )
            plan_cost_note = (
                '<div style="padding:6px 16px 10px;font-size:11px;color:var(--muted)">'
                'RAM weight: configured allocation (min 1&thinsp;GiB). '
                'VMs with local disk add 4×&thinsp;disk&thinsp;GiB to migration cost.'
                '</div>'
            )
        else:
            plan_thead    = "<thead><tr><th>Step</th><th>VM</th><th>Move</th></tr></thead>"
            plan_cost_note = ""

        notes = ""
        if run["pve_deferred"]:
            vms = ", ".join(h(v) for v in run["pve_deferred"])
            notes += f'<div style="padding:9px 16px;font-size:12px;color:var(--muted)">PVE-deferred (HA follow-up): {vms}</div>'
        if run["unbreakable_cycle"]:
            vms = ", ".join(h(v) for v in run["unbreakable_cycle"])
            notes += f'<div style="padding:9px 16px;font-size:12px;color:var(--red)">⚠ Unbreakable cycle: {vms}</div>'

        plan_section = (
            '<div class="section">'
            f'<div class="section-title">🔧 Solver migration plan'
            f'<span class="count">{len(plan_steps)} migrations</span></div>'
            '<div class="tbl-wrap"><table>'
            f"{plan_thead}"
            f"<tbody>{''.join(rows)}</tbody>"
            f"</table></div>{plan_cost_note}{notes}</div>"
        )
    elif run["infeasible"]:
        blockers = ", ".join(f"<code>{h(v)}</code>" for v in run["infeasible"].get("blocking_vms", []))
        plan_section = (
            '<div class="section">'
            '<div class="section-title" style="color:var(--red)">🚫 Infeasible — no plan</div>'
            f'<div style="padding:14px 16px;color:var(--red)">Blocking VMs: {blockers or "—"}</div>'
            "</div>"
        )
    else:
        plan_section = ""

    # ── Node load: before / after ─────────────────────────────────────────────
    cs = run.get("cluster_state")
    if cs:
        cs_method     = cs.get("method", "memory")
        nodes_data    = cs.get("nodes", {})
        guests_data   = cs.get("guests", {})
        plan_steps_ba = run["plan_steps"]

        # CPU allocation per node before migrations (sum of VM cores)
        cpu_alloc_before: dict = {n: 0 for n in nodes_data}
        for gd in guests_data.values():
            nd_name = gd.get("node")
            if nd_name in cpu_alloc_before:
                cpu_alloc_before[nd_name] += int(gd.get("cpu", 0))

        # Apply solver plan to estimate "after" state
        mem_after: dict = {n: nd.get("memory_used", 0) for n, nd in nodes_data.items()}
        cpu_after: dict = dict(cpu_alloc_before)
        for ps in plan_steps_ba:
            vm_name = ps.get("vm")
            src     = ps.get("source")
            tgt     = ps.get("target")
            gd      = guests_data.get(vm_name, {})
            vm_mem  = int(gd.get("memory", 0))
            vm_cpu  = int(gd.get("cpu", 0))
            if src in mem_after:
                mem_after[src] = max(0, mem_after[src] - vm_mem)
                cpu_after[src] = max(0, cpu_after[src] - vm_cpu)
            if tgt:
                mem_after[tgt] = mem_after.get(tgt, 0) + vm_mem
                cpu_after[tgt] = cpu_after.get(tgt, 0) + vm_cpu

        GiB = 1024 ** 3

        def _bar(pct: float, cls: str) -> str:
            w = max(0.0, min(100.0, pct))
            return (
                f'<div class="bar-track">'
                f'<div class="bar-fill {cls}" style="width:{w:.1f}%"></div>'
                f'</div>'
            )

        def _delta_html(d: float) -> str:
            if d < -0.5:
                return f'<span style="color:var(--green)">&#9660;&thinsp;{abs(d):.1f}%</span>'
            if d > 0.5:
                return f'<span style="color:var(--orange)">&#9650;&thinsp;{d:.1f}%</span>'
            return '<span style="color:var(--muted)">—</span>'

        show_after = bool(plan_steps_ba)

        # Build VM-to-node distribution before and after
        vms_before: dict = {n: [] for n in nodes_data}
        for vm_name, gd in sorted(guests_data.items()):
            nd_name = gd.get("node")
            if nd_name in vms_before:
                vms_before[nd_name].append((vm_name, gd.get("memory", 0), gd.get("cpu", 0)))

        vms_after: dict = {n: list(vl) for n, vl in vms_before.items()}
        moved: set = set()
        for ps in plan_steps_ba:
            vm_name = ps.get("vm")
            src     = ps.get("source")
            tgt     = ps.get("target")
            if vm_name in moved:
                continue
            moved.add(vm_name)
            gd      = guests_data.get(vm_name, {})
            entry   = (vm_name, gd.get("memory", 0), gd.get("cpu", 0))
            if src in vms_after:
                vms_after[src] = [v for v in vms_after[src] if v[0] != vm_name]
            if tgt:
                vms_after.setdefault(tgt, []).append(entry)

        # Collect per-node pcts for gap calculation
        mp_b_all: list[float] = []
        mp_a_all: list[float] = []

        load_rows: list[str] = []
        for node_name in sorted(nodes_data):
            nd      = nodes_data[node_name]
            mem_tot = nd.get("memory_total", 1) or 1
            cpu_tot = nd.get("cpu_total", 1) or 1

            mem_b = nd.get("memory_used", 0)
            mem_a = mem_after.get(node_name, mem_b)
            cpu_b = cpu_alloc_before.get(node_name, 0)
            cpu_a = cpu_after.get(node_name, cpu_b)

            mp_b = 100 * mem_b / mem_tot
            mp_a = 100 * mem_a / mem_tot
            cp_b = 100 * cpu_b / cpu_tot
            cp_a = 100 * cpu_a / cpu_tot
            md   = mp_a - mp_b
            cd   = cp_a - cp_b

            mp_b_all.append(mp_b)
            mp_a_all.append(mp_a)

            mc_a = "bar-fill-better" if md < -0.5 else ("bar-fill-worse" if md > 0.5 else "bar-fill-neutral")
            cc_a = "bar-fill-better" if cd < -0.5 else ("bar-fill-worse" if cd > 0.5 else "bar-fill-neutral")

            mem_b_label = f'{mem_b/GiB:.1f}&thinsp;GiB&ensp;{mp_b:.1f}%'
            mem_a_label = f'{mem_a/GiB:.1f}&thinsp;GiB&ensp;{mp_a:.1f}%'
            cpu_b_label = f'{cpu_b}&thinsp;/&thinsp;{cpu_tot}&ensp;{cp_b:.0f}%'
            cpu_a_label = f'{cpu_a}&thinsp;/&thinsp;{cpu_tot}&ensp;{cp_a:.0f}%'

            if show_after:
                load_rows.append(
                    "<tr>"
                    f'<td class="mono">{h(node_name)}</td>'
                    f'<td><div class="load-cell">{_bar(mp_b, "bar-fill-neutral")}'
                    f'<span class="mono">{mem_b_label}</span></div></td>'
                    f'<td><div class="load-cell">{_bar(mp_a, mc_a)}'
                    f'<span class="mono">{mem_a_label}</span></div></td>'
                    f'<td>{_delta_html(md)}</td>'
                    f'<td><div class="load-cell">{_bar(cp_b, "bar-fill-neutral")}'
                    f'<span class="mono">{cpu_b_label}</span></div></td>'
                    f'<td><div class="load-cell">{_bar(cp_a, cc_a)}'
                    f'<span class="mono">{cpu_a_label}</span></div></td>'
                    f'<td>{_delta_html(cd)}</td>'
                    "</tr>"
                )
            else:
                load_rows.append(
                    "<tr>"
                    f'<td class="mono">{h(node_name)}</td>'
                    f'<td><div class="load-cell">{_bar(mp_b, "bar-fill-neutral")}'
                    f'<span class="mono">{mem_b_label}</span></div></td>'
                    f'<td><div class="load-cell">{_bar(cp_b, "bar-fill-neutral")}'
                    f'<span class="mono">{cpu_b_label}</span></div></td>'
                    "</tr>"
                )

        # Gap badge for section title
        gap_b = max(mp_b_all) - min(mp_b_all) if mp_b_all else 0
        gap_a = max(mp_a_all) - min(mp_a_all) if mp_a_all else 0
        if show_after:
            gap_cls = "b-green" if gap_a < gap_b - 0.5 else ("b-orange" if gap_a > gap_b + 0.5 else "b-muted")
            gap_badge = (
                f' <span class="badge b-muted">gap before: {gap_b:.1f}%</span>'
                f' <span class="badge {gap_cls}">gap after: {gap_a:.1f}%</span>'
            )
        else:
            gap_badge = f' <span class="badge b-muted">gap: {gap_b:.1f}%</span>'

        if show_after:
            load_thead = (
                "<thead><tr>"
                "<th>Node</th>"
                "<th>RAM before</th><th>RAM after</th><th>Δ</th>"
                "<th>CPU alloc before</th><th>CPU alloc after</th><th>Δ</th>"
                "</tr></thead>"
            )
            after_note = (
                ' <span class="badge b-muted">projected</span>'
                if not is_active else ""
            )
        else:
            load_thead = (
                "<thead><tr>"
                "<th>Node</th><th>RAM</th><th>CPU alloc</th>"
                "</tr></thead>"
            )
            after_note = ""

        # VM distribution sub-table
        def _vm_cell(vm_list: list) -> str:
            if not vm_list:
                return '<span style="color:var(--muted);font-style:italic">empty</span>'
            parts = []
            for vm_name, mem, cpu in sorted(vm_list):
                gd_cs    = guests_data.get(vm_name, {})
                mem_used = gd_cs.get("memory_used")
                if mem_used is not None:
                    # Show actual RSS first, then configured (which the solver uses for weighting)
                    rss_str = f'{mem_used/GiB:.2f}&thinsp;GiB RSS'
                    cfg_str = (
                        f'<span title="Configured allocation — used by solver for load balancing and migration cost"'
                        f' style="color:var(--accent)">{mem/GiB:.1f}&thinsp;GiB cfg</span>'
                    )
                    mem_str = f'{rss_str} &middot; {cfg_str}'
                else:
                    cfg_str = (
                        f'<span title="Configured allocation — used by solver for load balancing and migration cost"'
                        f' style="color:var(--accent)">{mem/GiB:.1f}&thinsp;GiB cfg</span>'
                        if mem else ""
                    )
                    mem_str = cfg_str
                cpu_str = f'{cpu}&thinsp;vCPU' if cpu else ""
                detail  = " &middot; ".join(filter(None, [mem_str, cpu_str]))
                parts.append(
                    f'<code>{h(vm_name)}</code>'
                    + (f'&ensp;<span style="font-size:11px">{detail}</span>' if detail else "")
                )
            return "<br>".join(parts)

        vm_dist_rows: list[str] = []
        for node_name in sorted(nodes_data):
            before_cell = _vm_cell(vms_before.get(node_name, []))
            if show_after:
                after_cell = _vm_cell(vms_after.get(node_name, []))
                vm_dist_rows.append(
                    "<tr>"
                    f'<td class="mono">{h(node_name)}</td>'
                    f"<td>{before_cell}</td>"
                    f'<td style="color:var(--muted);text-align:center">→</td>'
                    f"<td>{after_cell}</td>"
                    "</tr>"
                )
            else:
                vm_dist_rows.append(
                    "<tr>"
                    f'<td class="mono">{h(node_name)}</td>'
                    f"<td>{before_cell}</td>"
                    "</tr>"
                )

        if show_after:
            vm_dist_thead = (
                "<thead><tr>"
                "<th>Node</th><th>VMs before</th><th></th><th>VMs after</th>"
                "</tr></thead>"
            )
        else:
            vm_dist_thead = "<thead><tr><th>Node</th><th>VMs</th></tr></thead>"

        ram_note = (
            '<div style="padding:6px 16px 10px;font-size:11px;color:var(--muted)">'
            'Bars show actual node RSS (Proxmox API). '
            '<span style="color:var(--accent)">Blue cfg values</span> = '
            'configured allocation — the basis the solver uses for load balancing and migration cost. '
            + ('After-state projects by adding/removing configured VM sizes.'
               if show_after else '')
            + '</div>'
        )

        load_section = (
            '<div class="section">'
            f'<div class="section-title">📊 Node load{after_note}{gap_badge}'
            f'<span class="count">method: {h(cs_method)}</span></div>'
            '<div class="tbl-wrap"><table>'
            f"{load_thead}"
            f"<tbody>{''.join(load_rows)}</tbody>"
            f"</table></div>"
            f"{ram_note}"
            '<div class="sub-heading">VM distribution</div>'
            '<div class="tbl-wrap"><table>'
            f"{vm_dist_thead}"
            f"<tbody>{''.join(vm_dist_rows)}</tbody>"
            "</table></div>"
            "</div>"
        )
    else:
        load_section = ""

    # ── Comparison ────────────────────────────────────────────────────────────
    _CMP = {
        "agree":       ("✓", "cmp-agree",  "Both agree"),
        "differ":      ("≠", "cmp-differ", "Different targets"),
        "solver_only": ("S", "cmp-solver", "Solver only"),
        "proxlb_only": ("P", "cmp-proxlb", "ProxLB only"),
    }
    compare = run["compare"]
    if compare:
        counts: dict[str, int] = {}
        rows = []
        for c in compare:
            res = c.get("result", "?")
            counts[res] = counts.get(res, 0) + 1
            icon, cls, label = _CMP.get(res, ("?", "", res))
            vm = h(c.get("vm", ""))
            if res == "agree":
                detail = f'target: <code>{h(c.get("target",""))}</code>'
            elif res == "differ":
                detail = (f'solver: <code>{h(c.get("solver_target",""))}</code>'
                          f'&ensp;proxlb: <code>{h(c.get("proxlb_target",""))}</code>')
            elif res == "solver_only":
                detail = f'solver → <code>{h(c.get("solver_target",""))}</code>'
            else:
                detail = f'proxlb → <code>{h(c.get("proxlb_target",""))}</code>'
            rows.append(
                "<tr>"
                f'<td class="mono">{vm}</td>'
                f'<td><span class="{cls}">{icon} {h(label)}</span></td>'
                f"<td>{detail}</td>"
                "</tr>"
            )

        summary = "  ".join(
            f'<span class="badge b-muted">{h(k)}: {v}</span>'
            for k, v in sorted(counts.items())
        )
        cmp_section = (
            '<div class="section">'
            f'<div class="section-title">⚖ ProxLB comparison'
            f'<span class="count">{summary}</span></div>'
            '<div class="tbl-wrap"><table>'
            "<thead><tr><th>VM</th><th>Result</th><th>Detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table></div></div>"
        )
    else:
        cmp_section = ""

    # ── Active Execution ───────────────────────────────────────────────────────
    active_step_results      = run["active_step_results"]
    active_step_retries_list = run["active_step_retries"]
    active_resolves_list     = run["active_resolves"]
    active_complete          = run.get("active_complete")
    plan_step_map            = run.get("plan_step_map", {})

    if active_step_results:
        # Group results: retry → step → [events]
        by_retry_step: dict[int, dict[int, list]] = {}
        for ev in active_step_results:
            r = ev.get("step_retry", 0)
            s = ev.get("step", 0)
            by_retry_step.setdefault(r, {}).setdefault(s, []).append(ev)

        retry_map   = {ev.get("step_retry"): ev for ev in active_step_retries_list}
        resolve_map = {ev.get("step_retry"): ev for ev in active_resolves_list}

        n_success = sum(1 for ev in active_step_results if ev.get("success"))
        n_failed  = len(active_step_results) - n_success

        def _duration(ev: dict) -> str:
            """Rough wall-clock duration: plan_step.ts → result.ts."""
            try:
                ps = plan_step_map.get((ev.get("step"), ev.get("vm")), {})
                t0 = datetime.fromisoformat(ps["ts"])
                t1 = datetime.fromisoformat(ev["ts"])
                secs = int((t1 - t0).total_seconds())
                return f"{secs}s"
            except Exception:
                return "—"

        active_parts: list[str] = []
        for retry_num in sorted(by_retry_step):
            # ── Re-solve heading (only for re-solve passes) ───────────────────
            if retry_num > 0:
                retry       = retry_map.get(retry_num, {})
                pinned      = retry.get("pinned_vms", [])
                resolve     = resolve_map.get(retry_num, {})
                failed_step = retry.get("step", "?")
                pinned_str  = ", ".join(
                    f'<code>{h(v)}</code>' for v in sorted(pinned)
                ) if pinned else "none"
                resolve_badge_html = (
                    " " + _status_badge(resolve.get("status", "?"))
                    if resolve else ""
                )
                active_parts.append(
                    f'<div class="sub-heading" style="color:var(--orange);padding-top:14px">'
                    f'⟳ Re-solve {retry_num} — step {h(str(failed_step))} failed, '
                    f'{len(pinned)} VM(s) pinned: {pinned_str}'
                    f'{resolve_badge_html}</div>'
                )

            # ── Flat sequential table for this retry pass ─────────────────────
            # Flatten all VMs across solver steps in order; execution is always
            # sequential (Balancing handles one VM at a time internally).
            steps_in_pass = sorted(by_retry_step[retry_num].keys())
            pass_evs: list[dict] = []
            for step_num in steps_in_pass:
                pass_evs.extend(by_retry_step[retry_num][step_num])

            # Only show "Solver step" column when there are multiple distinct
            # steps — i.e. dependency ordering actually mattered this pass.
            show_step_col = len(steps_in_pass) > 1

            rows: list[str] = []
            for seq, ev in enumerate(pass_evs, start=1):
                step_num = ev.get("step", 0)
                vm       = ev.get("vm", "")
                ps       = plan_step_map.get((step_num, vm), {})
                source   = ps.get("source", "")
                target   = ps.get("target", ev.get("expected", ""))
                actual   = ev.get("actual") or "—"
                success  = ev.get("success", False)
                dur      = _duration(ev)

                move_cell = (
                    f'<span class="mono">{h(source)}</span>'
                    f' <span style="color:var(--muted)">→</span> '
                    f'<span class="mono">{h(target)}</span>'
                    if source else
                    f'<span class="mono">{h(target)}</span>'
                )
                if success:
                    result_cell = '<span style="color:var(--green);font-size:16px">✓</span>'
                    actual_cell = f'<span class="mono" style="color:var(--green)">{h(actual)}</span>'
                else:
                    err = ev.get("error", "")
                    result_cell = '<span style="color:var(--red);font-size:16px">✗</span>'
                    actual_cell = (
                        f'<span class="mono" style="color:var(--red)">{h(actual)}</span>'
                        + (f'<br><span style="color:var(--red);font-size:11px">{h(err)}</span>' if err else "")
                    )
                step_td = (
                    f'<td class="mono" style="color:var(--muted)">{step_num}</td>'
                    if show_step_col else ""
                )
                rows.append(
                    "<tr>"
                    f'<td class="mono" style="color:var(--muted);text-align:right">{seq}</td>'
                    f'<td class="mono">{h(vm)}</td>'
                    f"<td>{move_cell}</td>"
                    f"{step_td}"
                    f"<td>{actual_cell}</td>"
                    f'<td style="text-align:center">{result_cell}</td>'
                    f'<td class="mono" style="color:var(--muted);text-align:right">{h(dur)}</td>'
                    "</tr>"
                )

            step_th = "<th>Solver step</th>" if show_step_col else ""
            active_parts.append(
                '<div class="tbl-wrap"><table>'
                "<thead><tr>"
                "<th>#</th><th>VM</th><th>Migration</th>"
                f"{step_th}"
                "<th>Landed on</th>"
                "<th style='text-align:center'>Result</th>"
                "<th style='text-align:right'>Duration</th>"
                "</tr></thead>"
                f"<tbody>{''.join(rows)}</tbody>"
                "</table></div>"
            )

        # ── Summary footer ────────────────────────────────────────────────────
        if active_complete:
            total_retries = active_complete.get("step_retries", 0)
            pinned_vms    = active_complete.get("pinned_vms", [])
            if pinned_vms:
                pv_str   = ", ".join(f"<code>{h(v)}</code>" for v in pinned_vms)
                footer_c = f'color:var(--orange)'
                footer_t = f'{total_retries} re-solve(s) · {len(pinned_vms)} VM(s) skipped: {pv_str}'
            else:
                footer_c = 'color:var(--green)'
                footer_t = (
                    f'All {n_success} migration(s) succeeded'
                    + (f' · {total_retries} re-solve(s)' if total_retries else '')
                )
            active_parts.append(
                f'<div style="padding:10px 16px;font-size:12px;border-top:1px solid var(--border);{footer_c}">'
                f'{footer_t}</div>'
            )

        summary_badges = ""
        if n_failed:
            summary_badges += f' <span class="badge b-red">{n_failed} failed</span>'
        if n_success:
            summary_badges += f' <span class="badge b-green">{n_success} migrated</span>'

        active_exec_section = (
            '<div class="section">'
            f'<div class="section-title">⚡ Active Execution{summary_badges}'
            f'<span class="count">{len(by_retry_step)} solve pass(es)</span></div>'
            f'{"".join(active_parts)}'
            "</div>"
        )
    else:
        active_exec_section = ""

    # In active mode, suppress the comparison section when ProxLB had no
    # independent plan (all VMs show as solver_only, which is expected and
    # not useful to display).
    if is_active and all(c.get("result") == "solver_only" for c in run["compare"]):
        cmp_section = ""

    # ── Error ─────────────────────────────────────────────────────────────────
    if run["error"]:
        err = run["error"]
        err_section = (
            '<div class="section">'
            '<div class="section-title" style="color:var(--red)">⚠ Error</div>'
            '<div style="padding:16px">'
            f'<div style="color:var(--red);margin-bottom:10px">{h(err.get("message",""))}</div>'
            f'<div class="errbox">{h(err.get("traceback",""))}</div>'
            "</div></div>"
        )
    else:
        err_section = ""

    # ── Assemble ──────────────────────────────────────────────────────────────
    breadcrumb = '<div class="breadcrumb"><a href="index.html">← Index</a></div>'
    body = (
        '<div class="container">'
        '<div class="page-header">'
        f"{breadcrumb}"
        f'<h1>📄 <span class="mono" style="font-size:15px">{h(filename)}</span></h1>'
        f'<div class="meta">Run timestamp: {ts_str}</div>'
        "</div>"
        f"{header_cards}"
        f"{c_section}"
        f"{proxlb_plan_section}"
        f"{plan_section}"
        f"{load_section}"
        f"{active_exec_section}"
        f"{cmp_section}"
        f"{err_section}"
        "</div>"
    )
    (output_dir / out_name).write_text(
        _page(f"{filename} — ProxLB Solver", body), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(log_dir: Path, output_dir: Path) -> int:
    """Read all ``solver_run_*.jsonl`` files from *log_dir* and write an HTML
    report to *output_dir*.

    Returns the number of run files processed.
    """
    log_dir    = Path(log_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_files = sorted(log_dir.glob("solver_run_*.jsonl"), reverse=True)
    runs = [_parse_run(f) for f in run_files]

    for run in runs:
        _render_run(run, output_dir)
    _render_index(runs, output_dir)

    return len(runs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an HTML report from ProxLB shadow-mode JSONL logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  proxlb-solver-report \\\n"
            "      --log-dir /var/log/proxlb/solver \\\n"
            "      --output-dir /var/www/solver-report\n"
        ),
    )
    parser.add_argument(
        "--log-dir", type=Path, required=True,
        metavar="DIR",
        help="Directory containing solver_run_*.jsonl files (shadow mode log_dir).",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        metavar="DIR",
        help="Directory to write the HTML report into (created if absent).",
    )
    args = parser.parse_args()

    if not args.log_dir.is_dir():
        parser.error(f"--log-dir does not exist or is not a directory: {args.log_dir}")

    n     = generate_report(args.log_dir, args.output_dir)
    index = args.output_dir / "index.html"
    print(f"Report generated: {n} run(s)  →  {index}")


if __name__ == "__main__":
    main()
