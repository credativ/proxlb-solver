"""CP-SAT based VM placement solver."""

from __future__ import annotations

import time

from ortools.sat.python import cp_model

from .models import Balancing, Cluster, Migration, Solution, SolverStats, MigrationPlan
from .planner import plan_migrations

# Scale RAM to MB for integer arithmetic
_MB = 1024 * 1024
# Scale load percentages ×10000 for integer precision
_LOAD_SCALE = 10000

# VMware DRS-style balanciness profiles
# (w_balance, w_stickiness, migration_threshold as fraction 0.0–1.0)
_BALANCINESS_PROFILES = {
    1: (0, 1, 1.0),       # Conservative: only hard constraints
    2: (1, 50, 0.25),     # Low: migrate only if >25% gap
    3: (10, 10, 0.15),    # Moderate (default)
    4: (50, 5, 0.05),     # High: active rebalancing
    5: (100, 1, 0.0),     # Aggressive: chase perfect balance
}


def _initial_load_gap(cluster: Cluster) -> float:
    """Compute the current RAM load gap before any optimization."""
    loads = []
    for node in cluster.nodes:
        if node.maintenance:
            continue
        used = sum(
            vm.memory for vm in cluster.vms if vm.node == node.name
        )
        pct = used / node.memory_total if node.memory_total else 0
        loads.append(pct)
    if not loads:
        return 0.0
    return max(loads) - min(loads)


def _resolve_balancing(
    bal: Balancing, current_gap: float
) -> tuple[int, int]:
    """Resolve effective (w_balance, w_stickiness) from balanciness.

    Returns the weights to use in the objective function.
    If w_balance/w_stickiness are explicitly set, they override the profile.
    If the current load gap is below the profile's migration threshold,
    w_balance is set to 0 (no voluntary migrations).
    """
    level = max(1, min(5, bal.balanciness))
    prof_wb, prof_ws, threshold = _BALANCINESS_PROFILES[level]

    w_b = bal.w_balance if bal.w_balance is not None else prof_wb
    w_s = bal.w_stickiness if bal.w_stickiness is not None else prof_ws

    # Threshold check: if cluster is already balanced enough, skip
    if current_gap < threshold:
        w_b = 0

    return w_b, w_s


def _find_blocking_vms(
    cluster: Cluster, time_limit_s: float
) -> list[str]:
    """Identify VMs that prevent evacuation of the target node.

    Strategy: try to evacuate each VM individually. If even a single
    VM cannot be placed elsewhere (due to pin, affinity, capacity, etc.),
    it is a blocker. Also detect aggregate capacity issues.
    """
    evac_node = cluster.evacuate_node
    if not evac_node:
        return []

    nodes = cluster.nodes
    vms = cluster.vms
    bal = cluster.balancing
    cons = cluster.constraints

    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}

    vms_on_node = [v for v in vms if v.node == evac_node]
    if not vms_on_node:
        return []

    blockers: list[str] = []

    for test_vm in vms_on_node:
        # Build a minimal model: can this single VM be placed elsewhere?
        model = cp_model.CpModel()
        y = []
        for j, node in enumerate(nodes):
            y.append(model.new_bool_var(f"y_{test_vm.name}_{node.name}"))

        # Must be placed on exactly one node
        model.add(sum(y) == 1)

        # Not on the evacuated node
        ej = node_idx[evac_node]
        model.add(y[ej] == 0)

        # Not on maintenance nodes
        for j, node in enumerate(nodes):
            if node.maintenance:
                model.add(y[j] == 0)

        # Respect pinning
        for rule in cons.pin:
            if rule["vm"] == test_vm.name:
                allowed = {node_idx[n] for n in rule["nodes"]
                           if n in node_idx}
                for j in range(len(nodes)):
                    if j not in allowed:
                        model.add(y[j] == 0)

        # RAM capacity (considering other VMs staying put for now)
        for j, node in enumerate(nodes):
            if j == ej:
                continue
            other_load = sum(
                v.memory for v in vms
                if v.node == node.name and v.name != test_vm.name
            )
            avail_mb = (node.memory_total - other_load) // _MB
            vm_mb = test_vm.memory // _MB
            # y[j] == 1 implies vm_mb <= avail_mb
            if vm_mb > avail_mb:
                model.add(y[j] == 0)

        # CPU capacity
        overcommit_1000 = int(bal.cpu_overcommit * 1000)
        for j, node in enumerate(nodes):
            if j == ej:
                continue
            other_cpu = sum(
                v.cpu for v in vms
                if v.node == node.name and v.name != test_vm.name
            )
            avail_cpu_1000 = node.cpu_total * overcommit_1000 - other_cpu * 1000
            if test_vm.cpu * 1000 > avail_cpu_1000:
                model.add(y[j] == 0)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(time_limit_s, 5.0)
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            blockers.append(test_vm.name)

    # If no individual blocker found but full solve failed,
    # it's an aggregate capacity issue — report all VMs on that node
    if not blockers:
        blockers = [v.name for v in vms_on_node]

    return blockers


