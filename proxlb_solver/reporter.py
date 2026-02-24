"""Result reporting and gap calculations for ProxLB Solver."""

from __future__ import annotations
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Any
from .models import Cluster, Solution, VM, Node, MigrationPlan

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
        m_gap = _compute_single_gap(cluster, placements, "memory_smart") # Wait, compute_single_gap doesn't handle smart
        # We need a recursive or explicit call
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
        for m in solution.migrations:
            m_table.add_row(m.vm, m.source, m.target, str(vm_map[m.vm].priority))
        console.print(m_table)
    else:
        console.print("[green]Cluster is already balanced.[/green]")

    console.print(f"\n  Status: [bold green]{solution.stats.status}[/bold green]")
    console.print(f"  Weighted Gap: {solution.stats.load_gap:.3f}")
    console.print(f"  Migrations: {solution.stats.migration_count}")
    console.print(f"  Solve time: {solution.stats.wall_time_ms:.1f} ms\n")

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

def write_junit_report(path, results):
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
