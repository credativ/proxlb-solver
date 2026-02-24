"""Rich terminal, JUnit XML and Markdown reporter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import Cluster, Expect, MigrationPlan, Solution
from .solver import _BALANCINESS_PROFILES, _initial_load_gap, _resolve_balancing

_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024

_BALANCINESS_NAMES = {
    1: "Conservative",
    2: "Low",
    3: "Moderate",
    4: "High",
    5: "Aggressive",
}


def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _md_bar(pct: float, width: int = 20) -> str:
    """Unicode progress bar for markdown."""
    filled = int(pct / 100 * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def print_report(cluster: Cluster, solution: Solution) -> None:
    """Print a rich terminal report of before/after placement."""
    console = Console()

    console.print(f"\n[bold]{cluster.name}[/bold]")
    console.print(f"  {cluster.description}\n")

    if not solution.feasible:
        console.print("[red bold]INFEASIBLE[/red bold] — no valid placement found.")
        console.print(f"  Solver status: {solution.stats.status}")
        if solution.blocking_vms:
            console.print("\n[red]Blocking VMs (preventing evacuation):[/red]")
            vm_map_local = {v.name: v for v in cluster.vms}
            for bv in solution.blocking_vms:
                vm = vm_map_local.get(bv)
                if vm:
                    console.print(
                        f"  - {bv}: {vm.cpu} CPU, "
                        f"{vm.memory / _GB:.0f} GB RAM, on {vm.node}"
                    )
        return

    # Node utilization table
    table = Table(title="Node Utilization (Before → After)")
    table.add_column("Node")
    table.add_column("Before RAM")
    table.add_column("")
    table.add_column("After RAM")
    table.add_column("")

    node_map = {n.name: n for n in cluster.nodes}
    vm_map = {v.name: v for v in cluster.vms}

    for node in cluster.nodes:
        # Before
        before_used = sum(
            v.memory for v in cluster.vms if v.node == node.name
        )
        before_pct = before_used / node.memory_total * 100 if node.memory_total else 0

        # After
        after_used = sum(
            vm_map[vm_name].memory
            for vm_name, target in solution.placements.items()
            if target == node.name
        )
        after_pct = after_used / node.memory_total * 100 if node.memory_total else 0

        maint = " [yellow](maint)[/yellow]" if node.maintenance else ""

        table.add_row(
            f"{node.name}{maint}",
            f"{before_used / _GB:.1f} GB ({before_pct:.0f}%)",
            _bar(before_pct),
            f"{after_used / _GB:.1f} GB ({after_pct:.0f}%)",
            _bar(after_pct),
        )

    console.print(table)

    # Migration table
    if solution.migrations:
        mig_table = Table(title="Migrations")
        mig_table.add_column("VM")
        mig_table.add_column("From")
        mig_table.add_column("To")
        mig_table.add_column("RAM")

        for mig in solution.migrations:
            vm = vm_map[mig.vm]
            mig_table.add_row(
                mig.vm, mig.source, mig.target,
                f"{vm.memory / _GB:.1f} GB",
            )
        console.print(mig_table)
    else:
        console.print("[green]No migrations needed.[/green]")

    # Stats
    console.print(f"\n  Status: {solution.stats.status}")
    console.print(f"  Load gap: {solution.stats.load_gap:.3f}")
    console.print(f"  Migrations: {solution.stats.migration_count}")
    console.print(f"  Solve time: {solution.stats.wall_time_ms:.1f} ms\n")


def write_junit_xml(
    results: list[tuple[str, Cluster, Solution, list[str]]],
    path: str | Path,
) -> None:
    """Write JUnit XML results file.

    results: list of (scenario_path, cluster, solution, errors)
    """
    path = Path(path)
    suite = ET.Element("testsuite", {
        "name": "proxlb-solver",
        "tests": str(len(results)),
        "failures": str(sum(1 for _, _, _, errs in results if errs)),
    })

    for scenario_path, cluster, solution, errors in results:
        tc = ET.SubElement(suite, "testcase", {
            "name": cluster.name,
            "classname": scenario_path,
            "time": f"{solution.stats.wall_time_ms / 1000:.3f}",
        })
        if errors:
            failure = ET.SubElement(tc, "failure", {
                "message": "; ".join(errors),
            })
            failure.text = "\n".join(errors)

    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(str(path), xml_declaration=True, encoding="unicode")


# ── Markdown helpers ──

def _compute_load_gap(cluster: Cluster, placements: dict[str, str]) -> float:
    loads = []
    for node in cluster.nodes:
        if node.maintenance:
            continue
        used = sum(
            vm.memory for vm in cluster.vms
            if placements.get(vm.name) == node.name
        )
        pct = used / node.memory_total if node.memory_total else 0
        loads.append(pct)
    if not loads:
        return 0.0
    return max(loads) - min(loads)


def _check_expectations(
    cluster: Cluster, solution: Solution
) -> list[tuple[str, str, bool, str]]:
    """Evaluate all expect checks. Returns list of (check, expected, passed, detail)."""
    expect = cluster.expect
    vm_map = {v.name: v for v in cluster.vms}
    node_map = {n.name: n for n in cluster.nodes}
    checks: list[tuple[str, str, bool, str]] = []

    # Feasibility
    if expect.feasible:
        passed = solution.feasible
        detail = solution.stats.status
        checks.append(("Feasible", "yes", passed, detail))
    else:
        passed = not solution.feasible
        detail = solution.stats.status
        checks.append(("Infeasible", "yes", passed, detail))

    if not solution.feasible:
        return checks

    # Constraints satisfied
    if expect.constraints_satisfied:
        constraint_errors = []

        for rule in cluster.constraints.anti_affinity:
            vm_nodes = [
                solution.placements.get(v) for v in rule["vms"]
                if v in solution.placements
            ]
            if len(vm_nodes) != len(set(vm_nodes)):
                constraint_errors.append(
                    "Anti-affinity '%s' violated" % rule["name"]
                )

        for rule in cluster.constraints.affinity:
            nodes_used = {
                solution.placements.get(v) for v in rule["vms"]
                if v in solution.placements
            }
            if len(nodes_used) > 1:
                constraint_errors.append(
                    "Affinity '%s' violated" % rule["name"]
                )

        for rule in cluster.constraints.pin:
            vm_name = rule["vm"]
            if vm_name in solution.placements:
                placed = solution.placements[vm_name]
                if placed not in rule["nodes"]:
                    constraint_errors.append(
                        "Pin '%s' on %s, want %s"
                        % (vm_name, placed, rule["nodes"])
                    )

        for node in cluster.nodes:
            if node.maintenance:
                for vm_name, placed in solution.placements.items():
                    if placed == node.name:
                        constraint_errors.append(
                            "%s on maintenance %s" % (vm_name, node.name)
                        )

        for vm_name in cluster.constraints.ignore:
            vm = next(
                (v for v in cluster.vms if v.name == vm_name), None
            )
            if vm and solution.placements.get(vm_name) != vm.node:
                constraint_errors.append(
                    "Ignored %s was moved" % vm_name
                )

        node_used: dict[str, int] = defaultdict(int)
        for vm_name, target in solution.placements.items():
            node_used[target] += vm_map[vm_name].memory
        for node_name, used in node_used.items():
            cap = node_map[node_name].memory_total
            if used > cap:
                constraint_errors.append(
                    "RAM overflow %s: %d > %d" % (node_name, used, cap)
                )

        passed = len(constraint_errors) == 0
        detail = "all satisfied" if passed else "; ".join(constraint_errors)
        checks.append(("Constraints", "satisfied", passed, detail))

    # Spread improved
    if expect.spread_improved is True:
        initial_plc = {vm.name: vm.node for vm in cluster.vms}
        initial_gap = _compute_load_gap(cluster, initial_plc)
        new_gap = _compute_load_gap(cluster, solution.placements)
        passed = new_gap < initial_gap or initial_gap == 0
        detail = "%.1f%% \u2192 %.1f%%" % (initial_gap * 100, new_gap * 100)
        checks.append(("Spread improved", "yes", passed, detail))

    # Max migrations
    if expect.max_migrations is not None:
        actual = solution.stats.migration_count
        passed = actual <= expect.max_migrations
        detail = "%d (max %d)" % (actual, expect.max_migrations)
        checks.append(("Max migrations", "\u2264 %d" % expect.max_migrations, passed, detail))

    # Node empty (evacuation)
    if expect.node_empty is not None:
        vms_remaining = [
            vn for vn, placed in solution.placements.items()
            if placed == expect.node_empty
        ]
        passed = len(vms_remaining) == 0
        if passed:
            detail = "%s is empty" % expect.node_empty
        else:
            detail = "%s still has: %s" % (
                expect.node_empty, ", ".join(vms_remaining)
            )
        checks.append((
            "Node empty", expect.node_empty, passed, detail
        ))

    # Specific placements
    for vm_name, expected in expect.placements.items():
        actual = solution.placements.get(vm_name, "?")
        if expected.startswith("== "):
            ref_vm = expected[3:]
            ref_node = solution.placements.get(ref_vm, "?")
            passed = actual == ref_node
            detail = "%s \u2192 %s (ref %s \u2192 %s)" % (
                vm_name, actual, ref_vm, ref_node
            )
            checks.append((
                "Placement %s == %s" % (vm_name, ref_vm),
                "same node", passed, detail
            ))
        else:
            passed = actual == expected
            detail = "%s \u2192 %s" % (vm_name, actual)
            checks.append((
                "Placement %s" % vm_name,
                expected, passed, detail
            ))

    return checks


def _render_migration_plan(
    lines: list[str],
    cluster: Cluster,
    plan: MigrationPlan,
) -> None:
    """Render migration plan section: Mermaid graph, step plan, state table."""
    vm_map = {v.name: v for v in cluster.vms}
    node_map = {n.name: n for n in cluster.nodes}

    if not plan.steps:
        return

    lines.append("#### Migration Plan")
    lines.append("")

    # Mermaid dependency graph (only if there are dependencies)
    mig_map = {}
    for step in plan.steps:
        for m in step.migrations:
            mig_map[m.vm] = m

    if plan.dependency_edges:
        lines.append("##### Dependency Graph")
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph LR")
        rendered_nodes = set()
        for vm_a, vm_b in plan.dependency_edges:
            for vm_name in (vm_a, vm_b):
                if vm_name not in rendered_nodes:
                    m = mig_map.get(vm_name)
                    if m:
                        lines.append(
                            '    %s["%s: %s→%s"]'
                            % (vm_name, vm_name, m.source, m.target)
                        )
                    rendered_nodes.add(vm_name)
            # Arrow: vm_b must finish before vm_a can start
            lines.append("    %s --> %s" % (vm_b, vm_a))
        lines.append("```")
        lines.append("")

    # Temp moves note
    if plan.temp_moves:
        lines.append(
            "**Temp moves required:** %s"
            % ", ".join("`%s`" % vm for vm in plan.temp_moves)
        )
        lines.append("")

    # Step plan
    lines.append("##### Execution Steps")
    lines.append("")
    for step in plan.steps:
        par_label = " (parallel)" if step.parallel else ""
        lines.append("**Step %d%s:**" % (step.step, par_label))
        lines.append("")
        for m in step.migrations:
            vm = vm_map.get(m.vm)
            ram = "%.0f GB" % (vm.memory / _GB) if vm else "?"
            is_temp = m.vm in plan.temp_moves and any(
                tm.vm == m.vm and tm.target != m.target
                for s in plan.steps for tm in s.migrations
            )
            suffix = " *(temp)*" if (
                m.vm in plan.temp_moves
                and m.target not in {
                    mig_map[m.vm].target
                    for _ in [1] if m.vm in mig_map
                }
            ) else ""
            lines.append(
                "- `%s`: %s → %s  (%s)%s"
                % (m.vm, m.source, m.target, ram, suffix)
            )
        lines.append("")

    # Cluster state per step
    lines.append("##### Cluster State per Step")
    lines.append("")

    active_nodes = [n for n in cluster.nodes if not n.maintenance]
    header = "| Step |"
    sep = "|------|"
    for n in active_nodes:
        header += " %s |" % n.name
        sep += "--------|"
    lines.append(header)
    lines.append(sep)

    # Track node usage through steps
    current_used: dict[str, int] = defaultdict(int)
    for vm in cluster.vms:
        current_used[vm.node] += vm.memory

    # Initial row
    row = "| Initial |"
    for n in active_nodes:
        used = current_used[n.name]
        pct = used / n.memory_total * 100 if n.memory_total else 0
        row += " %.0f GB (%d%%) |" % (used / _GB, pct)
    lines.append(row)

    # Each step
    for step in plan.steps:
        for m in step.migrations:
            vm = vm_map.get(m.vm)
            if vm:
                current_used[m.source] -= vm.memory
                current_used[m.target] += vm.memory
        row = "| Step %d |" % step.step
        for n in active_nodes:
            used = current_used[n.name]
            pct = used / n.memory_total * 100 if n.memory_total else 0
            row += " %.0f GB (%d%%) |" % (used / _GB, pct)
        lines.append(row)

    lines.append("")


def write_markdown_report(
    results: list[tuple[str, Cluster, Solution]],
    path: str | Path,
    migration_plans: dict[str, MigrationPlan] | None = None,
) -> None:
    """Write a comprehensive Markdown report.

    results: list of (scenario_rel_path, cluster, solution)
    migration_plans: optional dict of scenario_path -> MigrationPlan
    """
    path = Path(path)
    lines: list[str] = []

    total = len(results)
    all_checks: list[tuple[str, str, bool, str]] = []
    scenario_checks: list[tuple[str, list[tuple[str, str, bool, str]]]] = []
    for _, cluster, solution in results:
        checks = _check_expectations(cluster, solution)
        all_checks.extend(checks)
        scenario_checks.append((cluster.name, checks))

    passed = sum(1 for _, _, p, _ in all_checks if p)
    failed = sum(1 for _, _, p, _ in all_checks if not p)
    scenarios_passed = sum(
        1 for _, checks in scenario_checks
        if all(p for _, _, p, _ in checks)
    )

    # Header
    lines.append("# ProxLB CP-SAT Solver \u2014 Test Report")
    lines.append("")
    lines.append("Generated: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    if failed == 0:
        status_badge = "\u2705 **ALL PASSED**"
    else:
        status_badge = "\u274c **%d FAILED**" % failed
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append("| **Status** | %s |" % status_badge)
    lines.append(
        "| **Scenarios** | %d / %d passed |" % (scenarios_passed, total)
    )
    lines.append(
        "| **Checks** | %d passed, %d failed |" % (passed, failed)
    )
    lines.append("")

    # Parameter reference
    lines.append("## Parameter Reference")
    lines.append("")
    lines.append("### Balancing")
    lines.append("")
    lines.append("| Parameter | Type | Default | Description |")
    lines.append("|-----------|------|---------|-------------|")
    lines.append(
        "| `method` | string | `memory` |"
        " Balancing metric (`memory`) |"
    )
    lines.append(
        "| `balanciness` | int (1\u20135) | `3` |"
        " DRS-style aggressiveness level"
        " (see table below) |"
    )
    lines.append(
        "| `cpu_overcommit` | float | `2.0` |"
        " CPU overcommit factor \u2014"
        " effective CPU per node ="
        " `cpu_total \u00d7 cpu_overcommit` |"
    )
    lines.append(
        "| `w_balance` | int | *derived* |"
        " Manual override: weight for load-gap"
        " minimization in the objective |"
    )
    lines.append(
        "| `w_stickiness` | int | *derived* |"
        " Manual override: weight for migration"
        " penalty in the objective |"
    )
    lines.append("")
    lines.append("#### Balanciness Levels")
    lines.append("")
    lines.append(
        "| Level | Name | w_balance | w_stickiness |"
        " Migration Threshold | Behavior |"
    )
    lines.append(
        "|-------|------|-----------|--------------|"
        "--------------------|----------|"
    )
    lines.append(
        "| 1 | Conservative | 0 | 1 | \u2014 |"
        " Only mandatory migrations"
        " (maintenance, constraints) |"
    )
    lines.append(
        "| 2 | Low | 1 | 50 | 25% |"
        " Migrate only if load gap > 25% |"
    )
    lines.append(
        "| 3 | **Moderate** | 10 | 10 | 15% |"
        " Default \u2014 balanced cost/benefit |"
    )
    lines.append(
        "| 4 | High | 50 | 5 | 5% |"
        " Active rebalancing |"
    )
    lines.append(
        "| 5 | Aggressive | 100 | 1 | 0% |"
        " Chase near-perfect balance |"
    )
    lines.append("")
    lines.append(
        "The **migration threshold** prevents unnecessary migrations:"
        " if the current load gap is already below the threshold"
        " for the selected level, no voluntary migrations occur"
        " (only hard-constraint moves like maintenance evacuation)."
    )
    lines.append("")
    lines.append("### Constraints")
    lines.append("")
    lines.append("| Constraint | YAML Key | Description |")
    lines.append("|------------|----------|-------------|")
    lines.append(
        "| **Affinity** | `constraints.affinity` |"
        " VMs in the group must be placed on the"
        " same node |"
    )
    lines.append(
        "| **Anti-Affinity** | `constraints.anti_affinity` |"
        " VMs in the group must be on different"
        " nodes |"
    )
    lines.append(
        "| **Pin** | `constraints.pin` |"
        " VM may only run on the listed nodes |"
    )
    lines.append(
        "| **Ignore** | `constraints.ignore` |"
        " VM stays on its current node \u2014"
        " solver will not move it |"
    )
    lines.append(
        "| **Maintenance** | `nodes.<name>.maintenance` |"
        " No VMs may be placed on this node \u2014"
        " existing VMs are evacuated |"
    )
    lines.append(
        "| **Evacuate** | `evacuate_node` |"
        " Drain all VMs from the named node"
        " (like on-demand maintenance) |"
    )
    lines.append("")
    lines.append("### Solver Objective")
    lines.append("")
    lines.append(
        "The solver minimizes:"
    )
    lines.append("")
    lines.append(
        "```"
    )
    lines.append(
        "Objective = w_balance \u00d7 LoadGap"
        " + w_stickiness \u00d7 MigrationCount"
    )
    lines.append(
        "```"
    )
    lines.append("")
    lines.append(
        "- **LoadGap**: `max(node_load%) \u2212 min(node_load%)`"
        " across all non-maintenance nodes"
        " (RAM utilization, scaled \u00d71000 for integer precision)"
    )
    lines.append(
        "- **MigrationCount**: Number of VMs placed"
        " on a different node than their current one"
    )
    lines.append(
        "- Lower objective = better balance with fewer migrations"
    )
    lines.append("")
    lines.append("### Expectations (`expect` block)")
    lines.append("")
    lines.append("| Field | Type | Description |")
    lines.append("|-------|------|-------------|")
    lines.append(
        "| `feasible` | bool |"
        " Whether a valid placement must exist |"
    )
    lines.append(
        "| `constraints_satisfied` | bool |"
        " Verify all constraints are respected |"
    )
    lines.append(
        "| `spread_improved` | bool |"
        " Load gap must be smaller than before |"
    )
    lines.append(
        "| `max_migrations` | int |"
        " Upper bound on migration count |"
    )
    lines.append(
        "| `node_empty` | string |"
        " Assert that the named node has 0 VMs"
        " after solving |"
    )
    lines.append(
        "| `placements` | map |"
        " Assert specific VM placements"
        " (`vm: node` or `vm: \"== other_vm\""
        "` for co-location) |"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Overview table
    lines.append("## Scenario Overview")
    lines.append("")
    lines.append(
        "| Scenario | Feasible | Migrations | Load Gap | Result |"
    )
    lines.append("|----------|----------|------------|----------|--------|")
    for scenario_path, cluster, solution in results:
        checks = _check_expectations(cluster, solution)
        all_ok = all(p for _, _, p, _ in checks)
        icon = "\u2705" if all_ok else "\u274c"
        if solution.feasible:
            mig = str(solution.stats.migration_count)
            gap = "%.1f%%" % (solution.stats.load_gap * 100)
            feas = "yes"
        else:
            mig = "\u2014"
            gap = "\u2014"
            feas = "no"
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (cluster.name, feas, mig, gap, icon)
        )
    lines.append("")

    # Detailed sections per scenario
    lines.append("---")
    lines.append("")
    lines.append("## Detailed Results")
    lines.append("")

    vm_map_global: dict[str, any] = {}

    for scenario_path, cluster, solution in results:
        vm_map = {v.name: v for v in cluster.vms}
        vm_map_global.update(vm_map)

        lines.append("### %s" % cluster.name)
        lines.append("")
        if cluster.description:
            lines.append("> %s" % cluster.description)
            lines.append("")

        lines.append("**File:** `%s`" % scenario_path)
        lines.append("")

        # Cluster info
        lines.append("#### Cluster")
        lines.append("")
        lines.append("| Node | CPU | RAM | Maintenance |")
        lines.append("|------|-----|-----|-------------|")
        for node in cluster.nodes:
            maint = "\u26a0\ufe0f yes" if node.maintenance else "no"
            lines.append(
                "| %s | %d cores | %.0f GB | %s |"
                % (node.name, node.cpu_total,
                   node.memory_total / _GB, maint)
            )
        lines.append("")

        lines.append("| VM | CPU | RAM | Initial Node |")
        lines.append("|----|-----|-----|--------------|")
        for vm in cluster.vms:
            lines.append(
                "| %s | %d | %.0f GB | %s |"
                % (vm.name, vm.cpu, vm.memory / _GB, vm.node)
            )
        lines.append("")

        # Constraints
        cons = cluster.constraints
        has_constraints = (
            cons.affinity or cons.anti_affinity
            or cons.pin or cons.ignore
        )
        if has_constraints:
            lines.append("#### Constraints")
            lines.append("")
            if cons.affinity:
                for rule in cons.affinity:
                    lines.append(
                        "- **Affinity** `%s`: %s"
                        % (rule["name"], ", ".join(rule["vms"]))
                    )
            if cons.anti_affinity:
                for rule in cons.anti_affinity:
                    lines.append(
                        "- **Anti-Affinity** `%s`: %s"
                        % (rule["name"], ", ".join(rule["vms"]))
                    )
            if cons.pin:
                for rule in cons.pin:
                    lines.append(
                        "- **Pin** `%s` \u2192 %s"
                        % (rule["vm"], ", ".join(rule["nodes"]))
                    )
            if cons.ignore:
                lines.append(
                    "- **Ignore**: %s" % ", ".join(cons.ignore)
                )
            lines.append("")

        # Solver result
        lines.append("#### Solver Result")
        lines.append("")

        if not solution.feasible:
            lines.append(
                "\u274c **INFEASIBLE** \u2014 Status: `%s`"
                % solution.stats.status
            )
            lines.append("")
            if solution.blocking_vms:
                lines.append("**Blocking VMs** (preventing evacuation):")
                lines.append("")
                for bv in solution.blocking_vms:
                    vm = vm_map.get(bv)
                    if vm:
                        lines.append(
                            "- `%s` \u2014 %d CPU, %.0f GB RAM, "
                            "currently on `%s`"
                            % (bv, vm.cpu, vm.memory / _GB, vm.node)
                        )
                    else:
                        lines.append("- `%s`" % bv)
                lines.append("")
                # Show why each VM is blocked
                pin_map = {}
                for rule in cluster.constraints.pin:
                    pin_map[rule["vm"]] = rule["nodes"]
                has_reasons = False
                for bv in solution.blocking_vms:
                    reasons = []
                    if bv in pin_map:
                        reasons.append(
                            "pinned to %s" % ", ".join(pin_map[bv])
                        )
                    if bv in cluster.constraints.ignore:
                        reasons.append("ignored (must stay on current node)")
                    if reasons:
                        has_reasons = True
                        lines.append(
                            "- `%s`: %s" % (bv, "; ".join(reasons))
                        )
                if has_reasons:
                    lines.append("")
                if not has_reasons and len(solution.blocking_vms) > 1:
                    lines.append(
                        "*Aggregate capacity insufficient "
                        "on remaining nodes.*"
                    )
                    lines.append("")
        else:
            # Balanciness info
            bal = cluster.balancing
            level = max(1, min(5, bal.balanciness))
            level_name = _BALANCINESS_NAMES.get(level, "?")
            init_gap = _initial_load_gap(cluster)
            eff_wb, eff_ws = _resolve_balancing(bal, init_gap)
            _, _, threshold = _BALANCINESS_PROFILES[level]

            lines.append(
                "| Metric | Value |"
            )
            lines.append("|--------|-------|")
            lines.append(
                "| Balanciness | %d (%s) |" % (level, level_name)
            )
            lines.append(
                "| Effective weights | w_balance=%d, w_stickiness=%d |"
                % (eff_wb, eff_ws)
            )
            if threshold > 0 and init_gap < threshold:
                lines.append(
                    "| Threshold | %.0f%% — *gap %.0f%% below, "
                    "balancing skipped* |"
                    % (threshold * 100, init_gap * 100)
                )
            lines.append(
                "| Status | `%s` |" % solution.stats.status
            )
            lines.append(
                "| Objective | %s |"
                % str(int(solution.stats.objective))
            )
            lines.append(
                "| Load gap | %.1f%% |"
                % (solution.stats.load_gap * 100)
            )
            lines.append(
                "| Migrations | %d |"
                % solution.stats.migration_count
            )
            lines.append(
                "| Solve time | %.1f ms |"
                % solution.stats.wall_time_ms
            )
            lines.append("")

            # Node utilization before/after
            lines.append("#### Node Utilization")
            lines.append("")
            lines.append(
                "| Node | Before | | After | |"
            )
            lines.append(
                "|------|--------|-|-------|-|"
            )
            for node in cluster.nodes:
                before_used = sum(
                    v.memory for v in cluster.vms
                    if v.node == node.name
                )
                before_pct = (
                    before_used / node.memory_total * 100
                    if node.memory_total else 0
                )
                after_used = sum(
                    vm_map[vn].memory
                    for vn, tgt in solution.placements.items()
                    if tgt == node.name
                )
                after_pct = (
                    after_used / node.memory_total * 100
                    if node.memory_total else 0
                )
                maint = " \u26a0\ufe0f" if node.maintenance else ""
                lines.append(
                    "| %s%s | %.0f GB (%d%%) | `%s` | %.0f GB (%d%%) | `%s` |"
                    % (
                        node.name, maint,
                        before_used / _GB, before_pct,
                        _md_bar(before_pct, 15),
                        after_used / _GB, after_pct,
                        _md_bar(after_pct, 15),
                    )
                )
            lines.append("")

            # Migrations
            if solution.migrations:
                lines.append("#### Migrations")
                lines.append("")
                lines.append("| # | VM | From | To | RAM |")
                lines.append("|---|-----|------|----|-----|")
                for idx, mig in enumerate(solution.migrations, 1):
                    vm = vm_map[mig.vm]
                    lines.append(
                        "| %d | %s | %s | %s | %.0f GB |"
                        % (idx, mig.vm, mig.source, mig.target,
                           vm.memory / _GB)
                    )
                lines.append("")
            else:
                lines.append(
                    "*No migrations needed.*"
                )
                lines.append("")

            # Migration plan (step-based)
            if migration_plans and scenario_path in migration_plans:
                _render_migration_plan(
                    lines, cluster, migration_plans[scenario_path]
                )

            # VM placement table
            lines.append("#### VM Placements")
            lines.append("")
            lines.append("| VM | Before | After | Moved |")
            lines.append("|----|--------|-------|-------|")
            for vm in cluster.vms:
                after = solution.placements.get(vm.name, "?")
                moved = "\u2714\ufe0f" if after != vm.node else ""
                lines.append(
                    "| %s | %s | %s | %s |"
                    % (vm.name, vm.node, after, moved)
                )
            lines.append("")

        # Expectations check
        checks = _check_expectations(cluster, solution)
        lines.append("#### Expectations")
        lines.append("")
        lines.append("| Check | Expected | Result | Detail |")
        lines.append("|-------|----------|--------|--------|")
        for check_name, expected, passed_flag, detail in checks:
            icon = "\u2705" if passed_flag else "\u274c"
            lines.append(
                "| %s | %s | %s | %s |"
                % (check_name, expected, icon, detail)
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── HTML Report ──

def _slug(name: str) -> str:
    """Turn a scenario name into a URL-safe anchor slug."""
    return name.lower().replace(" ", "-").replace("—", "-")


def _html_bar(pct: float, width_px: int = 120) -> str:
    """Render a CSS progress bar."""
    if pct > 90:
        color = "#e74c3c"
    elif pct > 70:
        color = "#f39c12"
    else:
        color = "#27ae60"
    return (
        '<div class="bar" style="width:%dpx">'
        '<div class="bar-fill" style="width:%.0f%%;background:%s"></div>'
        '</div>' % (width_px, min(pct, 100), color)
    )


def _html_pct(used: int, total: int) -> str:
    pct = used / total * 100 if total else 0
    return "%.0f GB (%d%%)" % (used / _GB, pct)


def write_html_report(
    results: list[tuple[str, Cluster, Solution]],
    path: str | Path,
    migration_plans: dict[str, MigrationPlan] | None = None,
) -> None:
    """Write a self-contained HTML report with navigation."""
    path = Path(path)
    h: list[str] = []

    # Pre-compute checks
    all_checks: list[tuple[str, str, bool, str]] = []
    scenario_checks: list[tuple[str, list[tuple[str, str, bool, str]]]] = []
    for _, cluster, solution in results:
        checks = _check_expectations(cluster, solution)
        all_checks.extend(checks)
        scenario_checks.append((cluster.name, checks))

    passed_total = sum(1 for _, _, p, _ in all_checks if p)
    failed_total = sum(1 for _, _, p, _ in all_checks if not p)
    scenarios_passed = sum(
        1 for _, checks in scenario_checks
        if all(p for _, _, p, _ in checks)
    )
    total = len(results)

    h.append("<!DOCTYPE html>")
    h.append('<html lang="en">')
    h.append("<head>")
    h.append('<meta charset="utf-8">')
    h.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    h.append("<title>ProxLB CP-SAT Solver — Report</title>")
    h.append('<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js">'
             "</script>")
    h.append("<script>mermaid.initialize({startOnLoad:true});</script>")
    h.append("<style>")
    h.append("""
