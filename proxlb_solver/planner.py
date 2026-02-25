"""
Migration planner — orders migrations respecting dependencies and ops-safety.

This module determines THE SEQUENCE of migrations. Finding a target state 
is easy (Solver), but getting there safely is hard (Planner).

ALGORITHM:
The planner uses a variation of Kahn's Algorithm (Topological Sort).
1. Build a dependency graph: Migration A depends on B if A needs space 
   on a host that B is currently occupying.
2. Simulate node capacities step-by-step.
3. Handle deadlocks (circular dependencies like A->B, B->A) by inserting 
   temporary moves to a spare node.
"""

from __future__ import annotations
from collections import defaultdict
from .models import Cluster, Migration, MigrationPlan, MigrationStep, Solution


def _get_affinity_group(vm_name: str, cluster: Cluster) -> set[str]:
    """Finds all VMs belonging to the same affinity group(s) as vm_name."""
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
    """Checks where a whole affinity group can temporarily 'park' during a swap."""
    cons = cluster.constraints
    node_map = {n.name: n for n in cluster.nodes}
    vms_in_group = [v for v in cluster.vms if v.name in vm_group]

    # Rule: Any ignored VM in the group prevents the whole group from moving
    if any(v.name in cons.ignore for v in vms_in_group):
        return []

    # Parks are only allowed on nodes that are NOT the current or final destination
    candidates = {n.name for n in cluster.nodes if not n.maintenance}
    candidates -= exclude
    
    if cluster.evacuate_node:
        candidates.discard(cluster.evacuate_node)

    # Respect Hard Constraints even for temp moves
    for vm_name in vm_group:
        for rule in cons.pin:
            if rule["vm"] == vm_name: candidates &= set(rule["nodes"])

    # RAM and CPU capacity check for the whole group
    group_mem = sum(v.memory for v in vms_in_group)
    group_cpu = sum(v.cpu for v in vms_in_group)
    cpu_overcommit = cluster.balancing.cpu_overcommit

    for node_name in list(candidates):
        node = node_map[node_name]
        # Check RAM
        if node.memory_total - node_used_mem.get(node_name, 0) < group_mem:
            candidates.discard(node_name)
            continue
        # Check CPU
        effective_cpu = int(node.cpu_total * cpu_overcommit * 1000)
        if node_used_cpu.get(node_name, 0) * 1000 + group_cpu * 1000 > effective_cpu:
            candidates.discard(node_name)

    return sorted(candidates)


