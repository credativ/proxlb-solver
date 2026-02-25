"""Migration planner — orders migrations respecting dependencies and ops-safety."""

from __future__ import annotations

from collections import defaultdict

from .models import Cluster, Migration, MigrationPlan, MigrationStep, Solution


def _get_affinity_group(vm_name: str, cluster: Cluster) -> set[str]:
    """Find all VMs belonging to the same affinity group(s) as vm_name."""
    group = {vm_name}
    changed = True
    while changed:
        changed = False
        for rule in cluster.constraints.affinity:
            vms_in_rule = set(rule["vms"])
            if group & vms_in_rule:
                new_members = vms_in_rule - group
                if new_members:
                    group.update(new_members)
                    changed = True
    return group


def _allowed_temp_nodes(
    vm_group: set[str],
    cluster: Cluster,
    solution: Solution,
    exclude: set[str],
    node_used_mem: dict[str, int],
    node_used_cpu: dict[str, int],
) -> list[str]:
    """Return nodes where the entire *vm_group* may temporarily reside."""
    cons = cluster.constraints
    node_map = {n.name: n for n in cluster.nodes}
    vms_in_group = [v for v in cluster.vms if v.name in vm_group]

    if any(v.name in cons.ignore for v in vms_in_group):
        return []

    candidates = {n.name for n in cluster.nodes if not n.maintenance}
    candidates -= exclude
    if cluster.evacuate_node:
        candidates.discard(cluster.evacuate_node)

    for vm_name in vm_group:
        for rule in cons.pin:
            if rule["vm"] == vm_name:
                candidates &= set(rule["nodes"])

    current_node_vms: dict[str, set[str]] = defaultdict(set)
    for v in cluster.vms:
        current_node_vms[v.node].add(v.name)

    for vm_name in vm_group:
        for rule in cons.anti_affinity:
            if vm_name in rule["vms"]:
                partners = set(rule["vms"]) - vm_group
                for node_name in list(candidates):
                    if partners & current_node_vms.get(node_name, set()):
                        candidates.discard(node_name)

    group_mem = sum(v.memory for v in vms_in_group)
    for node_name in list(candidates):
        node = node_map[node_name]
        avail = node.memory_total - node_used_mem.get(node_name, 0)
        if avail < group_mem:
            candidates.discard(node_name)

    cpu_overcommit = cluster.balancing.cpu_overcommit
    group_cpu = sum(v.cpu for v in vms_in_group)
    for node_name in list(candidates):
        node = node_map[node_name]
        effective_cpu_1000 = int(node.cpu_total * cpu_overcommit * 1000)
        used_cpu_1000 = node_used_cpu.get(node_name, 0) * 1000
        if used_cpu_1000 + group_cpu * 1000 > effective_cpu_1000:
            candidates.discard(node_name)

    return sorted(candidates)