def solve(
    cluster: Cluster,
    time_limit_s: float = 30.0,
    forbidden_placements: list[dict[str, str]] | None = None
) -> Solution:
    """Solve VM placement using CP-SAT."""
    model = cp_model.CpModel()

    nodes = cluster.nodes
    vms = cluster.vms
    bal = cluster.balancing
    cons = cluster.constraints

    # Resolve effective weights from balanciness level
    current_gap = _initial_load_gap(cluster)
    eff_w_balance, eff_w_stickiness = _resolve_balancing(bal, current_gap)

    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}

    non_maintenance_nodes = [
        j for j, n in enumerate(nodes) if not n.maintenance
    ]

    # ── Decision Variables ──
    # x[i][j] = 1 iff VM i is placed on Node j
    x: list[list] = []
    for i, vm in enumerate(vms):
        row = []
        for j, node in enumerate(nodes):
            row.append(model.new_bool_var(f"x_{vm.name}_{node.name}"))
        x.append(row)

    # ── Feedback Loop: Forbidden Placements ──
    if forbidden_placements:
        for forbidden in forbidden_placements:
            # sum(x[vm][node] for vm, node in forbidden) <= len(forbidden) - 1
            # Meaning: at least one of these assignments must NOT happen
            literals = []
            for vm_name, node_name in forbidden.items():
                if vm_name in vm_idx and node_name in node_idx:
                    i = vm_idx[vm_name]
                    j = node_idx[node_name]
                    literals.append(x[i][j])
            
            if literals:
                model.add(sum(literals) <= len(literals) - 1)

    # ── Hard Constraint 1: Unique Placement ──
    for i in range(len(vms)):
        model.add(sum(x[i][j] for j in range(len(nodes))) == 1)

    # ── Hard Constraint 7: Maintenance — no VMs on maintenance nodes ──
    for j, node in enumerate(nodes):
        if node.maintenance:
            for i in range(len(vms)):
                model.add(x[i][j] == 0)

    # ── Evacuation — drain all VMs from the target node ──
    evacuate_j = None
    if cluster.evacuate_node:
        evacuate_j = node_idx.get(cluster.evacuate_node)
        if evacuate_j is not None:
            for i in range(len(vms)):
                model.add(x[i][evacuate_j] == 0)

    # ── Hard Constraint 8: Ignore — pinned to current node ──
    ignored_vms = set(cons.ignore)
    for i, vm in enumerate(vms):
        if vm.name in ignored_vms:
            current_j = node_idx[vm.node]
            model.add(x[i][current_j] == 1)

    # ── Hard Constraint 2: RAM Capacity ──
    for j, node in enumerate(nodes):
        mem_cap_mb = node.memory_total // _MB
        model.add(
            sum(
                (vms[i].memory // _MB) * x[i][j]
                for i in range(len(vms))
            ) <= mem_cap_mb
        )

    # ── Hard Constraint 3: CPU Capacity (Overcommitted) ──
    cpu_overcommit = bal.cpu_overcommit
    for j, node in enumerate(nodes):
        # Scale: multiply both sides by 1000 to avoid floats
        overcommit_1000 = int(cpu_overcommit * 1000)
        model.add(
            sum(
                vms[i].cpu * 1000 * x[i][j]
                for i in range(len(vms))
            ) <= node.cpu_total * overcommit_1000
        )

    # ── Hard Constraint 4: Anti-Affinity ──
    for rule in cons.anti_affinity:
        aa_vms_indices = [vm_idx[name] for name in rule["vms"] if name in vm_idx]
        if len(aa_vms_indices) < 2:
            continue
        for j in range(len(nodes)):
            # Sum of all VMs in the group on node j must be <= 1
            model.add(sum(x[i][j] for i in aa_vms_indices) <= 1)

    # ── Hard Constraint 5: Affinity — all VMs in group on same node ──
    for rule in cons.affinity:
        aff_vms = [vm_idx[name] for name in rule["vms"] if name in vm_idx]
        if len(aff_vms) < 2:
            continue
        first = aff_vms[0]
        for other in aff_vms[1:]:
            for j in range(len(nodes)):
                model.add(x[first][j] == x[other][j])

    # ── Hard Constraint 6: Pinning ──
    for rule in cons.pin:
        vi = vm_idx.get(rule["vm"])
        if vi is None:
            continue
        allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
        for j in range(len(nodes)):
            if j not in allowed:
                model.add(x[vi][j] == 0)

    # ── Objective ──
    # Load per node: we normalize to a common denominator so all loads
    # are comparable integers. For each node j:
    #   load_j = used_mb * LOAD_SCALE * (LCM / cap_mb) / LCM
    # Simpler approach: compute used_j * LOAD_SCALE / cap_j using
    # AddDivisionEquality which floors the result.
    load_vars = []
    total_vm_mem_mb = sum(v.memory // _MB for v in vms)
    for j, node in enumerate(nodes):
        if node.maintenance:
            continue
        mem_cap_mb = node.memory_total // _MB
        if mem_cap_mb == 0:
            continue
        used_scaled = model.new_int_var(
            0, total_vm_mem_mb * _LOAD_SCALE,
            f"used_scaled_{node.name}"
        )
        used = sum(
            (vms[i].memory // _MB) * x[i][j]
            for i in range(len(vms))
        )
        model.add(used_scaled == used * _LOAD_SCALE)

        load_j = model.new_int_var(0, _LOAD_SCALE, f"load_{node.name}")
        cap_var = model.new_constant(mem_cap_mb)
        model.add_division_equality(load_j, used_scaled, cap_var)
        load_vars.append(load_j)

    if load_vars:
        max_load = model.new_int_var(0, _LOAD_SCALE, "max_load")
        min_load = model.new_int_var(0, _LOAD_SCALE, "min_load")
        model.add_max_equality(max_load, load_vars)
        model.add_min_equality(min_load, load_vars)
        load_gap = model.new_int_var(0, _LOAD_SCALE, "load_gap")
        model.add(load_gap == max_load - min_load)
    else:
        load_gap = model.new_constant(0)

    # Migration count: number of VMs not on their original node
    migration_bools = []
    for i, vm in enumerate(vms):
        if vm.name in ignored_vms:
            continue
        current_j = node_idx[vm.node]
        # stayed = x[i][current_j]; migrated = 1 - stayed
        migrated = model.new_bool_var(f"mig_{vm.name}")
        model.add(migrated == 1 - x[i][current_j])
        migration_bools.append(migrated)

    migration_count = model.new_int_var(
        0, len(vms), "migration_count"
    )
    if migration_bools:
        model.add(migration_count == sum(migration_bools))
    else:
        model.add(migration_count == 0)

    # Weighted objective: minimize
    model.minimize(
        eff_w_balance * load_gap + eff_w_stickiness * migration_count
    )

    # ── Solve ──
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    t0 = time.monotonic()
    status = solver.solve(model)
    wall_time_ms = (time.monotonic() - t0) * 1000

    status_name = solver.status_name(status)
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    if not feasible:
        blocking = []
        if cluster.evacuate_node:
            blocking = _find_blocking_vms(cluster, time_limit_s)
        return Solution(
            feasible=False,
            placements={},
            migrations=[],
            stats=SolverStats(
                status=status_name,
                objective=0,
                load_gap=0.0,
                migration_count=0,
                wall_time_ms=wall_time_ms,
            ),
            blocking_vms=blocking,
        )

    # Extract placements
    placements: dict[str, str] = {}
    for i, vm in enumerate(vms):
        for j, node in enumerate(nodes):
            if solver.value(x[i][j]):
                placements[vm.name] = node.name
                break

    # Compute migrations
    migrations = []
    for vm in vms:
        target = placements[vm.name]
        if target != vm.node:
            migrations.append(Migration(
                vm=vm.name, source=vm.node, target=target
            ))

    return Solution(
        feasible=True,
        placements=placements,
        migrations=migrations,
        stats=SolverStats(
            status=status_name,
            objective=solver.objective_value,
            load_gap=solver.value(load_gap) / _LOAD_SCALE
            if load_vars else 0.0,
            migration_count=len(migrations),
            wall_time_ms=wall_time_ms,
        ),
    )


def solve_reachable(
    cluster: Cluster,
    total_time_limit_s: float = 60.0,
    max_retries: int = 10,
    quiet: bool = True,
) -> tuple[Solution, MigrationPlan]:
    """Solve VM placement and ensure the solution is reachable.

    Iteratively calls solve() and plan_migrations(). If the planner finds
    an unbreakable cycle, it adds a 'no-good' constraint to the solver
    and retries until a reachable solution is found or time/retry limits hit.
    """
    forbidden = []
    last_solution = None
    last_plan = None
    start_time = time.monotonic()

    for attempt in range(max_retries + 1):
        elapsed = time.monotonic() - start_time
        remaining = total_time_limit_s - elapsed
        
        if remaining <= 0:
            if not quiet:
                print(f"  [solve_reachable] Global timeout reached after {attempt} attempts.")
            break

        # Give the solver the remaining time, but at least 1s
        solve_timeout = max(1.0, remaining)
        solution = solve(cluster, time_limit_s=solve_timeout, forbidden_placements=forbidden)
        
        if not solution.feasible:
            if last_solution:
                import dataclasses
                return dataclasses.replace(last_solution, path_feasible=False), last_plan
            return solution, plan_migrations(cluster, solution)

        plan = plan_migrations(cluster, solution)
        last_solution = solution
        last_plan = plan

        if plan.path_feasible:
            return solution, plan

        if not plan.unbreakable_cycle:
            # Path infeasible but no cycle identified? Should not happen.
            break

        # Forbid this specific target configuration for the cycle VMs
        # This is a 'no-good' clause that prunes large parts of the search tree
        cycle_forbidden = {}
        for vm_name in plan.unbreakable_cycle:
            target_node = solution.placements.get(vm_name)
            if target_node:
                cycle_forbidden[vm_name] = target_node
        
        if not cycle_forbidden:
            break

        forbidden.append(cycle_forbidden)
        if not quiet:
            print(f"  [solve_reachable] Attempt {attempt+1}: Unbreakable cycle {plan.unbreakable_cycle}. Retrying...")

    # If we fall through, it means we hit max_retries or timeout without finding a feasible path.
    # Return the last found (but unreachable) solution marked as path_feasible=False.
    if last_solution:
        import dataclasses
        return dataclasses.replace(last_solution, path_feasible=False), last_plan
    
    return solution, plan_migrations(cluster, solution)
