"""
Migration planner — orders migrations respecting dependencies and operational safety.

This module determines THE SEQUENCE of migrations. The Solver finds a valid
target state, but the Planner ensures we can reach that state without
temporarily oversubscribing any node or violating native Proxmox HA rules.

ALGORITHM:
The planner uses a modified Topological Sort (Kahn's Algorithm):
1.  Build a dependency graph: VM-A depends on VM-B if VM-A needs space
    on a node that VM-B currently occupies.
2.  Identify and break circular dependencies (deadlocks) by inserting
    temporary "parking" moves to spare nodes.
3.  Group migrations into steps that can be executed in parallel,
    respecting node inflow limits and cluster-wide concurrency limits.
"""

from __future__ import annotations
from collections import defaultdict
from .models import Cluster, Migration, MigrationPlan, MigrationStep, Solution


def _get_affinity_group(vm_name: str, cluster: Cluster, origin: str | None = None) -> set[str]:
    """
    Finds all VMs belonging to the same affinity group(s) as vm_name.

    This is used to move groups of VMs together (important for PVE native rules)
    or to ensure that a whole group is moved during a temporary "parking" move.
    """
    group = {vm_name}
    changed = True
    while changed:
        changed = False
        for rule in cluster.constraints.affinity:
            # Optionally filter by rule origin (e.g., 'pve' or 'plb')
            if origin is not None and rule.get("origin") != origin:
                continue

            vms_in_rule = set(rule["vms"])
            # If any current member is in this rule, add all other members of the rule
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
    exclude_nodes: set[str],
    current_node_used_mem: dict[str, int],
    current_node_used_cpu: dict[str, int],
) -> list[str]:
    """
    Checks which nodes in the cluster can host a group of VMs temporarily.
    This is used to break deadlocks (e.g. Node A <-> Node B swap).
    """
    constraints = cluster.constraints
    node_map = {node.name: node for node in cluster.nodes}
    vms_in_group = [vm for vm in cluster.vms if vm.name in vm_group]

    # Safety: If any VM in the group is marked as 'ignored', the whole group stays put.
    if any(vm.name in constraints.ignore for vm in vms_in_group):
        return []

    # Candidates are all online nodes except maintenance nodes,
    # the current source node, and the final destination node.
    candidates = {node.name for node in cluster.nodes if not node.maintenance}
    candidates -= exclude_nodes

    if cluster.evacuate_node:
        candidates.discard(cluster.evacuate_node)

    # Filter candidates by Hard Constraints
    # 1. Node Pinning: VMs must be allowed to land on the temp node.
    for vm_name in vm_group:
        for rule in constraints.pin:
            if rule["vm"] == vm_name:
                candidates &= set(rule["nodes"])

    # 2. Hard Anti-Affinity: Ensure no conflict with VMs staying on the candidate node.
    current_node_residents: dict[str, set[str]] = defaultdict(set)
    for vm in cluster.vms:
        current_node_residents[vm.node].add(vm.name)

    migration_map = {m.vm: m for m in solution.migrations}

    for vm_name in vm_group:
        for rule in constraints.anti_affinity:
            if vm_name in rule["vms"] and rule.get("hard", True):
                # Partners that are NOT part of this current moving group
                external_partners = set(rule["vms"]) - vm_group
                for node_name in list(candidates):
                    # If any external partner is currently on that node, we can only land
                    # there if that partner is ALSO migrating away in this plan.
                    partners_on_node = external_partners & current_node_residents.get(node_name, set())
                    for partner in partners_on_node:
                        is_partner_leaving = (partner in migration_map and
                                            migration_map[partner].source == node_name)
                        if not is_partner_leaving:
                            candidates.discard(node_name)
                            break

    # 3. Capacity: Ensure the temp node has enough RAM and CPU for the WHOLE group.
    group_mem_total = sum(vm.memory for vm in vms_in_group)
    group_cpu_total = sum(vm.cpu for vm in vms_in_group)
    cpu_overcommit = cluster.balancing.cpu_overcommit

    for node_name in list(candidates):
        node = node_map[node_name]
        # RAM check
        free_mem = node.memory_total - current_node_used_mem.get(node_name, 0)
        if free_mem < group_mem_total:
            candidates.discard(node_name)
            continue
        # CPU check
        effective_cpu_limit = int(node.cpu_total * cpu_overcommit * 1000)
        current_cpu_load = current_node_used_cpu.get(node_name, 0) * 1000
        if current_cpu_load + (group_cpu_total * 1000) > effective_cpu_limit:
            candidates.discard(node_name)

    return sorted(candidates)


