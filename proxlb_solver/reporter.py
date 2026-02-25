"""Result reporting and gap calculations for ProxLB Solver."""

from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
from .models import Cluster, Migration, Solution, VM, Node, MigrationPlan

# ── Gap Calculation Helpers (must match solver logic) ──

def _compute_single_gap(cluster: Cluster, placements: Dict[str, str], metric: str) -> float:
    loads = []
    vm_map = {v.name: v for v in cluster.vms}
    for node in cluster.nodes:
        if node.maintenance: continue
        if metric == "cpu":
            # Priority weights the impact of load on the gap
            used = sum(vm_map[v.name].cpu_usage * vm_map[v.name].priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = node.cpu_total
        elif metric == "cpu_psi":
            used = sum(vm_map[v.name].cpu_pressure * vm_map[v.name].priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = 100.0
        elif metric == "memory":
            used = sum(vm_map[v.name].memory * vm_map[v.name].priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = node.memory_total
        elif metric == "memory_psi":
            used = sum(vm_map[v.name].memory_pressure * vm_map[v.name].priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = 100.0
        elif metric == "io_usage":
            used = sum(sum(vm_map[v.name].disks.values()) * vm_map[v.name].priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = sum(node.storage_free.values()) or 1
        elif metric == "io_psi":
            used = sum(vm_map[v.name].io_pressure * vm_map[v.name].priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = 100.0
        else: return 0.0
        loads.append(used / cap if cap else 0)
    return max(loads) - min(loads) if loads else 0.0

def _compute_load_gap(cluster: Cluster, placements: Dict[str, str]) -> float:
    method = cluster.balancing.method
    bal = cluster.balancing
    vm_map = {v.name: v for v in cluster.vms}
    
    if method == "global_smart":
        m_gap = (_compute_single_gap(cluster, placements, "memory") * bal.w_mem_usage +
                 _compute_single_gap(cluster, placements, "memory_psi") * bal.w_mem_psi) / (bal.w_mem_usage + bal.w_mem_psi)
        c_gap = (_compute_single_gap(cluster, placements, "cpu") * bal.w_cpu_usage + 
                 _compute_single_gap(cluster, placements, "cpu_psi") * bal.w_cpu_psi) / (bal.w_cpu_usage + bal.w_cpu_psi)
        i_gap = (_compute_single_gap(cluster, placements, "io_usage") * bal.w_io_usage + 
                 _compute_single_gap(cluster, placements, "io_psi") * bal.w_io_psi) / (bal.w_io_usage + bal.w_io_psi)
        total_w = bal.w_global_mem + bal.w_global_cpu + bal.w_global_io
        return (bal.w_global_mem * m_gap + bal.w_global_cpu * c_gap + bal.w_global_io * i_gap) / total_w if total_w else m_gap

    if method == "cpu_smart":
        return (_compute_single_gap(cluster, placements, "cpu") * bal.w_cpu_usage + 
                _compute_single_gap(cluster, placements, "cpu_psi") * bal.w_cpu_psi) / (bal.w_cpu_usage + bal.w_cpu_psi)
    if method == "memory_smart":
        return (_compute_single_gap(cluster, placements, "memory") * bal.w_mem_usage + 
                _compute_single_gap(cluster, placements, "memory_psi") * bal.w_mem_psi) / (bal.w_mem_usage + bal.w_mem_psi)
    if method == "io_smart":
        return (_compute_single_gap(cluster, placements, "io_usage") * bal.w_io_usage + 
                _compute_single_gap(cluster, placements, "io_psi") * bal.w_io_psi) / (bal.w_io_usage + bal.w_io_psi)

    # Base methods
    loads = []
    for node in cluster.nodes:
        if node.maintenance: continue
        if method == "cpu":
            used = sum(v.cpu_usage * v.priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = node.cpu_total
        elif method == "cpu_psi":
            used = sum(v.cpu_pressure * v.priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = 100.0
        elif method == "memory_psi":
            used = sum(v.memory_pressure * v.priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = 100.0
        elif method == "io_psi":
            used = sum(v.io_pressure * v.priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = 100.0
        else: # memory
            used = sum(v.memory * v.priority for v in cluster.vms if placements.get(v.name) == node.name)
            cap = node.memory_total
        loads.append(used / cap if cap else 0)
    return max(loads) - min(loads) if loads else 0.0

def _initial_load_gap(cluster: Cluster) -> float:
    placements = {vm.name: vm.node for vm in cluster.vms}
    return _compute_load_gap(cluster, placements)

# ── Rule-Origin Helpers ──

def _vm_rule_origins(vm_name: str, cluster) -> str:
    """Return a comma-separated list of affinity/anti-affinity rule names for a VM.

    Format: 'rule-name (origin)', e.g. 'pve-core-group (pve), plb-tag (plb)'.
    Returns 'none' if the VM belongs to no rules.
    """
    entries: list[str] = []
    for rule in cluster.constraints.affinity:
        if vm_name in rule["vms"]:
            name = rule.get("name", "?")
            origin = rule.get("origin", "plb")
            entries.append(f"{name} ({origin})")
    for rule in cluster.constraints.anti_affinity:
        if vm_name in rule["vms"]:
            name = rule.get("name", "?")
            origin = rule.get("origin", "plb")
            entries.append(f"{name} ({origin})")
    return ", ".join(sorted(entries)) if entries else "none"



# ── Reporting ──

def print_report(cluster: Cluster, solution: Solution):
    """Print a terminal report of the solution."""
    from rich.console import Console
    from rich.table import Table
    console = Console()
    
    console.print(f"\n[bold]{cluster.name}[/bold]")
    console.print(f"  {cluster.description}\n")
    
    if not solution.feasible:
        console.print(f"[red]INFEASIBLE[/red] — {solution.stats.status}")
        if solution.blocking_vms:
            console.print(f"  Blockers: {', '.join(solution.blocking_vms)}")
        return

    if not solution.path_feasible:
        console.print("[yellow bold]⚠ UNREACHABLE[/yellow bold] — solver found an optimal state, "
                      "but no executable migration path exists.")
        console.print("  The target placement is blocked by circular dependencies "
                      "that cannot be resolved with temp-moves.")
        console.print("")

    # Utilization Table
    table = Table(title="Node Utilization (Before → After)")
    table.add_column("Node")
    table.add_column("Before Load")
    table.add_column("")
    table.add_column("After Load")
    
    method = cluster.balancing.method
    vm_map = {v.name: v for v in cluster.vms}
    initial_placements = {v.name: v.node for v in cluster.vms}
    
    for node in cluster.nodes:
        if node.maintenance: continue
        
        def get_load_str(placements):
            # Show the primary metric for the chosen method
            if method == "cpu": 
                u = sum(vm_map[v].cpu_usage for v, n in placements.items() if n == node.name)
                return f"{u:.1f}/{node.cpu_total} cores"
            if method == "memory":
                m = sum(vm_map[v].memory for v, n in placements.items() if n == node.name)
                return f"{m / (1024**3):.1f}/{node.memory_total / (1024**3):.1f} GB"
            # Fallback to percentage for smart/psi
            pct = _get_node_load_pct(cluster, node, placements)
            return f"{pct*100:.1f}%"

        table.add_row(
            node.name,
            get_load_str(initial_placements),
            "→",
            get_load_str(solution.placements)
        )
    console.print(table)

    # Migrations Table
    if solution.migrations:
        m_table = Table(title="Proposed Migrations")
        m_table.add_column("VM")
        m_table.add_column("From")
        m_table.add_column("To")
        m_table.add_column("Priority")
        m_table.add_column("Rule Origin")
        for m in solution.migrations:
            m_table.add_row(m.vm, m.source, m.target, str(vm_map[m.vm].priority), _vm_rule_origins(m.vm, cluster))
        console.print(m_table)
    else:
        console.print("[green]Cluster is already balanced.[/green]")

    console.print(f"\n  Status: [bold green]{solution.stats.status}[/bold green]")
    console.print(f"  Weighted Gap: {solution.stats.load_gap:.3f}")
    console.print(f"  Migrations: {solution.stats.migration_count}")
    console.print(f"  Solve time: {solution.stats.wall_time_ms:.1f} ms")
    if solution.reachability_attempts > 1:
        console.print(f"  [yellow]Reachability: {solution.reachability_attempts} attempts "
                      f"({'solved' if solution.path_feasible else 'FAILED'})[/yellow]")
    console.print("")

def _get_node_load_pct(cluster: Cluster, node: Node, placements: Dict[str, str]) -> float:
    method = cluster.balancing.method
    vm_map = {v.name: v for v in cluster.vms}
    vms_here = [v for v, n in placements.items() if n == node.name]
    if method == "cpu": return sum(vm_map[v].cpu_usage for v in vms_here) / node.cpu_total if node.cpu_total else 0
    if method.endswith("psi"):
        # Sum of guest footprints
        p_field = "cpu_pressure" if "cpu" in method else "memory_pressure" if "memory" in method else "io_pressure"
        return sum(getattr(vm_map[v], p_field) for v in vms_here) / 100.0
    # memory
    return sum(vm_map[v].memory for v in vms_here) / node.memory_total if node.memory_total else 0

def write_junit_report(results, path):
    """Write test results to JUnit XML format."""
    suite = ET.Element("testsuite", {
        "name": "ProxLB Solver Scenarios",
        "tests": str(len(results)),
        "failures": str(sum(1 for _, _, _, errs in results if errs)),
    })
    for scenario_path, cluster, solution, errors in results:
        tc = ET.SubElement(suite, "testcase", {
            "name": cluster.name,
            "classname": str(scenario_path),
            "time": f"{solution.stats.wall_time_ms / 1000:.3f}",
        })
        if errors:
            failure = ET.SubElement(tc, "failure", {"message": "; ".join(errors)})
            failure.text = "\n".join(errors)
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(str(path), xml_declaration=True, encoding="unicode")

# Alias for backward compatibility
write_junit_xml = write_junit_report

_GB = 1024 * 1024 * 1024
_MB_FMT = 1024 * 1024
_ICON_OK = "\u2705"
_ICON_FAIL = "\u274c"

def _icon(ok: bool) -> str:
    return _ICON_OK if ok else _ICON_FAIL


# ── Expectation Checking ──

def _check_expectations(
    cluster: Cluster, solution: Solution,
    migration_plan: MigrationPlan | None = None,
) -> list[tuple[str, str, bool, str]]:
    """Return list of (check_name, expected, passed, detail) tuples."""
    checks: list[tuple[str, str, bool, str]] = []
    expect = cluster.expect

    # Feasibility
    if expect.feasible:
        ok = solution.feasible
        checks.append(("feasible", "True", ok,
                        solution.stats.status if not ok else "OK"))
    else:
        ok = not solution.feasible
        checks.append(("feasible", "False", ok,
                        "got feasible" if not ok else "OK"))

    if not solution.feasible:
        return checks

    # Constraints satisfied
    if expect.constraints_satisfied:
        errs = []
        for rule in cluster.constraints.anti_affinity:
            if not rule.get("hard", True): continue
            nodes_used = [solution.placements.get(v) for v in rule["vms"] if v in solution.placements]
            if len(nodes_used) != len(set(nodes_used)):
                errs.append(f"anti-affinity '{rule.get('name', '?')}' violated")
        for rule in cluster.constraints.affinity:
            if not rule.get("hard", True): continue
            ns = {solution.placements.get(v) for v in rule["vms"] if v in solution.placements}
            if len(ns) > 1:
                errs.append(f"affinity '{rule.get('name', '?')}' violated: {ns}")
        for rule in cluster.constraints.pin:
            placed = solution.placements.get(rule["vm"])
            if placed and placed not in rule["nodes"]:
                errs.append(f"pin {rule['vm']} on {placed}")
        for node in cluster.nodes:
            if node.maintenance:
                on_maint = [v for v, n in solution.placements.items() if n == node.name]
                if on_maint: errs.append(f"VMs on maintenance {node.name}: {on_maint}")
        for vm_name in cluster.constraints.ignore:
            vm = next((v for v in cluster.vms if v.name == vm_name), None)
            if vm and solution.placements.get(vm_name) != vm.node:
                errs.append(f"ignored VM {vm_name} was moved")
        ok = len(errs) == 0
        checks.append(("constraints", "satisfied", ok, "; ".join(errs) if errs else "OK"))

    # Spread improved
    if expect.spread_improved is not None:
        initial = _initial_load_gap(cluster)
        final = _compute_load_gap(cluster, solution.placements)
        improved = final < initial or initial == 0
        detail = f"{initial*100:.1f}% → {final*100:.1f}%"
        if expect.spread_improved:
            checks.append(("spread_improved", "True", improved, detail))
        else:
            checks.append(("spread_improved", "False", not improved, detail))

    # Max migrations
    if expect.max_migrations is not None:
        ok = solution.stats.migration_count <= expect.max_migrations
        checks.append(("max_migrations", f"≤{expect.max_migrations}", ok,
                        f"got {solution.stats.migration_count}"))

    # Node empty
    if expect.node_empty is not None:
        on_node = [v for v, n in solution.placements.items() if n == expect.node_empty]
        ok = len(on_node) == 0
        checks.append(("node_empty", expect.node_empty, ok,
                        f"still has {on_node}" if on_node else "OK"))

    # Placements
    for vm_name, expected_val in expect.placements.items():
        actual = solution.placements.get(vm_name)
        if expected_val.startswith("== "):
            ref_vm = expected_val[3:]
            ref_node = solution.placements.get(ref_vm)
            ok = actual == ref_node
            checks.append((f"placement:{vm_name}", f"== {ref_vm}", ok,
                           f"{actual} vs {ref_node}"))
        else:
            ok = actual == expected_val
            checks.append((f"placement:{vm_name}", expected_val, ok,
                           f"got {actual}"))

    # Path feasible — check from expect block or from solution feedback loop
    if expect.path_feasible is not None and migration_plan is not None:
        if expect.path_feasible:
            ok = migration_plan.path_feasible
            detail = f"unbreakable: {migration_plan.unbreakable_cycle}" if not ok else "OK"
        else:
            ok = not migration_plan.path_feasible
            detail = "path is feasible (expected infeasible)" if not ok else "OK"
        checks.append(("path_feasible", str(expect.path_feasible), ok, detail))
    elif not solution.path_feasible:
        # No explicit expectation, but solve_reachable flagged it as unreachable
        cycle_info = ""
        if migration_plan and migration_plan.unbreakable_cycle:
            cycle_info = f" — cycle: {migration_plan.unbreakable_cycle}"
        checks.append(("path_feasible", "reachable", False,
                        f"UNREACHABLE: no executable migration path{cycle_info}"))

    return checks


# ── Markdown Report ──

def _fmt_bytes(b: int) -> str:
    if b >= _GB: return f"{b / _GB:.1f} GB"
    return f"{b / _MB_FMT:.0f} MB"


def _node_load_pct(cluster: Cluster, node: Node, placements: Dict[str, str]) -> float:
    """Compute load percentage for a node given placements."""
    vm_map = {v.name: v for v in cluster.vms}
    vms_here = [v for v, n in placements.items() if n == node.name]
    method = cluster.balancing.method
    if method == "cpu":
        return sum(vm_map[v].cpu_usage for v in vms_here) / node.cpu_total if node.cpu_total else 0
    if "psi" in method:
        field = "cpu_pressure" if "cpu" in method else "memory_pressure" if "memory" in method else "io_pressure"
        return sum(getattr(vm_map[v], field) for v in vms_here) / 100.0
    return sum(vm_map[v].memory for v in vms_here) / node.memory_total if node.memory_total else 0


def write_markdown_report(
    results: list[tuple[str, Cluster, Solution]],
    path: str | Path,
    migration_plans: dict[str, MigrationPlan] | None = None,
) -> None:
    """Write a Markdown report."""
    path = Path(path)
    L: list[str] = []

    # Pre-compute checks
    all_checks = []
    scenario_checks = []
    for scenario_path, cluster, solution in results:
        plan = migration_plans.get(scenario_path) if migration_plans else None
        checks = _check_expectations(cluster, solution, plan)
        all_checks.extend(checks)
        scenario_checks.append((cluster.name, checks))

    passed = sum(1 for _, _, p, _ in all_checks if p)
    failed = sum(1 for _, _, p, _ in all_checks if not p)
    sc_passed = sum(1 for _, chks in scenario_checks if all(p for _, _, p, _ in chks))

    L.append("# ProxLB CP-SAT Solver — Test Report")
    L.append("")
    L.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("")
    L.append(f"**{len(results)}** scenarios | **{sc_passed}/{len(results)}** passed | "
             f"**{passed}** checks OK, **{failed}** failed")
    L.append("")

    # Overview table
    L.append("## Scenario Overview")
    L.append("")
    L.append("| Scenario | Feasible | Migrations | Load Gap | Result |")
    L.append("|----------|----------|------------|----------|--------|")
    for scenario_path, cluster, solution in results:
        plan = migration_plans.get(scenario_path) if migration_plans else None
        checks = _check_expectations(cluster, solution, plan)
        ok = all(p for _, _, p, _ in checks)
        icon = "\u2705" if ok else "\u274c"
        if solution.feasible:
            gap = _compute_load_gap(cluster, solution.placements)
            feasible_str = "\u26a0\ufe0f UNREACHABLE" if not solution.path_feasible else "Yes"
            L.append(f"| {cluster.name} | {feasible_str} | {solution.stats.migration_count} | {gap:.3f} | {icon} |")
        else:
            L.append(f"| {cluster.name} | No | — | — | {icon} |")
    L.append("")

    # Detail per scenario
    L.append("## Details")
    L.append("")
    for scenario_path, cluster, solution in results:
        L.append(f"### {cluster.name}")
        L.append("")
        if cluster.description:
            L.append(f"_{cluster.description}_")
            L.append("")
        L.append(f"- Method: `{cluster.balancing.method}` | Balanciness: {cluster.balancing.balanciness}")
        status_line = f"- Status: **{solution.stats.status}** | Solve time: {solution.stats.wall_time_ms:.1f} ms"
        if solution.reachability_attempts > 1:
            outcome = "resolved" if solution.path_feasible else "FAILED"
            status_line += f" | \u26a0\ufe0f Reachability: {solution.reachability_attempts} attempts ({outcome})"
        L.append(status_line)
        L.append("")

        if not solution.feasible:
            L.append(f"**INFEASIBLE** — {solution.stats.status}")
            L.append("")
            # Expectations
            plan = migration_plans.get(scenario_path) if migration_plans else None
            checks = _check_expectations(cluster, solution, plan)
            L.append("| Check | Expected | Result | Detail |")
            L.append("|-------|----------|--------|--------|")
            for name, exp, ok, detail in checks:
                L.append(f"| {name} | {exp} | {_icon(ok)} | {detail} |")
            L.append("")
            continue

        if not solution.path_feasible:
            L.append("> \u26a0\ufe0f **UNREACHABLE** — The solver found an optimal target state, "
                     "but no executable migration path exists. Circular dependencies "
                     "could not be resolved with temp-moves.")
            L.append("")

        # Node utilization
        initial = {v.name: v.node for v in cluster.vms}
        L.append("#### Node Utilization")
        L.append("")
        L.append("| Node | Before | After |")
        L.append("|------|--------|-------|")
        for node in cluster.nodes:
            if node.maintenance:
                L.append(f"| {node.name} | \u26a0\ufe0f maintenance | — |")
                continue
            before = _node_load_pct(cluster, node, initial)
            after = _node_load_pct(cluster, node, solution.placements)
            L.append(f"| {node.name} | {before*100:.1f}% | {after*100:.1f}% |")
        L.append("")

        # Constraints
        cons = cluster.constraints
        if cons.affinity or cons.anti_affinity or cons.pin or cons.ignore:
            L.append("#### Constraints")
            L.append("")
            for rule in cons.affinity:
                origin = rule.get("origin", "plb")
                hard = "hard" if rule.get("hard", True) else "soft"
                L.append(f"- **Affinity** `{rule['name']}` ({origin}, {hard}): {', '.join(rule['vms'])}")
            for rule in cons.anti_affinity:
                origin = rule.get("origin", "plb")
                hard = "hard" if rule.get("hard", True) else "soft"
                L.append(f"- **Anti-Affinity** `{rule['name']}` ({origin}, {hard}): {', '.join(rule['vms'])}")
            for rule in cons.pin:
                L.append(f"- **Pin** `{rule['vm']}` → {', '.join(rule['nodes'])}")
            if cons.ignore:
                L.append(f"- **Ignore**: {', '.join(cons.ignore)}")
            L.append("")

        # Migrations
        if solution.migrations:
            L.append("#### Migrations")
            L.append("")
            L.append("| VM | From | To | Priority | Rule Origin |")
            L.append("|----|------|----|----------|-------------|")
            vm_map = {v.name: v for v in cluster.vms}
            for m in solution.migrations:
                prio = vm_map[m.vm].priority
                L.append(f"| {m.vm} | {m.source} | {m.target} | {prio} | {_vm_rule_origins(m.vm, cluster)} |")
            L.append("")

            # Migration plan steps
            plan = migration_plans.get(scenario_path) if migration_plans else None
            if plan and plan.steps:
                total_cmds = sum(len(s.migrations) for s in plan.steps)
                L.append("#### Execution Plan")
                L.append("")
                L.append(f"_{total_cmds} Proxmox migration command(s) in {len(plan.steps)} step(s)._")
                L.append("")
                for step in plan.steps:
                    par = " (parallel)" if step.parallel else ""
                    L.append(f"**Step {step.step}**{par}:")
                    for m in step.migrations:
                        L.append(f"- {m.vm}: {m.source} → {m.target}")
                    L.append("")
                if plan.temp_moves:
                    L.append(f"Temp moves: {', '.join(plan.temp_moves)}")
                    L.append("")
                if plan.pve_deferred:
                    L.append(f"PVE HA deferred (not issued by ProxLB): {', '.join(plan.pve_deferred)}")
                    L.append("")
                if not plan.path_feasible:
                    L.append(f"\u26a0\ufe0f **Path infeasible** — unbreakable cycle: {plan.unbreakable_cycle}")
                    L.append("")

                # Dependency graph (Mermaid)
                if plan.dependency_edges:
                    mermaid = _mermaid_graph(plan, solution.migrations)
                    if mermaid:
                        L.append("#### Dependency Graph")
                        L.append("")
                        L.append("```mermaid")
                        L.append(mermaid)
                        L.append("```")
                        L.append("")

        # Expectations
        plan = migration_plans.get(scenario_path) if migration_plans else None
        checks = _check_expectations(cluster, solution, plan)
        L.append("#### Expectations")
        L.append("")
        L.append("| Check | Expected | Result | Detail |")
        L.append("|-------|----------|--------|--------|")
        for name, exp, ok, detail in checks:
            L.append(f"| {name} | {exp} | {_icon(ok)} | {detail} |")
        L.append("")

    path.write_text("\n".join(L), encoding="utf-8")


# ── HTML Report ──

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def write_html_report(
    results: list[tuple[str, Cluster, Solution]],
    path: str | Path,
    migration_plans: dict[str, MigrationPlan] | None = None,
) -> None:
    """Write a self-contained HTML report with sidebar navigation."""
    path = Path(path)
    h: list[str] = []

    # Pre-compute checks per scenario
    sc_data = []
    all_passed_count = 0
    all_failed_count = 0
    for scenario_path, cluster, solution in results:
        plan = migration_plans.get(scenario_path) if migration_plans else None
        checks = _check_expectations(cluster, solution, plan)
        ok = all(p for _, _, p, _ in checks)
        if ok: all_passed_count += 1
        all_failed_count += sum(1 for _, _, p, _ in checks if not p)
        sc_data.append((scenario_path, cluster, solution, plan, checks, ok))

    total_checks = sum(len(c) for _, _, _, _, c, _ in sc_data)

    h.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    h.append("<title>ProxLB Solver Report</title>")
    h.append("<style>")
    h.append("""
:root { --bg: #ffffff; --fg: #1f2937; --card: #f9fafb; --accent: #2563eb;
        --ok: #16a34a; --err: #dc2626; --warn: #d97706; --border: #e5e7eb;
        --sidebar: #f3f4f6; --sidebar-w: 260px; --th: #1e40af;
        --hover: #f0f4ff; --meta: #6b7280; --heading: #111827; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg);
       color: var(--fg); display: flex; min-height: 100vh; font-size: 14px; }
nav { position: fixed; left: 0; top: 0; bottom: 0; width: var(--sidebar-w);
      background: var(--sidebar); border-right: 1px solid var(--border);
      overflow-y: auto; padding: 16px 0; z-index: 10; }
nav .brand { color: var(--accent); font-weight: 700; font-size: 16px;
             padding: 0 16px 12px; border-bottom: 1px solid var(--border); }
nav ul { list-style: none; padding: 8px 0; }
nav li a { display: flex; align-items: center; justify-content: space-between;
           padding: 6px 16px; color: var(--fg); text-decoration: none;
           font-size: 13px; transition: background .15s; }
nav li a:hover { background: var(--border); }
nav .section { color: var(--meta); text-transform: uppercase; font-size: 11px;
               letter-spacing: .5px; padding: 12px 16px 4px; cursor: default; }
.badge { font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 600; }
.badge-pass { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
.badge-fail { background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
main { margin-left: var(--sidebar-w); padding: 24px 32px; flex: 1; max-width: 1200px; }
h1 { color: var(--heading); margin-bottom: 4px; }
h2 { color: var(--heading); margin: 24px 0 12px; border-bottom: 2px solid var(--border); padding-bottom: 6px; }
h3 { margin: 16px 0 8px; color: var(--heading); }
h4 { margin: 12px 0 6px; color: var(--accent); }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 16px 20px; margin-bottom: 16px; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 12px; margin: 12px 0; }
.stat { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
        padding: 12px; text-align: center; }
.stat .num { font-size: 28px; font-weight: 700; color: var(--heading); }
.stat .label { font-size: 12px; color: var(--meta); margin-top: 2px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 13px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--th); font-weight: 600; background: #f0f4ff; }
tr:hover { background: var(--hover); }
.bar-bg { display: inline-block; width: 100px; height: 12px; background: #e5e7eb;
          border-radius: 3px; overflow: hidden; vertical-align: middle; }
.bar-fill { height: 100%; border-radius: 3px; }
.ok { color: var(--ok); } .err { color: var(--err); } .warn { color: var(--warn); }
.tag { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px;
       font-weight: 600; margin-right: 4px; }
.tag-pass { background: #dcfce7; color: #15803d; }
.tag-fail { background: #fee2e2; color: #b91c1c; }
.tag-info { background: #dbeafe; color: #1e40af; }
.meta { color: var(--meta); font-size: 12px; }
a { color: var(--accent); }
a:hover { text-decoration: underline; }
""")
    h.append("</style>")
    h.append('<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>')
    h.append("<script>mermaid.initialize({startOnLoad:true, theme:'default'});</script>")
    h.append("</head><body>")

    # Sidebar
    h.append("<nav>")
    h.append('<div class="brand">ProxLB Solver</div>')
    h.append("<ul>")
    h.append('<li><a href="#summary">Summary</a></li>')
    h.append('<li><a href="#overview">Scenario Overview</a></li>')
    h.append('<li><a class="section">Scenarios</a></li>')
    for _, cluster, _, _, checks, ok in sc_data:
        slug = _slug(cluster.name)
        cls = "badge-pass" if ok else "badge-fail"
        txt = "PASS" if ok else "FAIL"
        h.append(f'<li><a href="#{slug}">{cluster.name}<span class="badge {cls}">{txt}</span></a></li>')
    h.append("</ul></nav>")

    # Main
    h.append("<main>")

    # Summary
    h.append('<div id="summary">')
    h.append("<h1>ProxLB CP-SAT Solver Report</h1>")
    h.append(f'<p class="meta">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')
    h.append('<div class="summary-grid">')
    h.append(f'<div class="stat"><div class="num">{len(results)}</div><div class="label">Scenarios</div></div>')
    h.append(f'<div class="stat"><div class="num ok">{all_passed_count}</div><div class="label">Passed</div></div>')
    failed_sc = len(results) - all_passed_count
    h.append(f'<div class="stat"><div class="num {"err" if failed_sc else "ok"}">{failed_sc}</div><div class="label">Failed</div></div>')
    h.append(f'<div class="stat"><div class="num">{total_checks - all_failed_count}/{total_checks}</div><div class="label">Checks OK</div></div>')
    h.append("</div></div>")

    # Overview table
    h.append('<div id="overview">')
    h.append("<h2>Scenario Overview</h2>")
    h.append("<table><tr><th>Scenario</th><th>Feasible</th><th>Migrations</th>"
             "<th>Load Gap</th><th>Steps</th><th>Result</th></tr>")
    for scenario_path, cluster, solution, plan, checks, ok in sc_data:
        slug = _slug(cluster.name)
        badge = f'<span class="tag tag-pass">PASS</span>' if ok else '<span class="tag tag-fail">FAIL</span>'
        if solution.feasible:
            gap = _compute_load_gap(cluster, solution.placements)
            steps = len(plan.steps) if plan else "—"
            if not solution.path_feasible:
                feas_cell = '<td class="warn">\u26a0\ufe0f UNREACHABLE</td>'
            else:
                feas_cell = '<td class="ok">Yes</td>'
            h.append(f'<tr><td><a href="#{slug}">{cluster.name}</a></td>{feas_cell}'
                     f'<td>{solution.stats.migration_count}</td><td>{gap:.3f}</td>'
                     f'<td>{steps}</td><td>{badge}</td></tr>')
        else:
            h.append(f'<tr><td><a href="#{slug}">{cluster.name}</a></td><td class="err">No</td>'
                     f'<td>—</td><td>—</td><td>—</td><td>{badge}</td></tr>')
    h.append("</table></div>")

    # Detailed sections
    h.append("<h2>Detailed Results</h2>")
    for scenario_path, cluster, solution, plan, checks, ok in sc_data:
        slug = _slug(cluster.name)
        vm_map = {v.name: v for v in cluster.vms}
        initial = {v.name: v.node for v in cluster.vms}

        h.append(f'<div class="card" id="{slug}">')
        h.append(f'<h3>{cluster.name} {_html_badge(ok)}</h3>')
        if cluster.description:
            h.append(f'<p class="meta">{cluster.description}</p>')
        retry_info = ""
        if solution.reachability_attempts > 1:
            outcome = "resolved" if solution.path_feasible else "FAILED"
            retry_info = (f' | <span class="warn">\u26a0\ufe0f Reachability: '
                          f'{solution.reachability_attempts} attempts ({outcome})</span>')
        h.append(f'<p class="meta">Method: <code>{cluster.balancing.method}</code> '
                 f'| Balanciness: {cluster.balancing.balanciness} '
                 f'| Status: <b>{solution.stats.status}</b> '
                 f'| {solution.stats.wall_time_ms:.1f} ms{retry_info}</p>')

        if not solution.feasible:
            h.append(f'<p class="err"><b>INFEASIBLE</b> — {solution.stats.status}</p>')
            if solution.blocking_vms:
                h.append(f'<p>Blockers: {", ".join(solution.blocking_vms)}</p>')
            _html_checks_table(h, checks)
            h.append("</div>")
            continue

        if not solution.path_feasible:
            h.append('<p class="warn"><b>\u26a0\ufe0f UNREACHABLE</b> — '
                     'The solver found an optimal target state, but no executable '
                     'migration path exists. Circular dependencies could not be '
                     'resolved with temp-moves.</p>')

        # Node utilization
        h.append("<h4>Node Utilization</h4>")
        h.append("<table><tr><th>Node</th><th>Before</th><th></th><th>After</th><th>Bar</th></tr>")
        for node in cluster.nodes:
            if node.maintenance:
                h.append(f'<tr><td>{node.name}</td><td colspan="4" class="warn">maintenance</td></tr>')
                continue
            before = _node_load_pct(cluster, node, initial)
            after = _node_load_pct(cluster, node, solution.placements)
            color = "var(--ok)" if after < 0.8 else "var(--warn)" if after < 0.95 else "var(--err)"
            h.append(f'<tr><td>{node.name}</td><td>{before*100:.1f}%</td><td>→</td>'
                     f'<td>{after*100:.1f}%</td>'
                     f'<td><div class="bar-bg"><div class="bar-fill" style="width:{min(after*100,100):.0f}%;background:{color}"></div></div></td></tr>')
        h.append("</table>")

        # Constraints
        cons = cluster.constraints
        if cons.affinity or cons.anti_affinity or cons.pin or cons.ignore:
            h.append("<h4>Constraints</h4>")
            h.append("<ul>")
            for rule in cons.affinity:
                origin = rule.get("origin", "plb")
                hard = "hard" if rule.get("hard", True) else "soft"
                h.append(f'<li><strong>Affinity</strong> <code>{rule["name"]}</code> '
                         f'<span class="tag tag-info">{origin}</span> {hard}: {", ".join(rule["vms"])}</li>')
            for rule in cons.anti_affinity:
                origin = rule.get("origin", "plb")
                hard = "hard" if rule.get("hard", True) else "soft"
                h.append(f'<li><strong>Anti-Affinity</strong> <code>{rule["name"]}</code> '
                         f'<span class="tag tag-info">{origin}</span> {hard}: {", ".join(rule["vms"])}</li>')
            for rule in cons.pin:
                h.append(f'<li><strong>Pin</strong> <code>{rule["vm"]}</code> &rarr; {", ".join(rule["nodes"])}</li>')
            if cons.ignore:
                h.append(f'<li><strong>Ignore</strong>: {", ".join(cons.ignore)}</li>')
            h.append("</ul>")

        # Migrations
        if solution.migrations:
            h.append("<h4>Migrations</h4>")
            h.append("<table><tr><th>VM</th><th>From</th><th>To</th><th>Memory</th><th>Priority</th><th>Rule Origin</th></tr>")
            for m in solution.migrations:
                vm = vm_map[m.vm]
                h.append(f'<tr><td>{m.vm}</td><td>{m.source}</td><td>{m.target}</td>'
                         f'<td>{_fmt_bytes(vm.memory)}</td><td>{vm.priority}</td><td>{_vm_rule_origins(m.vm, cluster)}</td></tr>')
            h.append("</table>")

            # Execution plan
            if plan and plan.steps:
                total_cmds = sum(len(s.migrations) for s in plan.steps)
                h.append("<h4>Execution Plan</h4>")
                h.append(f'<p class="meta">{total_cmds} Proxmox migration command(s) in {len(plan.steps)} step(s). '
                         f'Each VM row is one API call to Proxmox.</p>')
                h.append("<table><tr><th>Step</th><th>VM</th><th>From</th><th>To</th><th>Parallel</th></tr>")
                for step in plan.steps:
                    par = "Yes" if step.parallel else "No"
                    n = len(step.migrations)
                    for i, m in enumerate(step.migrations):
                        if i == 0:
                            h.append(f'<tr><td rowspan="{n}">{step.step}</td><td>{m.vm}</td>'
                                     f'<td>{m.source}</td><td>{m.target}</td>'
                                     f'<td rowspan="{n}">{par}</td></tr>')
                        else:
                            h.append(f'<tr><td>{m.vm}</td><td>{m.source}</td><td>{m.target}</td></tr>')
                h.append("</table>")
                if plan.temp_moves:
                    h.append(f'<p class="warn">Temp moves: {", ".join(plan.temp_moves)}</p>')
                if plan.pve_deferred:
                    h.append(f'<p class="meta">PVE HA deferred — migration triggered by PVE HA, not ProxLB: '
                             f'{", ".join(f"<code>{v}</code>" for v in plan.pve_deferred)}</p>')
                if not plan.path_feasible:
                    h.append(f'<p class="err"><b>Path infeasible</b> — unbreakable cycle: {plan.unbreakable_cycle}</p>')

            # Dependency graph (Mermaid)
            if plan and plan.dependency_edges:
                mermaid = _mermaid_graph(plan, solution.migrations)
                if mermaid:
                    h.append("<h4>Dependency Graph</h4>")
                    h.append(f'<pre class="mermaid">{mermaid}</pre>')

        # Expectations
        _html_checks_table(h, checks)
        h.append("</div>")

    h.append("</main></body></html>")
    path.write_text("\n".join(h), encoding="utf-8")


def _mermaid_graph(plan: MigrationPlan, migrations: list[Migration]) -> str | None:
    """Build a Mermaid graph LR string from dependency edges and migrations."""
    if not plan or not plan.dependency_edges:
        return None
    mig_map = {m.vm: m for m in migrations}
    lines = ["graph LR"]
    # Define nodes with labels
    seen = set()
    for a, b in plan.dependency_edges:
        for vm_name in (a, b):
            if vm_name not in seen:
                seen.add(vm_name)
                m = mig_map.get(vm_name)
                if m:
                    lines.append(f'    {vm_name}["{vm_name}: {m.source} → {m.target}"]')
                else:
                    lines.append(f'    {vm_name}["{vm_name}"]')
    # Edges: a waits for b  →  b --> a  (b must go first)
    for a, b in plan.dependency_edges:
        lines.append(f"    {b} --> {a}")
    if plan.temp_moves:
        for vm_name in plan.temp_moves:
            lines.append(f"    style {vm_name} stroke:#d97706,stroke-width:2px")
    return "\n".join(lines)


def _html_badge(ok: bool) -> str:
    if ok: return '<span class="tag tag-pass">PASS</span>'
    return '<span class="tag tag-fail">FAIL</span>'


def _html_checks_table(h: list[str], checks: list[tuple[str, str, bool, str]]) -> None:
    h.append("<h4>Expectations</h4>")
    h.append("<table><tr><th>Check</th><th>Expected</th><th>Result</th><th>Detail</th></tr>")
    for name, exp, ok, detail in checks:
        icon = '<span class="ok">\u2705</span>' if ok else '<span class="err">\u274c</span>'
        h.append(f'<tr><td>{name}</td><td>{exp}</td><td>{icon}</td><td>{detail}</td></tr>')
    h.append("</table>")