def plan_migrations(
    cluster: Cluster, solution: Solution,
    max_parallel: int | None = None,
) -> MigrationPlan:
    """Order migrations into executable steps respecting dependencies and ops-safety."""
    if not solution.feasible or not solution.migrations:
        return MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])

    max_parallel = max_parallel or cluster.balancing.max_parallel_migrations
    max_inflow = cluster.balancing.max_node_inflow

    node_mem = {n.name: n.memory_total for n in cluster.nodes}
    node_used = defaultdict(int)
    node_cpu = defaultdict(int)
    vm_by_name = {v.name: v for v in cluster.vms}
    for vm in cluster.vms:
        node_used[vm.node] += vm.memory
        node_cpu[vm.node] += vm.cpu

    mig_map = {m.vm: m for m in solution.migrations}
    node_residents = defaultdict(set)
    for vm in cluster.vms:
        node_residents[vm.node].add(vm.name)

    # Dependency Graph (Capacity based)
    deps = defaultdict(set)
    for vm_name, mig in mig_map.items():
        vm = vm_by_name[vm_name]
        target = mig.target
        avail = node_mem.get(target, 0) - node_used[target]
        if avail >= vm.memory:
            continue
        for resident in node_residents[target]:
            if resident in mig_map and mig_map[resident].source == target:
                deps[vm_name].add(resident)

    # Collect dependency edges for reporting BEFORE they get cleared
    dependency_edges: list[tuple[str, str]] = []
    for vm_a, dep_set in deps.items():
        for vm_b in dep_set:
            dependency_edges.append((vm_a, vm_b))

    # Detect cycles
    cycle_members = set()
    visited, in_stack = set(), set()

    def _find_cycles(vm_name: str):
        if vm_name in visited: return
        if vm_name in in_stack:
            cycle_members.add(vm_name)
            return
        in_stack.add(vm_name)
        for dep in deps.get(vm_name, set()):
            _find_cycles(dep)
        in_stack.discard(vm_name)
        visited.add(vm_name)

    for vm_name in mig_map:
        _find_cycles(vm_name)

    temp_moves, temp_migrations = [], []
    if cycle_members:
        # Full cycle detection: anyone reachable from a cycle member is in a cycle
        all_cycle_vms = set(cycle_members)
        changed = True
        while changed:
            changed = False
            for v, dset in deps.items():
                if v not in all_cycle_vms and (dset & all_cycle_vms):
                    all_cycle_vms.add(v)
                    changed = True

        node_used_work = defaultdict(int, node_used)
        node_cpu_work = defaultdict(int, node_cpu)
        cycle_broken = False
        for vm_name in sorted(all_cycle_vms):
            mig = mig_map[vm_name]
            vm_group = _get_affinity_group(vm_name, cluster)
            allowed = _allowed_temp_nodes(vm_group, cluster, solution, {mig.source, mig.target}, node_used_work, node_cpu_work)
            if allowed:
                temp_node = allowed[0]
                for gvm_name in sorted(vm_group):
                    gvm = vm_by_name[gvm_name]
                    temp_moves.append(gvm_name)
                    temp_migrations.append(Migration(vm=gvm_name, source=gvm.node, target=temp_node))
                    node_used_work[gvm.node] -= gvm.memory
                    node_used_work[temp_node] += gvm.memory
                    node_cpu_work[gvm.node] -= gvm.cpu
                    node_cpu_work[temp_node] += gvm.cpu
                    for other in list(deps.keys()): deps[other].discard(gvm_name)
                    deps.pop(gvm_name, None)
                cycle_broken = True
                break
        if not cycle_broken:
            return MigrationPlan([], dependency_edges, [], False, sorted(all_cycle_vms))

    current_node_used = defaultdict(int, node_used)
    steps = []
    step_num = 1
    
    remaining_migs = []
    for tm in temp_migrations:
        remaining_migs.append((tm.source, tm.target, tm.vm, True))
    for vm_name, m in mig_map.items():
        if vm_name not in temp_moves:
            remaining_migs.append((m.source, m.target, m.vm, False))

    while remaining_migs:
        current_step_migs = []
        inflow_count = defaultdict(int)
        processed_indices = []
        
        for i, (src, dst, vm_name, is_temp) in enumerate(remaining_migs):
            vm = vm_by_name[vm_name]
            if deps.get(vm_name): continue
            
            avail = node_mem.get(dst, 0) - current_node_used[dst]
            if avail < vm.memory: continue
            if max_inflow and inflow_count[dst] >= max_inflow: continue
            if max_parallel and len(current_step_migs) >= max_parallel: break
                
            current_step_migs.append(Migration(vm_name, src, dst))
            inflow_count[dst] += 1
            processed_indices.append(i)
            
        if not current_step_migs:
            # Check if temp-moved VMs can now move to their final target
            for vm_name in temp_moves:
                if any(m.vm == vm_name for s in steps for m in s.migrations): continue
                # The temp-moved VM might not be in remaining_migs yet if it just finished step 1
                pass
            break

        steps.append(MigrationStep(step_num, current_step_migs, len(current_step_migs) > 1))
        step_num += 1
        
        for m in sorted(processed_indices, reverse=True):
            src, dst, vn, is_temp = remaining_migs.pop(m)
            current_node_used[src] -= vm_by_name[vn].memory
            current_node_used[dst] += vm_by_name[vn].memory
            for other in list(deps.keys()): deps[other].discard(vn)
            if is_temp:
                final_mig = mig_map[vn]
                remaining_migs.append((dst, final_mig.target, vn, False))

    return MigrationPlan(steps, dependency_edges, temp_moves)
