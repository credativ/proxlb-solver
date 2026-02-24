"""Migration planner — orders migrations respecting dependencies."""

from __future__ import annotations

from collections import defaultdict, deque

from .models import Cluster, Migration, MigrationPlan, MigrationStep, Solution


def _allowed_temp_nodes(
    vm_name: str,
    cluster: Cluster,
    solution: Solution,
    exclude: set[str],
    node_used_mem: dict[str, int],
    node_used_cpu: dict[str, int],
) -> list[str]:
    """Return nodes where *vm_name* may temporarily reside.

    Checks pin, affinity, anti-affinity, ignore, maintenance,
    RAM capacity and CPU capacity (with overcommit).
    """
    vm = next(v for v in cluster.vms if v.name == vm_name)
    cons = cluster.constraints
    node_map = {n.name: n for n in cluster.nodes}

    # Ignored VMs must not move at all
    if vm_name in cons.ignore:
        return []

    # Start with non-maintenance nodes
    candidates = {n.name for n in cluster.nodes if not n.maintenance}

    # Exclude source / target (caller passes these)
    candidates -= exclude

    # Evacuate node
    if cluster.evacuate_node:
        candidates.discard(cluster.evacuate_node)

    # Pin constraint — VM may only reside on listed nodes
    for rule in cons.pin:
        if rule["vm"] == vm_name:
            candidates &= set(rule["nodes"])

    # Anti-affinity — temp node must not host an anti-affinity partner
    # Use the *solution* placements for the final state, but during temp
    # moves we care about who is physically on each node right now.  We
    # approximate with the initial placement (cluster.vms) since temp
    # moves happen before any other migration.
    current_node_vms: dict[str, set[str]] = defaultdict(set)
    for v in cluster.vms:
        current_node_vms[v.node].add(v.name)

    for rule in cons.anti_affinity:
        if vm_name in rule["vms"]:
            partners = set(rule["vms"]) - {vm_name}
            for node_name in list(candidates):
                if partners & current_node_vms.get(node_name, set()):
                    candidates.discard(node_name)

    # Affinity — all group members must stay together.  A temp move
    # inherently separates the VM from its affinity partners, so we can
    # only allow nodes where ALL partners already are.  In practice this
    # means affinity VMs can only temp-move to the node their partners
    # are on (if any space).  If no such node exists, the list is empty
    # and cycle-breaking will have to try another VM.
    for rule in cons.affinity:
        if vm_name in rule["vms"]:
            partners = set(rule["vms"]) - {vm_name}
            if partners:
                # Find the node(s) where partners currently sit
                partner_nodes = set()
                for p in partners:
                    for v in cluster.vms:
                        if v.name == p:
                            partner_nodes.add(v.node)
                # VM must go where partners are (only if not excluded)
                candidates &= partner_nodes

    # RAM capacity
    for node_name in list(candidates):
        node = node_map[node_name]
        avail = node.memory_total - node_used_mem.get(node_name, 0)
        if avail < vm.memory:
            candidates.discard(node_name)

    # CPU capacity (with overcommit)
    cpu_overcommit = cluster.balancing.cpu_overcommit
    for node_name in list(candidates):
        node = node_map[node_name]
        effective_cpu = int(node.cpu_total * cpu_overcommit)
        used_cpu = node_used_cpu.get(node_name, 0)
        if used_cpu + vm.cpu > effective_cpu:
            candidates.discard(node_name)

    return sorted(candidates)


