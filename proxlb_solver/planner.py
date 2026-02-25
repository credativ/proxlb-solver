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


def _get_pve_affinity_group(vm_name: str, cluster: Cluster) -> set[str]:
    """Finds all VMs belonging to the same PVE native affinity group as vm_name."""
    group = {vm_name}
    changed = True
    while changed:
        changed = False
        for rule in cluster.constraints.affinity:
            if rule.get("origin") != "pve":
                continue
            vms_in_rule = set(rule["vms"])
            if group & vms_in_rule:
                new_members = vms_in_rule - group
                if new_members:
                    group.update(new_members)
                    changed = True
    return group


def _get_affinity_group(vm_name: str, cluster: Cluster) -> set[str]:
    """Finds all VMs belonging to the same affinity group(s) as vm_name.
    This includes both PVE and internal rules for the purpose of temp-move grouping.
    """
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
    # 1. Pinning
    for vm_name in vm_group:
        for rule in cons.pin:
            if rule["vm"] == vm_name: candidates &= set(rule["nodes"])

    # 2. Anti-affinity (only HARD rules apply here to prevent deadlocks)
    # Important: We only care about partners that are NOT moving with us.
    current_node_vms: dict[str, set[str]] = defaultdict(set)
    for v in cluster.vms:
        current_node_vms[v.node].add(v.name)

    for vm_name in vm_group:
        for rule in cons.anti_affinity:
            if vm_name in rule["vms"] and rule.get("hard", True):
                # Partners that are NOT part of this current moving group
                external_partners = set(rule["vms"]) - vm_group
                for node_name in list(candidates):
                    # If any external partner is currently on that node, we can't park there
                    if external_partners & current_node_vms.get(node_name, set()):
                        candidates.discard(node_name)

    # 3. RAM and CPU capacity check for the whole group
    group_mem = sum(v.memory for v in vms_in_group)
    group_cpu = sum(v.cpu for v in vms_in_group)
    cpu_overcommit = cluster.balancing.cpu_overcommit

    for node_name in list(candidates):
        node = node_map[node_name]
        if node.memory_total - node_used_mem.get(node_name, 0) < group_mem:
            candidates.discard(node_name); continue
        effective_cpu = int(node.cpu_total * cpu_overcommit * 1000)
        if node_used_cpu.get(node_name, 0) * 1000 + group_cpu * 1000 > effective_cpu:
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
    node_used, node_cpu, vm_by_name = defaultdict(int), defaultdict(int), {v.name: v for v in cluster.vms}
    for vm in cluster.vms:
        node_used[vm.node] += vm.memory; node_cpu[vm.node] += vm.cpu

    # PVE affinity groups must move together.
    # We consolidate their migrations into "super-migrations"
    pve_groups: dict[str, set[str]] = {}
    for vm in cluster.vms:
        if vm.name not in pve_groups:
            group = _get_pve_affinity_group(vm.name, cluster)
            for member in group:
                pve_groups[member] = group

    mig_map = {m.vm: m for m in solution.migrations}
    node_residents = defaultdict(set)
    for vm in cluster.vms: node_residents[vm.node].add(vm.name)

    # Build Dependency Graph
    # 1. Capacity Dependencies: A depends on B if A's target needs B's space.
    # 2. PVE Anti-Affinity: A depends on B if A moves to B's node and they are anti-affine.
    deps = defaultdict(set)
    
    # Pre-calculate PVE anti-affinity partners
    pve_aa_partners = defaultdict(set)
    for rule in cluster.constraints.anti_affinity:
        if rule.get("origin") == "pve" and rule.get("hard", True):
            for v1 in rule["vms"]:
                for v2 in rule["vms"]:
                    if v1 != v2:
                        pve_aa_partners[v1].add(v2)

    for vm_name, mig in mig_map.items():
        vm = vm_by_name[vm_name]
        
        # Who is currently blocking target node?
        for res in node_residents[mig.target]:
            if res == vm_name: continue
            
            # Special Case: PVE Anti-Affinity
            # If res is an anti-affine partner, we can land on its node ONLY IF it migrates AWAY.
            # However, if we move to res's node, and res moves to OUR node, we have a cycle.
            is_pve_aa_block = res in pve_aa_partners.get(vm_name, set())
            is_capacity_block = node_mem.get(mig.target, 0) - node_used[mig.target] < vm.memory
            
            if is_capacity_block or is_pve_aa_block:
                # If res is moving AWAY, we depend on it to clear the spot.
                if res in mig_map and mig_map[res].source == mig.target:
                    # If res is in the same PVE affinity group as vm_name, it's not a dependency
                    if res in pve_groups.get(vm_name, set()):
                        continue
                    deps[vm_name].add(res)

    # Cross-link dependencies for PVE groups: if A depends on B, 
    # and A is in a PVE group with C, then C also "depends" on B (effectively).
    for vm_name in list(mig_map.keys()):
        group = pve_groups.get(vm_name, set())
        if len(group) > 1:
            all_group_deps = set()
            for member in group:
                all_group_deps.update(deps.get(member, set()))
            for member in group:
                if member in mig_map:
                    deps[member] = all_group_deps

    dependency_edges = [(a, b) for a in deps for b in deps[a]]

    # Detect Cycles
    cycle_members, visited, in_stack = set(), set(), set()
    def _find_cycles(v: str):
        if v in visited: return
        if v in in_stack: cycle_members.add(v); return
        in_stack.add(v)
        for d in deps.get(v, set()): _find_cycles(d)
        in_stack.discard(v); visited.add(v)
    for vm_name in mig_map: _find_cycles(vm_name)

    # Break Cycles
    temp_moves, temp_migrations = [], []
    if cycle_members:
        all_cycle = set(cycle_members)
        changed = True
        while changed:
            changed = False
            for v, dset in deps.items():
                if v not in all_cycle and (dset & all_cycle): all_cycle.add(v); changed = True
        
        node_used_work, node_cpu_work = defaultdict(int, node_used), defaultdict(int, node_cpu)
        cycle_broken = False
        for vm_name in sorted(all_cycle):
            mig = mig_map[vm_name]
            # Use affinity group if any, otherwise just the single VM
            vm_group = _get_affinity_group(vm_name, cluster)
            
            # Use current node used/cpu for capacity check
            allowed = _allowed_temp_nodes(vm_group, cluster, solution, {mig.source, mig.target}, node_used_work, node_cpu_work)
            if allowed:
                temp_node = allowed[0]
                for gvm_name in sorted(vm_group):
                    # We must only temp-move VMs that are actually migrating
                    # (though _get_affinity_group usually returns such members in this context)
                    gvm = vm_by_name[gvm_name]
                    temp_moves.append(gvm_name)
                    temp_migrations.append(Migration(gvm_name, gvm.node, temp_node))
                    node_used_work[gvm.node] -= gvm.memory; node_used_work[temp_node] += gvm.memory
                    node_cpu_work[gvm.node] -= gvm.cpu; node_cpu_work[temp_node] += gvm.cpu
                    
                    # Remove this VM from all dependency lists
                    for o in list(deps.keys()):
                        deps[o].discard(gvm_name)
                    # And remove its own dependencies
                    deps.pop(gvm_name, None)
                cycle_broken = True; break
            else:
                pass 
        if not cycle_broken:
            return MigrationPlan([], dependency_edges, [], False, sorted(all_cycle))

    # Step Generation
    current_node_used, steps, step_num = defaultdict(int, node_used), [], 1
    queue = []
    for tm in temp_migrations: queue.append((tm.source, tm.target, tm.vm, True))
    for vm_name, m in mig_map.items():
        if vm_name not in temp_moves: queue.append((m.source, m.target, m.vm, False))

    while queue:
        cur_step, inflow, processed = [], defaultdict(int), []
        skip_vms = set()
        
        for i, (src, dst, vn, is_temp) in enumerate(queue):
            if vn in skip_vms: continue
            
            # For PVE groups, we must check if the WHOLE group can move together
            # Internal rules (origin != 'pve') are handled individually.
            group = pve_groups.get(vn, {vn})
            if len(group) == 1:
                # Standard individual move
                if deps.get(vn): continue
                if node_mem.get(dst, 0) - current_node_used[dst] < vm_by_name[vn].memory: continue
                if max_inflow and inflow[dst] >= max_inflow: continue
                if max_parallel and len(cur_step) >= max_parallel: break
                
                cur_step.append(Migration(vn, src, dst))
                inflow[dst] += 1
                processed.append(i)
                skip_vms.add(vn)
                continue

            # Group move for PVE native affinity
            group_migs = []
            can_move_group = True
            
            # Find all members of this group that are in the queue
            queue_indices = []
            for j, (q_src, q_dst, q_vn, q_is_temp) in enumerate(queue):
                if q_vn in group and q_vn not in skip_vms:
                    # Check dependencies for this member
                    if deps.get(q_vn): 
                        can_move_group = False; break
                    # Check target capacity
                    if node_mem.get(q_dst, 0) - current_node_used[q_dst] < vm_by_name[q_vn].memory:
                        can_move_group = False; break
                    group_migs.append((j, Migration(q_vn, q_src, q_dst)))
                    queue_indices.append(j)
            
            if not can_move_group or not group_migs:
                continue

            # Check inflow limits for the whole group on their respective targets
            for _, mig in group_migs:
                if max_inflow and inflow[mig.target] >= max_inflow:
                    can_move_group = False; break
            
            if not can_move_group:
                continue
                
            # Check parallel limit
            if max_parallel and len(cur_step) + len(group_migs) > max_parallel:
                if len(cur_step) == 0:
                    # Allow atomic group even if it exceeds parallel limit to avoid deadlock
                    pass 
                else:
                    break
            
            # Add all members to current step
            for idx, mig in group_migs:
                cur_step.append(mig)
                inflow[mig.target] += 1
                processed.append(idx)
                skip_vms.add(mig.vm)

        if not cur_step: break
        steps.append(MigrationStep(step_num, cur_step, len(cur_step) > 1)); step_num += 1
        for idx in sorted(processed, reverse=True):
            src, dst, vn, is_t = queue.pop(idx)
            current_node_used[src] -= vm_by_name[vn].memory; current_node_used[dst] += vm_by_name[vn].memory
            for o in list(deps.keys()): deps[o].discard(vn)
            if is_t: queue.append((dst, mig_map[vn].target, vn, False))

    return MigrationPlan(steps, dependency_edges, temp_moves)