def plan_migrations(
    cluster: Cluster, solution: Solution,
    max_parallel: int | None = None,
) -> MigrationPlan:
    """
    Determines the executable sequence of migrations.
    Returns a MigrationPlan with ordered steps and identified dependencies.
    """
    if not solution.feasible or not solution.migrations:
        return MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])

    max_parallel = max_parallel or cluster.balancing.max_parallel_migrations
    max_inflow = cluster.balancing.max_node_inflow

    # Initial cluster state tracking
    node_mem_total = {node.name: node.memory_total for node in cluster.nodes}
    node_used_mem = defaultdict(int)
    node_used_cpu = defaultdict(int)
    vm_lookup = {vm.name: vm for vm in cluster.vms}

    for vm in cluster.vms:
        node_used_mem[vm.node] += vm.memory
        node_used_cpu[vm.node] += vm.cpu

    # ── PVE Native Affinity Handling ────────────────────────────────────────
    # For native PVE affinity, moving ONE member of the group triggers Proxmox
    # to move ALL other members automatically. We must only plan one API call
    # per group to avoid redundant or conflicting commands.
    all_migrations = {m.vm: m for m in solution.migrations}
    pve_deferred_vms: set[str] = set()

    for vm_name in sorted(all_migrations):
        if vm_name in pve_deferred_vms:
            continue
        # Find all partners in the same NATIVE Proxmox affinity group
        native_group = _get_affinity_group(vm_name, cluster, origin="pve")
        # Other members of this group that are also supposed to migrate
        other_migrants = (native_group & set(all_migrations)) - {vm_name}
        pve_deferred_vms.update(other_migrants)

    # Final list of migrations that require an explicit command
    active_migrations_map = {vm: m for vm, m in all_migrations.items() if vm not in pve_deferred_vms}

    node_residents = defaultdict(set)
    for vm in cluster.vms:
        node_residents[vm.node].add(vm.name)

    # ── Dependency Analysis ────────────────────────────────────────────────
    # Build a directed graph where A -> B means "VM A must wait for VM B to move".
    dependencies = defaultdict(set)

    # Pre-calculate partners for PVE Anti-Affinity (strict ordering requirement)
    pve_anti_affinity_partners = defaultdict(set)
    for rule in cluster.constraints.anti_affinity:
        if rule.get("origin") == "pve" and rule.get("hard", True):
            for v1 in rule["vms"]:
                for v2 in rule["vms"]:
                    if v1 != v2:
                        pve_anti_affinity_partners[v1].add(v2)

    for vm_name, migration in active_migrations_map.items():
        vm = vm_lookup[vm_name]
        # Does the target node currently have enough space for this VM?
        has_immediate_space = node_mem_total.get(migration.target, 0) - node_used_mem[migration.target] >= vm.memory

        # Check everyone currently residing on our target node
        for resident in node_residents[migration.target]:
            if resident == vm_name: continue

            # Dependency condition 1: Target node is too full (Capacity wait)
            is_capacity_block = not has_immediate_space

            # Dependency condition 2: Native anti-affinity (Strict separation)
            # We cannot land on the node until the anti-affine partner has left.
            is_pve_aa_block = resident in pve_anti_affinity_partners.get(vm_name, set())

            if is_capacity_block or is_pve_aa_block:
                # We depend on 'resident' ONLY if it is also moving away from our target node.
                if resident in active_migrations_map and active_migrations_map[resident].source == migration.target:
                    # SPECIAL CASE: Direct Swaps (A->B, B->A).
                    # If we add mutual dependencies here, we create an unbreakable cycle.
                    # PVE handles this internally by sequencing. We rely on the Planner's
                    # step serialization instead of adding a hard graph dependency.
                    if is_pve_aa_block and active_migrations_map[resident].target == vm.node:
                        continue

                    dependencies[vm_name].add(resident)

    dependency_edges = [(a, b) for a in dependencies for b in dependencies[a]]

    # ── Deadlock Detection (Cycles) ────────────────────────────────────────
    # Find sets of VMs that form a dependency loop (e.g. A depends on B, B on A).
    cycle_members, visited, recursion_stack = set(), set(), set()

    def _find_cycles(node_name: str):
        if node_name in visited: return
        if node_name in recursion_stack:
            cycle_members.add(node_name); return
        recursion_stack.add(node_name)
        for neighbor in dependencies.get(node_name, set()):
            _find_cycles(neighbor)
        recursion_stack.discard(node_name)
        visited.add(node_name)

    for vm_name in active_migrations_map:
        _find_cycles(vm_name)

    # ── Cycle Breaking (Temp-Moves) ────────────────────────────────────────
    temp_moves_vms, temp_migrations_list = [], []

    if cycle_members:
        # Expand the cycle set to include all nodes that lead into it
        all_cycle_participants = set(cycle_members)
        expansion_occurred = True
        while expansion_occurred:
            expansion_occurred = False
            for vm, target_set in dependencies.items():
                if vm not in all_cycle_participants and (target_set & all_cycle_participants):
                    all_cycle_participants.add(vm)
                    expansion_occurred = True

        # Working state for capacity simulation
        node_used_mem_work = defaultdict(int, node_used_mem)
        node_used_cpu_work = defaultdict(int, node_used_cpu)

        is_cycle_broken = False
        # Try to break the cycle by moving ONE VM (and its affinity group) to a temp node.
        for vm_name in sorted(all_cycle_participants):
            migration = active_migrations_map[vm_name]
            affinity_group = _get_affinity_group(vm_name, cluster)

            # Look for a parking spot
            spare_nodes = _allowed_temp_nodes(
                affinity_group, cluster, solution,
                {migration.source, migration.target},
                node_used_mem_work, node_used_cpu_work
            )

            if spare_nodes:
                parking_node = spare_nodes[0]
                for member_name in sorted(affinity_group):
                    member_vm = vm_lookup[member_name]
                    temp_moves_vms.append(member_name)
                    # Step 1: Move to parking spot
                    temp_migrations_list.append(Migration(member_name, member_vm.node, parking_node))

                    # Update simulated load
                    node_used_mem_work[member_vm.node] -= member_vm.memory
                    node_used_mem_work[parking_node] += member_vm.memory
                    node_used_cpu_work[member_vm.node] -= member_vm.cpu
                    node_used_cpu_work[parking_node] += member_vm.cpu

                    # Remove all outgoing dependencies for this VM (it's now "free" to move)
                    dependencies.pop(member_name, None)
                    # Remove all incoming dependencies (it no longer blocks others)
                    for other_vm in list(dependencies.keys()):
                        dependencies[other_vm].discard(member_name)

                is_cycle_broken = True
                break

        if not is_cycle_broken:
            # Fatal: The cluster is too full or constrained to break the dependency cycle.
            return MigrationPlan([], dependency_edges, [], False, sorted(all_cycle_participants), sorted(pve_deferred_vms))

    # ── Step Generation (Kahn's Algorithm) ──────────────────────────────────
    simulated_node_used_mem = defaultdict(int, node_used_mem)
    final_steps = []
    step_counter = 1

    # Priority Queue: Temp-moves (parking) must always happen first
    task_queue = []
    for temp_mig in temp_migrations_list:
        task_queue.append((temp_mig.source, temp_mig.target, temp_mig.vm, True))
    for vm_name, mig in active_migrations_map.items():
        if vm_name not in temp_moves_vms:
            task_queue.append((mig.source, mig.target, mig.vm, False))

    while task_queue:
        current_step_migrations = []
        inflow_count = defaultdict(int)
        processed_indices = []

        for i, (source, target, vm_name, is_temp_move) in enumerate(task_queue):
            # 1. Dependency Check: Can only move if no one is blocking us
            if dependencies.get(vm_name):
                continue

            # 2. Capacity Check: Target node must have space RIGHT NOW
            target_free_mem = node_mem_total.get(target, 0) - simulated_node_used_mem[target]
            if target_free_mem < vm_lookup[vm_name].memory:
                continue

            # 3. Operational Safety: Target node inflow limit
            if max_inflow and inflow_count[target] >= max_inflow:
                continue

            # 4. Global Concurrency limit
            if max_parallel and len(current_step_migrations) >= max_parallel:
                break

            # All checks passed! Add to current step
            current_step_migrations.append(Migration(vm_name, source, target))
            inflow_count[target] += 1
            processed_indices.append(i)

        if not current_step_migrations:
            # If queue is not empty but no one can move, we hit an unexpected deadlock
            break

        # Register the completed step
        is_parallel = len(current_step_migrations) > 1
        final_steps.append(MigrationStep(step_counter, current_step_migrations, is_parallel))
        step_counter += 1

        # Execute migrations in simulation (free space at source, take at destination)
        for idx in sorted(processed_indices, reverse=True):
            source, target, vm_name, was_temp = task_queue.pop(idx)
            simulated_node_used_mem[source] -= vm_lookup[vm_name].memory
            simulated_node_used_mem[target] += vm_lookup[vm_name].memory

            # Remove this VM from all dependency lists (unlocking others)
            for other_vm in list(dependencies.keys()):
                dependencies[other_vm].discard(vm_name)

            # If this was a parking move, add the second half (Parking -> Target) to the queue
            if was_temp:
                final_target = all_migrations[vm_name].target
                task_queue.append((target, final_target, vm_name, False))

    return MigrationPlan(
        final_steps,
        dependency_edges,
        temp_moves_vms,
        pve_deferred=sorted(pve_deferred_vms)
    )