:root{--bg:#f8f9fa;--sidebar:#1e293b;--sidebar-w:320px;--accent:#3b82f6;
--pass:#22c55e;--fail:#ef4444;--warn:#f59e0b;--border:#e2e8f0;--text:#1e293b;
--card:#fff;--code-bg:#f1f5f9}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.5}
nav{position:fixed;top:0;left:0;width:var(--sidebar-w);height:100vh;
background:var(--sidebar);color:#cbd5e1;overflow-y:auto;padding:16px 0;
z-index:100;font-size:13px}
nav .brand{color:#fff;font-weight:700;font-size:15px;padding:0 16px 12px;
border-bottom:1px solid #334155}
nav ul{list-style:none;padding:8px 0}
nav li{padding:0}
nav a{display:block;color:#94a3b8;text-decoration:none;padding:5px 16px;
transition:background .15s,color .15s;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
nav a:hover,nav a.active{background:#334155;color:#fff}
nav a.section{color:#64748b;font-size:11px;text-transform:uppercase;
letter-spacing:.05em;padding-top:12px;pointer-events:none}
nav .badge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;
margin-left:4px}
nav .badge-pass{background:#166534;color:#bbf7d0}
nav .badge-fail{background:#991b1b;color:#fecaca}
main{margin-left:var(--sidebar-w);padding:24px 32px 64px}
h1{font-size:24px;margin-bottom:4px}
h2{font-size:20px;margin:32px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--border)}
h3{font-size:17px;margin:24px 0 8px}
h4{font-size:14px;margin:16px 0 6px;color:#475569}
.meta{color:#64748b;font-size:13px;margin-bottom:24px}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:12px;margin-bottom:24px}
.summary-card{background:var(--card);border:1px solid var(--border);border-radius:8px;
padding:16px;text-align:center}
.summary-card .value{font-size:28px;font-weight:700}
.summary-card .label{font-size:12px;color:#64748b;text-transform:uppercase;
letter-spacing:.04em}
.pass{color:var(--pass)}.fail{color:var(--fail)}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:13px}
th,td{padding:6px 10px;border:1px solid var(--border);text-align:left}
th{background:#f1f5f9;font-weight:600;position:sticky;top:0}
tr:hover{background:#f8fafc}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;
padding:20px;margin:16px 0}
.bar{background:#e2e8f0;border-radius:4px;height:14px;display:inline-block;
vertical-align:middle}
.bar-fill{height:100%;border-radius:4px;transition:width .3s}
.badge-inline{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;
font-weight:600}
.badge-ok{background:#dcfce7;color:#166534}
.badge-err{background:#fee2e2;color:#991b1b}
.badge-info{background:#dbeafe;color:#1e40af}
.badge-warn{background:#fef3c7;color:#92400e}
.step-block{border-left:3px solid var(--accent);padding:8px 16px;margin:8px 0;
background:#f8fafc;border-radius:0 6px 6px 0}
.step-block.temp{border-left-color:var(--warn)}
.step-title{font-weight:600;font-size:14px;margin-bottom:4px}
.step-mig{font-size:13px;color:#334155;padding:2px 0}
.step-mig code{background:var(--code-bg);padding:1px 5px;border-radius:3px;
font-size:12px}
.mermaid{margin:12px 0}
a.anchor{color:var(--accent);text-decoration:none}
a.anchor:hover{text-decoration:underline}
.constraint-list{list-style:none;padding:0;margin:4px 0}
.constraint-list li{padding:3px 0;font-size:13px}
.constraint-list li::before{content:"\\2022";color:var(--accent);margin-right:6px}
@media(max-width:800px){nav{display:none}main{margin-left:0}}
""")
    h.append("</style>")
    h.append("</head>")
    h.append("<body>")

    # ── Sidebar ──
    h.append("<nav>")
    h.append('<div class="brand">ProxLB Solver</div>')
    h.append("<ul>")
    h.append('<li><a href="#summary">Summary</a></li>')
    h.append('<li><a href="#overview">Scenario Overview</a></li>')
    h.append('<li><a class="section">Scenarios</a></li>')
    for _, cluster, solution in results:
        slug = _slug(cluster.name)
        checks = _check_expectations(cluster, solution)
        ok = all(p for _, _, p, _ in checks)
        badge_cls = "badge-pass" if ok else "badge-fail"
        badge_txt = "PASS" if ok else "FAIL"
        h.append(
            '<li><a href="#%s">%s<span class="badge %s">%s</span></a></li>'
            % (slug, cluster.name, badge_cls, badge_txt)
        )
    h.append("</ul>")
    h.append("</nav>")

    # ── Main ──
    h.append("<main>")
    h.append('<h1>ProxLB CP-SAT Solver &mdash; Test Report</h1>')
    h.append(
        '<p class="meta">Generated: %s</p>'
        % datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    # Summary cards
    h.append('<div id="summary">')
    h.append('<h2>Summary</h2>')
    h.append('<div class="summary-grid">')
    if failed_total == 0:
        status_cls = "pass"
        status_txt = "ALL PASSED"
    else:
        status_cls = "fail"
        status_txt = "%d FAILED" % failed_total
    h.append(
        '<div class="summary-card"><div class="value %s">%s</div>'
        '<div class="label">Status</div></div>' % (status_cls, status_txt)
    )
    h.append(
        '<div class="summary-card"><div class="value">%d / %d</div>'
        '<div class="label">Scenarios Passed</div></div>'
        % (scenarios_passed, total)
    )
    h.append(
        '<div class="summary-card"><div class="value">%d</div>'
        '<div class="label">Checks Passed</div></div>' % passed_total
    )
    h.append(
        '<div class="summary-card"><div class="value %s">%d</div>'
        '<div class="label">Checks Failed</div></div>'
        % ("fail" if failed_total else "pass", failed_total)
    )
    h.append("</div>")  # summary-grid
    h.append("</div>")  # summary

    # Overview table
    h.append('<div id="overview">')
    h.append("<h2>Scenario Overview</h2>")
    h.append("<table>")
    h.append(
        "<tr><th>Scenario</th><th>Feasible</th><th>Migrations</th>"
        "<th>Load Gap</th><th>Steps</th><th>Result</th></tr>"
    )
    for scenario_path, cluster, solution in results:
        slug = _slug(cluster.name)
        checks = _check_expectations(cluster, solution)
        ok = all(p for _, _, p, _ in checks)
        badge = (
            '<span class="badge-inline badge-ok">PASS</span>' if ok
            else '<span class="badge-inline badge-err">FAIL</span>'
        )
        plan = migration_plans.get(scenario_path) if migration_plans else None
        steps_str = str(len(plan.steps)) if plan else "&mdash;"
        if solution.feasible:
            h.append(
                "<tr><td><a class=\"anchor\" href=\"#%s\">%s</a></td>"
                "<td>yes</td><td>%d</td><td>%.1f%%</td><td>%s</td><td>%s</td></tr>"
                % (slug, cluster.name, solution.stats.migration_count,
                   solution.stats.load_gap * 100, steps_str, badge)
            )
        else:
            h.append(
                "<tr><td><a class=\"anchor\" href=\"#%s\">%s</a></td>"
                "<td>no</td><td>&mdash;</td><td>&mdash;</td>"
                "<td>&mdash;</td><td>%s</td></tr>"
                % (slug, cluster.name, badge)
            )
    h.append("</table>")
    h.append("</div>")

    # ── Detailed scenario sections ──
    h.append("<h2>Detailed Results</h2>")

    for scenario_path, cluster, solution in results:
        slug = _slug(cluster.name)
        vm_map = {v.name: v for v in cluster.vms}
        node_map = {n.name: n for n in cluster.nodes}

        h.append('<div class="card" id="%s">' % slug)
        h.append("<h3>%s</h3>" % cluster.name)
        if cluster.description:
            h.append("<p><em>%s</em></p>" % cluster.description)
        h.append(
            '<p style="font-size:12px;color:#64748b">File: <code>%s</code></p>'
            % scenario_path
        )

        # Cluster info
        h.append("<h4>Cluster</h4>")
        h.append("<table><tr><th>Node</th><th>CPU</th><th>RAM</th>"
                 "<th>Maintenance</th></tr>")
        for node in cluster.nodes:
            maint = '<span class="badge-inline badge-warn">yes</span>' \
                if node.maintenance else "no"
            h.append(
                "<tr><td>%s</td><td>%d cores</td><td>%.0f GB</td><td>%s</td></tr>"
                % (node.name, node.cpu_total, node.memory_total / _GB, maint)
            )
        h.append("</table>")

        h.append("<table><tr><th>VM</th><th>CPU</th><th>RAM</th>"
                 "<th>Initial Node</th></tr>")
        for vm in cluster.vms:
            h.append(
                "<tr><td>%s</td><td>%d</td><td>%.0f GB</td><td>%s</td></tr>"
                % (vm.name, vm.cpu, vm.memory / _GB, vm.node)
            )
        h.append("</table>")

        # Constraints
        cons = cluster.constraints
        has_constraints = (
            cons.affinity or cons.anti_affinity or cons.pin or cons.ignore
        )
        if has_constraints:
            h.append("<h4>Constraints</h4>")
            h.append('<ul class="constraint-list">')
            for rule in cons.affinity:
                h.append(
                    "<li><strong>Affinity</strong> <code>%s</code>: %s</li>"
                    % (rule["name"], ", ".join(rule["vms"]))
                )
            for rule in cons.anti_affinity:
                h.append(
                    "<li><strong>Anti-Affinity</strong> <code>%s</code>: %s</li>"
                    % (rule["name"], ", ".join(rule["vms"]))
                )
            for rule in cons.pin:
                h.append(
                    "<li><strong>Pin</strong> <code>%s</code> &rarr; %s</li>"
                    % (rule["vm"], ", ".join(rule["nodes"]))
                )
            if cons.ignore:
                h.append(
                    "<li><strong>Ignore</strong>: %s</li>"
                    % ", ".join(cons.ignore)
                )
            h.append("</ul>")

        # Solver result
        h.append("<h4>Solver Result</h4>")

        if not solution.feasible:
            h.append(
                '<p><span class="badge-inline badge-err">INFEASIBLE</span>'
                " &mdash; Status: <code>%s</code></p>"
                % solution.stats.status
            )
            if solution.blocking_vms:
                h.append("<p><strong>Blocking VMs:</strong></p><ul>")
                for bv in solution.blocking_vms:
                    vm = vm_map.get(bv)
                    if vm:
                        h.append(
                            "<li><code>%s</code> &mdash; %d CPU, %.0f GB, on %s</li>"
                            % (bv, vm.cpu, vm.memory / _GB, vm.node)
                        )
                h.append("</ul>")
        else:
            bal = cluster.balancing
            level = max(1, min(5, bal.balanciness))
            level_name = _BALANCINESS_NAMES.get(level, "?")
            init_gap = _initial_load_gap(cluster)
            eff_wb, eff_ws = _resolve_balancing(bal, init_gap)

            h.append("<table>")
            h.append("<tr><th>Metric</th><th>Value</th></tr>")
            h.append(
                "<tr><td>Balanciness</td><td>%d (%s)</td></tr>" % (level, level_name)
            )
            h.append(
                "<tr><td>Weights</td><td>w_balance=%d, w_stickiness=%d</td></tr>"
                % (eff_wb, eff_ws)
            )
            h.append(
                "<tr><td>Status</td><td><code>%s</code></td></tr>"
                % solution.stats.status
            )
            h.append(
                "<tr><td>Objective</td><td>%d</td></tr>"
                % int(solution.stats.objective)
            )
            h.append(
                "<tr><td>Load gap</td><td>%.1f%%</td></tr>"
                % (solution.stats.load_gap * 100)
            )
            h.append(
                "<tr><td>Migrations</td><td>%d</td></tr>"
                % solution.stats.migration_count
            )
            h.append(
                "<tr><td>Solve time</td><td>%.1f ms</td></tr>"
                % solution.stats.wall_time_ms
            )
            h.append("</table>")

            # Node utilization
            h.append("<h4>Node Utilization</h4>")
            h.append("<table><tr><th>Node</th>"
                     "<th>Before</th><th></th>"
                     "<th>After</th><th></th></tr>")
            for node in cluster.nodes:
                before_used = sum(
                    v.memory for v in cluster.vms if v.node == node.name
                )
                before_pct = (
                    before_used / node.memory_total * 100
                    if node.memory_total else 0
                )
                after_used = sum(
                    vm_map[vn].memory
                    for vn, tgt in solution.placements.items()
                    if tgt == node.name
                )
                after_pct = (
                    after_used / node.memory_total * 100
                    if node.memory_total else 0
                )
                maint = " &#9888;" if node.maintenance else ""
                h.append(
                    "<tr><td>%s%s</td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td>%s</td></tr>"
                    % (
                        node.name, maint,
                        _html_pct(before_used, node.memory_total),
                        _html_bar(before_pct),
                        _html_pct(after_used, node.memory_total),
                        _html_bar(after_pct),
                    )
                )
            h.append("</table>")

            # Migration plan
            plan = migration_plans.get(scenario_path) if migration_plans else None
            if plan and plan.steps:
                h.append("<h4>Migration Plan</h4>")

                # Mermaid dependency graph
                mig_map_html = {}
                for step in plan.steps:
                    for m in step.migrations:
                        mig_map_html[m.vm] = m

                if plan.dependency_edges:
                    h.append("<h4>Dependency Graph</h4>")
                    h.append('<div class="mermaid">')
                    h.append("graph LR")
                    rendered = set()
                    for vm_a, vm_b in plan.dependency_edges:
                        for vn in (vm_a, vm_b):
                            if vn not in rendered:
                                m = mig_map_html.get(vn)
                                if m:
                                    h.append(
                                        '    %s["%s: %s &rarr; %s"]'
                                        % (vn, vn, m.source, m.target)
                                    )
                                rendered.add(vn)
                        h.append("    %s --> %s" % (vm_b, vm_a))
                    h.append("</div>")

                if plan.temp_moves:
                    h.append(
                        '<p><span class="badge-inline badge-warn">Temp moves</span> %s</p>'
                        % ", ".join("<code>%s</code>" % vm for vm in plan.temp_moves)
                    )

                # Execution steps
                h.append("<h4>Execution Steps</h4>")
                for step in plan.steps:
                    is_temp_step = any(
                        m.vm in plan.temp_moves
                        and m.target != mig_map_html.get(m.vm, m).target
                        for m in step.migrations
                    )
                    cls = "step-block temp" if is_temp_step else "step-block"
                    par_label = " (parallel)" if step.parallel else ""
                    h.append('<div class="%s">' % cls)
                    h.append(
                        '<div class="step-title">Step %d%s</div>'
                        % (step.step, par_label)
                    )
                    for m in step.migrations:
                        vm = vm_map.get(m.vm)
                        ram = "%.0f GB" % (vm.memory / _GB) if vm else "?"
                        suffix = ""
                        if m.vm in plan.temp_moves:
                            final_target = None
                            for s2 in plan.steps:
                                for m2 in s2.migrations:
                                    if m2.vm == m.vm:
                                        final_target = m2.target
                            if m.target != final_target:
                                suffix = (
                                    ' <span class="badge-inline badge-warn">'
                                    "temp</span>"
                                )
                        h.append(
                            '<div class="step-mig"><code>%s</code> '
                            "%s &rarr; %s &nbsp;(%s)%s</div>"
                            % (m.vm, m.source, m.target, ram, suffix)
                        )
                    h.append("</div>")

                # Cluster state per step
                h.append("<h4>Cluster State per Step</h4>")
                active_nodes = [n for n in cluster.nodes if not n.maintenance]
                h.append("<table><tr><th>Step</th>")
                for n in active_nodes:
                    h.append("<th>%s</th>" % n.name)
                h.append("</tr>")

                current_used_h: dict[str, int] = defaultdict(int)
                for vm in cluster.vms:
                    current_used_h[vm.node] += vm.memory

                # Initial
                h.append("<tr><td>Initial</td>")
                for n in active_nodes:
                    used = current_used_h[n.name]
                    pct = used / n.memory_total * 100 if n.memory_total else 0
                    h.append(
                        "<td>%s %s</td>"
                        % (_html_pct(used, n.memory_total), _html_bar(pct, 80))
                    )
                h.append("</tr>")

                for step in plan.steps:
                    for m in step.migrations:
                        vm = vm_map.get(m.vm)
                        if vm:
                            current_used_h[m.source] -= vm.memory
                            current_used_h[m.target] += vm.memory
                    h.append("<tr><td>Step %d</td>" % step.step)
                    for n in active_nodes:
                        used = current_used_h[n.name]
                        pct = used / n.memory_total * 100 if n.memory_total else 0
                        h.append(
                            "<td>%s %s</td>"
                            % (_html_pct(used, n.memory_total), _html_bar(pct, 80))
                        )
                    h.append("</tr>")
                h.append("</table>")

            elif solution.migrations:
                # No plan steps but has migrations (flat list)
                h.append("<h4>Migrations</h4>")
                h.append("<table><tr><th>#</th><th>VM</th><th>From</th>"
                         "<th>To</th><th>RAM</th></tr>")
                for idx, mig in enumerate(solution.migrations, 1):
                    vm = vm_map[mig.vm]
                    h.append(
                        "<tr><td>%d</td><td>%s</td><td>%s</td>"
                        "<td>%s</td><td>%.0f GB</td></tr>"
                        % (idx, mig.vm, mig.source, mig.target,
                           vm.memory / _GB)
                    )
                h.append("</table>")
            else:
                h.append("<p><em>No migrations needed.</em></p>")

            # VM placements
            h.append("<h4>VM Placements</h4>")
            h.append("<table><tr><th>VM</th><th>Before</th>"
                     "<th>After</th><th>Moved</th></tr>")
            for vm in cluster.vms:
                after = solution.placements.get(vm.name, "?")
                moved = "&#10004;" if after != vm.node else ""
                h.append(
                    "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (vm.name, vm.node, after, moved)
                )
            h.append("</table>")

        # Expectations
        checks = _check_expectations(cluster, solution)
        h.append("<h4>Expectations</h4>")
        h.append("<table><tr><th>Check</th><th>Expected</th>"
                 "<th>Result</th><th>Detail</th></tr>")
        for check_name, expected, passed_flag, detail in checks:
            badge = (
                '<span class="badge-inline badge-ok">PASS</span>' if passed_flag
                else '<span class="badge-inline badge-err">FAIL</span>'
            )
            h.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (check_name, expected, badge, detail)
            )
        h.append("</table>")

        h.append("</div>")  # card

    h.append("</main>")
    h.append("</body>")
    h.append("</html>")

    path.write_text("\n".join(h), encoding="utf-8")