def plan_migrations(
    cluster: Cluster, solution: Solution,
    max_parallel: int | None = None,
) -> MigrationPlan:
    """
    Main planning logic. Transforms a target state into a series of steps.
    """
    if not solution.feasible or not solution.migrations:
        return MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])

    # 1. Gather configuration
    max_parallel = max_parallel or cluster.balancing.max_parallel_migrations
    max_inflow = cluster.balancing.max_node_inflow

    # 2. Virtual State: track current usage as we 'plan' moves
    node_mem = {n.name: n.memory_total for n in cluster.nodes}
    node_used = defaultdict(int)
    node_cpu = defaultdict(int)
    vm_by_name = {v.name: v for v in cluster.vms}
    for vm in cluster.vms:
        node_used[vm.node] += vm.memory
        node_cpu[vm.node] += vm.cpu

    mig_map = {m.vm: m for m in solution.migrations}
    node_residents = defaultdict(set)
    for vm in cluster.vms: node_residents[vm.node].add(vm.name)

    # 3. Build Initial Dependency Graph
    # A migration is blocked if the target node doesn't have room 
    # until someone else leaves.
    deps = defaultdict(set)
    for vm_name, mig in mig_map.items():
        vm = vm_by_name[vm_name]
        avail = node_mem.get(mig.target, 0) - node_used[mig.target]
        if avail >= vm.memory: continue # Direct move possible
        # Node full: wait for every VM that is migrating AWAY from target node
        for resident in node_residents[mig.target]:
            if resident in mig_map and mig_map[resident].source == mig.target:
                deps[vm_name].add(resident)

    dependency_edges = [(a, b) for a in deps for b in deps[a]]

    # 4. Detect Deadlocks (Cycles)
    cycle_members = set()
    visited, in_stack = set(), set()
    def _find_cycles(vm_name: str):
        if vm_name in visited: return
        if vm_name in in_stack:
            cycle_members.add(vm_name); return
        in_stack.add(vm_name)
        for dep in deps.get(vm_name, set()): _find_cycles(dep)
        in_stack.discard(vm_name); visited.add(vm_name)

    for vm_name in mig_map: _find_cycles(vm_name)

    # 5. Break Cycles
    temp_moves, temp_migrations = [], []
    if cycle_members:
        # Full cycle expansion: find all VMs in the deadlock chain
        all_cycle_vms = set(cycle_members)
        changed = True
        while changed:
            changed = False
            for v, dset in deps.items():
                if v not in all_cycle_vms and (dset & all_cycle_vms):
                    all_cycle_vms.add(v); changed = True

        # Try to break the cycle by parking one VM (or its affinity group) elsewhere
        node_used_work, node_cpu_work = defaultdict(int, node_used), defaultdict(int, node_cpu)
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
                    node_used_work[gvm.node] -= gvm.memory; node_used_work[temp_node] += gvm.memory
                    node_cpu_work[gvm.node] -= gvm.cpu; node_cpu_work[temp_node] += gvm.cpu
                    for other in list(deps.keys()): deps[other].discard(gvm_name)
                    deps.pop(gvm_name, None)
                cycle_broken = True; break
        
        if not cycle_broken:
            # Unbreakable cycle: solver state is unreachable
            return MigrationPlan([], dependency_edges, [], False, sorted(all_cycle_vms))

    # 6. Step Generation (Kahn's Algorithm)
    # We maintain current virtual capacity to decide if a migration can start.
    current_node_used = defaultdict(int, node_used)
    steps = []
    step_num = 1
    
    # Queue of migrations to be planned
    remaining_queue = []
    for tm in temp_migrations: remaining_queue.append((tm.source, tm.target, tm.vm, True))
    for vm_name, m in mig_map.items():
        if vm_name not in temp_moves: remaining_queue.append((m.source, m.target, m.vm, False))

    while remaining_queue:
        current_step_migs = []
        inflow_count = defaultdict(int)
        processed_indices = []
        
        for i, (src, dst, vm_name, is_temp) in enumerate(remaining_queue):
            # Check if this VM is still waiting for another VM to leave its target
            if deps.get(vm_name): continue
            
            # Check capacity: Does the target node have room RIGHT NOW?
            avail = node_mem.get(dst, 0) - current_node_used[dst]
            if avail < vm_by_name[vm_name].memory: continue
            
            # Check Safety: Max inflow reached for this node?
            if max_inflow and inflow_count[dst] >= max_inflow: continue
            
            # Check Safety: Global parallel limit reached?
            if max_parallel and len(current_step_migs) >= max_parallel: break
                
            # OK to migrate in this step!
            current_step_migs.append(Migration(vm_name, src, dst))
            inflow_count[dst] += 1
            processed_indices.append(i)
            
        if not current_step_migs: break

        steps.append(MigrationStep(step_num, current_step_migs, len(current_step_migs) > 1))
        step_num += 1
        
        # 7. Update virtual state after each planned step
        for idx in sorted(processed_indices, reverse=True):
            src, dst, vn, is_temp = remaining_queue.pop(idx)
            current_node_used[src] -= vm_by_name[vn].memory; current_node_used[dst] += vm_by_name[vn].memory
            # VM is gone from its source: anyone waiting for it can now move
            for other in list(deps.keys()): deps[other].discard(vn)
            
            if is_temp:
                # If this was a temp move, add the final leg (temp -> target) back to queue
                remaining_queue.append((dst, mig_map[vn].target, vn, False))

    return MigrationPlan(steps, dependency_edges, temp_moves)
