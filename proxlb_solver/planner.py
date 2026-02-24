"""Migration planner — orders migrations respecting dependencies."""

from __future__ import annotations

from collections import defaultdict, deque

from .models import Cluster, Migration, MigrationPlan, MigrationStep, Solution


def plan_migrations(
    cluster: Cluster, solution: Solution,
    max_parallel: int | None = None,
) -> MigrationPlan:
    """Order migrations into executable steps respecting dependencies.

    Builds a dependency graph: if VM-A needs to move to node-X, but
    node-X is full until VM-B moves away, then VM-B must migrate first.

    Uses Kahn's algorithm for layered topological sort — each layer
    contains migrations that can execute in parallel.

    Cycles are broken by inserting temp-moves to a third node.

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
    vm_by_name: dict[str, any] = {}
    for vm in cluster.vms:
        node_used[vm.node] += vm.memory
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

    # Break cycles with temp moves
    temp_moves: list[str] = []
    temp_migrations: list[Migration] = []  # (temp_move, final_move) pairs
    all_nodes = {n.name for n in cluster.nodes if not n.maintenance}

    if cycle_members:
        # We need a working copy of node_used for bookkeeping
        node_used_work: dict[str, int] = defaultdict(int, node_used)

        for vm_name in sorted(cycle_members):  # sorted for determinism
            vm = vm_by_name[vm_name]
            mig = mig_map[vm_name]

            # Find a temp node (not source, not target) with enough space
            temp_node = None
            for n in sorted(all_nodes):
                if n == mig.source or n == mig.target:
                    continue
                avail = node_mem.get(n, 0) - node_used_work[n]
                if avail >= vm.memory:
                    temp_node = n
                    break

            if temp_node:
                temp_moves.append(vm_name)
                temp_migrations.append(
                    Migration(vm=vm_name, source=mig.source, target=temp_node)
                )
                # Update bookkeeping for temp move
                node_used_work[mig.source] -= vm.memory
                node_used_work[temp_node] += vm.memory
                # Remove this VM from deps (cycle is broken)
                for other in list(deps.keys()):
                    deps[other].discard(vm_name)
                deps.pop(vm_name, None)
                # The final move will be added after non-cycle migrations
                break  # Break one edge to resolve the cycle

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