def plan_migrations(
    cluster: Cluster, solution: Solution,
    max_parallel: int | None = None,
) -> MigrationPlan:
    """Order migrations into executable steps respecting dependencies.

    Builds a dependency graph: if VM-A needs to move to node-X, but
    node-X is full until VM-B moves away, then VM-B must migrate first.

    Uses Kahn's algorithm for layered topological sort — each layer
    contains migrations that can execute in parallel.

    Cycles are broken by inserting temp-moves to a third node,
    respecting all constraints (pin, affinity, anti-affinity,
    maintenance, CPU/RAM capacity).

    max_parallel: if set, limits the number of migrations per step.
        A layer with more ready migrations is split into chunks of
        this size, each becoming its own step.
    """
    if not solution.feasible or not solution.migrations:
        return MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])

    node_mem = {}
    for node in cluster.nodes:
        node_mem[node.name] = node.memory_total

    # Current memory usage per node (before any migration)
    node_used: dict[str, int] = defaultdict(int)
    node_cpu: dict[str, int] = defaultdict(int)
    vm_by_name: dict[str, any] = {}
    for vm in cluster.vms:
        node_used[vm.node] += vm.memory
        node_cpu[vm.node] += vm.cpu
        vm_by_name[vm.name] = vm

    # Build migration lookup
    mig_map: dict[str, Migration] = {m.vm: m for m in solution.migrations}

    # Which VMs currently reside on each node
    node_residents: dict[str, set[str]] = defaultdict(set)
    for vm in cluster.vms:
        node_residents[vm.node].add(vm.name)

    # Build dependency graph:
    # migration of VM-A depends on migration of VM-B if:
    #   - VM-A wants to go to node-X
    #   - node-X is currently full (no space for VM-A)
    #   - VM-B is on node-X and is migrating away
    deps: dict[str, set[str]] = defaultdict(set)  # vm -> set of vms it waits for

    for vm_name, mig in mig_map.items():
        vm = vm_by_name[vm_name]
        target = mig.target
        avail = node_mem.get(target, 0) - node_used[target]
        if avail >= vm.memory:
            continue
        for resident in node_residents[target]:
            if resident in mig_map and mig_map[resident].source == target:
                deps[vm_name].add(resident)

    # Detect cycles using DFS
    cycle_members: set[str] = set()
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _find_cycles(vm_name: str) -> None:
        if vm_name in visited:
            return
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

    # Break cycles with temp moves (constraint-aware)
    temp_moves: list[str] = []
    temp_migrations: list[Migration] = []
    unbreakable_cycle: list[str] = []

    if cycle_members:
        # Working copies for bookkeeping
        node_used_work: dict[str, int] = defaultdict(int, node_used)
        node_cpu_work: dict[str, int] = defaultdict(int, node_cpu)

        cycle_broken = False
        for vm_name in sorted(cycle_members):  # sorted for determinism
            vm = vm_by_name[vm_name]
            mig = mig_map[vm_name]

            # Find a constraint-respecting temp node
            allowed = _allowed_temp_nodes(
                vm_name, cluster, solution,
                exclude={mig.source, mig.target},
                node_used_mem=node_used_work,
                node_used_cpu=node_cpu_work,
            )

            if allowed:
                temp_node = allowed[0]
                temp_moves.append(vm_name)
                temp_migrations.append(
                    Migration(vm=vm_name, source=mig.source, target=temp_node)
                )
                # Update bookkeeping for temp move
                node_used_work[mig.source] -= vm.memory
                node_used_work[temp_node] += vm.memory
                node_cpu_work[mig.source] -= vm.cpu
                node_cpu_work[temp_node] += vm.cpu
                # Remove this VM from deps (cycle is broken)
                for other in list(deps.keys()):
                    deps[other].discard(vm_name)
                deps.pop(vm_name, None)
                cycle_broken = True
                break  # Break one edge to resolve the cycle

        if not cycle_broken:
            # No VM in the cycle can be temp-moved — path is infeasible
            # Report all VMs involved in dependency cycles (not just
            # the ones found by DFS marking, which may be incomplete).
            # Any VM that has deps AND is depended upon is in a cycle.
            depended_on = set()
            for dep_set in deps.values():
                depended_on.update(dep_set)
            all_cycle = set()
            for vm_name in deps:
                if vm_name in depended_on or deps[vm_name] & depended_on:
                    all_cycle.add(vm_name)
            all_cycle.update(cycle_members)
            unbreakable_cycle = sorted(all_cycle)
            return MigrationPlan(
                steps=[],
                dependency_edges=[(a, b) for a in deps for b in deps[a]],
                temp_moves=[],
                path_feasible=False,
                unbreakable_cycle=unbreakable_cycle,
            )

    # Collect dependency edges for reporting
    dependency_edges: list[tuple[str, str]] = []
    for vm_a, dep_set in deps.items():
        for vm_b in dep_set:
            dependency_edges.append((vm_a, vm_b))

    # Kahn's algorithm — layered topological sort
    # Build in-degree map for non-cycle VMs
    remaining_vms = {vm_name for vm_name in mig_map if vm_name not in temp_moves}
    in_degree: dict[str, int] = {vm: 0 for vm in remaining_vms}
    adj: dict[str, set[str]] = defaultdict(set)  # vm_b -> set of vms that depend on b

    for vm_a in remaining_vms:
        for vm_b in deps.get(vm_a, set()):
            if vm_b in remaining_vms:
                in_degree[vm_a] += 1
                adj[vm_b].add(vm_a)

    steps: list[MigrationStep] = []
    step_num = 1

    def _add_steps(migrations: list[Migration]) -> None:
        """Append one or more MigrationSteps, respecting max_parallel."""
        nonlocal step_num
        if not migrations:
            return
        if max_parallel and max_parallel > 0:
            for i in range(0, len(migrations), max_parallel):
                chunk = migrations[i:i + max_parallel]
                steps.append(MigrationStep(
                    step=step_num,
                    migrations=chunk,
                    parallel=len(chunk) > 1,
                ))
                step_num += 1
        else:
            steps.append(MigrationStep(
                step=step_num,
                migrations=migrations,
                parallel=len(migrations) > 1,
            ))
            step_num += 1

    # If there are temp moves, they go first as step 1
    _add_steps(temp_migrations)

    # Process layers
    while remaining_vms:
        # Find all VMs with in-degree 0
        ready = sorted(
            vm for vm in remaining_vms if in_degree.get(vm, 0) == 0
        )

        if not ready:
            # Shouldn't happen after cycle-breaking, but safety fallback
            ready = sorted(remaining_vms)

        layer_migrations = []
        for vm_name in ready:
            if vm_name in temp_moves:
                # Add final move for temp-moved VMs
                mig = mig_map[vm_name]
                # Find temp node from temp_migrations
                temp_src = None
                for tm in temp_migrations:
                    if tm.vm == vm_name:
                        temp_src = tm.target
                        break
                if temp_src:
                    layer_migrations.append(
                        Migration(vm=vm_name, source=temp_src, target=mig.target)
                    )
            else:
                layer_migrations.append(mig_map[vm_name])

        _add_steps(layer_migrations)

        # Remove processed VMs and update in-degrees
        for vm_name in ready:
            remaining_vms.discard(vm_name)
            for dependent in adj.get(vm_name, set()):
                if dependent in in_degree:
                    in_degree[dependent] -= 1

    # Add final moves for temp-moved VMs (if not already placed)
    final_moves = []
    placed_finals = {m.vm for s in steps for m in s.migrations
                     if m.vm in temp_moves and m.target == mig_map[m.vm].target}
    for vm_name in temp_moves:
        if vm_name not in placed_finals:
            mig = mig_map[vm_name]
            temp_src = None
            for tm in temp_migrations:
                if tm.vm == vm_name:
                    temp_src = tm.target
                    break
            if temp_src:
                final_moves.append(
                    Migration(vm=vm_name, source=temp_src, target=mig.target)
                )

    _add_steps(final_moves)

    return MigrationPlan(
        steps=steps,
        dependency_edges=dependency_edges,
        temp_moves=temp_moves,
    )
