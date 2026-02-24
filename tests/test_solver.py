"""Parametrized solver tests over all YAML scenarios."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from proxlb_solver.loader import load_scenario
from proxlb_solver.planner import plan_migrations
from proxlb_solver.solver import solve
from proxlb_solver.reporter import _compute_load_gap, _initial_load_gap


def test_scenario(scenario_path: Path):
    """Test a single YAML scenario against its expect block."""
    cluster = load_scenario(scenario_path)
    solution = solve(cluster)
    expect = cluster.expect

    errors = []

    # Check feasibility
    if expect.feasible and not solution.feasible:
        errors.append(
            f"Expected feasible but got {solution.stats.status}"
        )
    if not expect.feasible and solution.feasible:
        errors.append("Expected infeasible but got a solution")

    if not solution.feasible:
        if expect.feasible:
            raise AssertionError("\n".join(errors))
        return

    # Check constraints satisfied
    if expect.constraints_satisfied:
        # Verify anti-affinity
        for rule in cluster.constraints.anti_affinity:
            if not rule.get("hard", True): continue
            vm_nodes = [
                solution.placements.get(v)
                for v in rule["vms"]
                if v in solution.placements
            ]
            if len(vm_nodes) != len(set(vm_nodes)):
                errors.append(
                    f"Anti-affinity violated for {rule['name']}"
                )

        # Verify affinity
        for rule in cluster.constraints.affinity:
            if not rule.get("hard", True): continue
            nodes_used = {
                solution.placements.get(v)
                for v in rule["vms"]
                if v in solution.placements
            }
            if len(nodes_used) > 1:
                errors.append(
                    f"Affinity violated for {rule['name']}: "
                    f"placed on {nodes_used}"
                )

        # Verify pinning
        for rule in cluster.constraints.pin:
            vm_name = rule["vm"]
            if vm_name in solution.placements:
                placed = solution.placements[vm_name]
                if placed not in rule["nodes"]:
                    errors.append(
                        f"Pin violated: {vm_name} on {placed}, "
                        f"expected one of {rule['nodes']}"
                    )

        # Verify maintenance
        for node in cluster.nodes:
            if node.maintenance:
                for vm_name, placed in solution.placements.items():
                    if placed == node.name:
                        errors.append(
                            f"VM {vm_name} placed on maintenance "
                            f"node {node.name}"
                        )

        # Verify ignore
        for vm_name in cluster.constraints.ignore:
            vm = next(
                (v for v in cluster.vms if v.name == vm_name), None
            )
            if vm and solution.placements.get(vm_name) != vm.node:
                errors.append(
                    f"Ignored VM {vm_name} was moved from {vm.node}"
                )

        # Verify RAM capacity
        node_map = {n.name: n for n in cluster.nodes}
        node_used: dict[str, int] = defaultdict(int)
        for vm_name, target in solution.placements.items():
            vm = next(v for v in cluster.vms if v.name == vm_name)
            node_used[target] += vm.memory
        for node_name, used in node_used.items():
            node = node_map[node_name]
            cap = node.memory_total - node.memory_reserve
            if used > cap:
                errors.append(
                    f"RAM overflow on {node_name}: "
                    f"{used} > {cap} (Total: {node.memory_total}, Res: {node.memory_reserve})"
                )
        
        # Verify storage capacity
        storage_usage = defaultdict(lambda: defaultdict(int)) # node -> {storage -> bytes}
        for vm_name, target in solution.placements.items():
            vm = next(v for v in cluster.vms if v.name == vm_name)
            for sname, sbytes in vm.disks.items():
                storage_usage[target][sname] += sbytes
        
        for node_name, usages in storage_usage.items():
            node = node_map[node_name]
            for sname, sbytes in usages.items():
                # cap = Free - Reserved
                cap = node.storage_free.get(sname, 0) - node.storage_reserve.get(sname, 0)
                if sbytes > cap:
                    errors.append(
                        f"Storage '{sname}' overflow on {node_name}: "
                        f"{sbytes} > {cap}"
                    )
        
        # Verify CPU capacity
        cpu_usage_vcpus = defaultdict(int)
        for vm_name, target in solution.placements.items():
            vm = next(v for v in cluster.vms if v.name == vm_name)
            cpu_usage_vcpus[target] += vm.cpu
        
        for node_name, used_vcpus in cpu_usage_vcpus.items():
            node = node_map[node_name]
            # usable_cores = (total - reserve) * overcommit
            usable = max(0, node.cpu_total - node.cpu_reserve)
            limit = int(usable * cluster.balancing.cpu_overcommit)
            if used_vcpus > limit:
                errors.append(
                    f"vCPU overflow on {node_name}: {used_vcpus} > {limit}"
                )

    # Check spread improved
    if expect.spread_improved is True:
        initial_gap = _initial_load_gap(cluster)
        new_gap = _compute_load_gap(cluster, solution.placements)
        if new_gap >= initial_gap and initial_gap > 0:
            errors.append(
                f"Spread not improved: {initial_gap:.3f} -> {new_gap:.3f}"
            )

    # Check max migrations
    if expect.max_migrations is not None:
        if solution.stats.migration_count > expect.max_migrations:
            errors.append(
                f"Too many migrations: {solution.stats.migration_count} "
                f"> {expect.max_migrations}"
            )

    # Check node_empty
    if expect.node_empty is not None:
        vms_on_node = [
            vm_name for vm_name, placed in solution.placements.items()
            if placed == expect.node_empty
        ]
        if vms_on_node:
            errors.append(
                f"Node {expect.node_empty} not empty, "
                f"still has: {', '.join(vms_on_node)}"
            )

    # Check specific placements
    for vm_name, expected in expect.placements.items():
        actual = solution.placements.get(vm_name)
        if expected.startswith("== "):
            # Relative placement: must be same node as another VM
            ref_vm = expected[3:]
            ref_node = solution.placements.get(ref_vm)
            if actual != ref_node:
                errors.append(
                    f"{vm_name} not colocated with {ref_vm}: "
                    f"{actual} vs {ref_node}"
                )
        else:
            if actual != expected:
                errors.append(
                    f"{vm_name} on {actual}, expected {expected}"
                )

    # Check path_feasible
    if expect.path_feasible is not None:
        mig_plan = plan_migrations(cluster, solution)
        if expect.path_feasible and not mig_plan.path_feasible:
            errors.append(
                f"Expected path_feasible but planner found unbreakable cycle: "
                f"{mig_plan.unbreakable_cycle}"
            )
        if not expect.path_feasible and mig_plan.path_feasible:
            errors.append(
                "Expected path infeasible but planner found a valid path"
            )

    if errors:
        raise AssertionError("\n".join(errors))
