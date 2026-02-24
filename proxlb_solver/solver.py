"""CP-SAT based VM placement solver."""

from __future__ import annotations

import time

from ortools.sat.python import cp_model

from .models import Balancing, Cluster, Migration, Solution, SolverStats, MigrationPlan
from .planner import plan_migrations
from .validator import validate_and_merge_constraints, RuleConflictError

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
    """Compute the initial load gap using the sum of VM footprints."""
    method = cluster.balancing.method
    bal = cluster.balancing
    
    # For smart modes, we need a composite gap.
    if method in ["cpu_smart", "memory_smart", "io_smart"]:
        usage_loads = []
        psi_loads = []
        
        for node in cluster.nodes:
            if node.maintenance: continue
            
            if method == "cpu_smart":
                u_used = sum(vm.cpu_usage for vm in cluster.vms if vm.node == node.name)
                u_total = node.cpu_total
                p_used = sum(vm.cpu_pressure for vm in cluster.vms if vm.node == node.name)
                w_u, w_p = bal.w_cpu_usage, bal.w_cpu_psi
            elif method == "memory_smart":
                u_used = sum(vm.memory for vm in cluster.vms if vm.node == node.name)
                u_total = node.memory_total
                p_used = sum(vm.memory_pressure for vm in cluster.vms if vm.node == node.name)
                w_u, w_p = bal.w_mem_usage, bal.w_mem_psi
            else: # io_smart
                u_used = sum(sum(vm.disks.values()) for vm in cluster.vms if vm.node == node.name)
                u_total = sum(node.storage_free.values()) or 1
                p_used = sum(vm.io_pressure for vm in cluster.vms if vm.node == node.name)
                w_u, w_p = bal.w_io_usage, bal.w_io_psi
                
            usage_loads.append(u_used / u_total if u_total else 0)
            psi_loads.append(p_used / 100.0)
            
        if not usage_loads: return 0.0
        usage_gap = max(usage_loads) - min(usage_loads)
        psi_gap = max(psi_loads) - min(psi_loads)
        
        total_w = w_u + w_p
        return (w_u * usage_gap + w_p * psi_gap) / total_w if total_w else usage_gap

    loads = []
    for node in cluster.nodes:
        if node.maintenance:
            continue
        
        if method == "cpu":
            used = sum(vm.cpu_usage for vm in cluster.vms if vm.node == node.name)
            total = node.cpu_total
        elif method == "cpu_psi":
            used = sum(vm.cpu_pressure for vm in cluster.vms if vm.node == node.name)
            total = 100.0
        elif method == "memory_psi":
            used = sum(vm.memory_pressure for vm in cluster.vms if vm.node == node.name)
            total = 100.0
        elif method == "io_psi":
            used = sum(vm.io_pressure for vm in cluster.vms if vm.node == node.name)
            total = 100.0
        else:  # memory
            used = sum(vm.memory for vm in cluster.vms if vm.node == node.name)
            total = node.memory_total
            
        pct = used / total if total else 0
        loads.append(pct)
    if not loads:
        return 0.0
    return max(loads) - min(loads)


def _resolve_balancing(
    bal: Balancing, current_gap: float
) -> tuple[int, int]:
    """Resolve effective (w_balance, w_stickiness) from balanciness."""
    level = max(1, min(5, bal.balanciness))
    prof_wb, prof_ws, threshold = _BALANCINESS_PROFILES[level]

    w_b = bal.w_balance if bal.w_balance is not None else prof_wb
    w_s = bal.w_stickiness if bal.w_stickiness is not None else prof_ws

    if current_gap < threshold:
        w_b = 0

    return w_b, w_s


def _find_blocking_vms(
    cluster: Cluster, time_limit_s: float
) -> list[str]:
    """Identify VMs that prevent evacuation of the target node."""
    evac_node = cluster.evacuate_node
    if not evac_node:
        return []

    nodes = cluster.nodes
    vms = cluster.vms
    bal = cluster.balancing
    cons = cluster.constraints

    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vms_on_node = [v for v in vms if v.node == evac_node]
    if not vms_on_node:
        return []

    blockers: list[str] = []

    for test_vm in vms_on_node:
        model = cp_model.CpModel()
        y = []
        for j, node in enumerate(nodes):
            y.append(model.new_bool_var(f"y_{test_vm.name}_{node.name}"))

        model.add(sum(y) == 1)
        ej = node_idx[evac_node]
        model.add(y[ej] == 0)

        for j, node in enumerate(nodes):
            if node.maintenance:
                model.add(y[j] == 0)

        for rule in cons.pin:
            if rule["vm"] == test_vm.name:
                allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
                for j in range(len(nodes)):
                    if j not in allowed:
                        model.add(y[j] == 0)

        for j, node in enumerate(nodes):
            if j == ej: continue
            other_load = sum(v.memory for v in vms if v.node == node.name and v.name != test_vm.name)
            avail_mb = (node.memory_total - other_load) // _MB
            if test_vm.memory // _MB > avail_mb:
                model.add(y[j] == 0)

        overcommit_1000 = int(bal.cpu_overcommit * 1000)
        for j, node in enumerate(nodes):
            if j == ej: continue
            other_cpu = sum(v.cpu for v in vms if v.node == node.name and v.name != test_vm.name)
            avail_cpu_1000 = node.cpu_total * overcommit_1000 - other_cpu * 1000
            if test_vm.cpu * 1000 > avail_cpu_1000:
                model.add(y[j] == 0)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(time_limit_s, 5.0)
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            blockers.append(test_vm.name)

    if not blockers:
        blockers = [v.name for v in vms_on_node]

    return blockers


def solve(
    cluster: Cluster,
    time_limit_s: float = 30.0,
    forbidden_placements: list[dict[str, str]] | None = None
) -> Solution:
    """Solve VM placement using CP-SAT."""
    try:
        validate_and_merge_constraints(cluster)
    except RuleConflictError as e:
        return Solution(
            feasible=False, placements={}, migrations=[],
            stats=SolverStats(status=f"RULE_CONFLICT: {str(e)}", objective=0, load_gap=0.0, migration_count=0, wall_time_ms=0)
        )

    model = cp_model.CpModel()
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints
    current_gap = _initial_load_gap(cluster)
    eff_w_balance, eff_w_stickiness = _resolve_balancing(bal, current_gap)
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}

    x = []
    for i, vm in enumerate(vms):
        row = [model.new_bool_var(f"x_{vm.name}_{node.name}") for node in nodes]
        x.append(row)

    if forbidden_placements:
        for forbidden in forbidden_placements:
            literals = [x[vm_idx[v]][node_idx[n]] for v, n in forbidden.items() if v in vm_idx and n in node_idx]
            if literals: model.add(sum(literals) <= len(literals) - 1)

    for i in range(len(vms)): model.add(sum(x[i][j] for j in range(len(nodes))) == 1)

    for j, node in enumerate(nodes):
        if node.maintenance:
            for i in range(len(vms)): model.add(x[i][j] == 0)

    if cluster.evacuate_node:
        evj = node_idx.get(cluster.evacuate_node)
        if evj is not None:
            for i in range(len(vms)): model.add(x[i][evj] == 0)

    ignored_vms = set(cons.ignore)
    for i, vm in enumerate(vms):
        if vm.name in ignored_vms: model.add(x[i][node_idx[vm.node]] == 1)

    # Hard Constraints: RAM, CPU, Storage
    for j, node in enumerate(nodes):
        usable_mb = (node.memory_total - node.memory_reserve) // _MB
        model.add(sum((vms[i].memory // _MB) * x[i][j] for i in range(len(vms))) <= usable_mb)

    overcommit_1000 = int(bal.cpu_overcommit * 1000)
    for j, node in enumerate(nodes):
        usable_cores = max(0, node.cpu_total - node.cpu_reserve)
        model.add(sum(vms[i].cpu * 1000 * x[i][j] for i in range(len(vms))) <= usable_cores * overcommit_1000)

    all_storages = {s for v in vms for s in v.disks.keys()}
    for storage_name in all_storages:
        for j, node in enumerate(nodes):
            usable_mb = max(0, (node.storage_free.get(storage_name, 0) - node.storage_reserve.get(storage_name, 0)) // _MB)
            model.add(sum((vms[i].disks.get(storage_name, 0) // _MB) * x[i][j] for i in range(len(vms))) <= usable_mb)

    # Affinity / Anti-Affinity
    for rule in cons.anti_affinity:
        aa_vms_indices = [vm_idx[name] for name in rule["vms"] if name in vm_idx]
        if len(aa_vms_indices) < 2: continue
        for j in range(len(nodes)): model.add(sum(x[i][j] for i in aa_vms_indices) <= 1)

    for rule in cons.affinity:
        aff_vms = [vm_idx[name] for name in rule["vms"] if name in vm_idx]
        if len(aff_vms) < 2: continue
        for other in aff_vms[1:]:
            for j in range(len(nodes)): model.add(x[aff_vms[0]][j] == x[other][j])

    for rule in cons.pin:
        vi = vm_idx.get(rule["vm"])
        if vi is not None:
            allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
            for j in range(len(nodes)):
                if j not in allowed: model.add(x[vi][j] == 0)

    # ── Objective ──
    method = bal.method
    max_load_val = _LOAD_SCALE * 5
    load_vars = []
    
    if method in ["cpu_smart", "memory_smart", "io_smart"]:
        u_load_vars, p_load_vars = [], []
        if method == "cpu_smart":
            w_u, w_p = bal.w_cpu_usage, bal.w_cpu_psi
        elif method == "memory_smart":
            w_u, w_p = bal.w_mem_usage, bal.w_mem_psi
        else:
            w_u, w_p = bal.w_io_usage, bal.w_io_psi
        
        for j, node in enumerate(nodes):
            if node.maintenance: continue
            
            if method == "cpu_smart":
                cap_u = max(1, node.cpu_total - node.cpu_reserve)
                used_u = sum(int(vms[i].cpu_usage * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif method == "memory_smart":
                cap_u = max(1, (node.memory_total - node.memory_reserve) // _MB)
                used_u = sum((vms[i].memory // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            else: # io_smart
                cap_u = max(1, sum(node.storage_free.values()) // _MB)
                used_u = sum((sum(vms[i].disks.values()) // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            
            u_load_j = model.new_int_var(0, max_load_val, f"u_load_{node.name}")
            model.add_division_equality(u_load_j, used_u, model.new_constant(cap_u))
            u_load_vars.append(u_load_j)

            # PSI selection
            if method == "cpu_smart":
                p_sum = sum(int(vms[i].cpu_pressure * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif method == "memory_smart":
                p_sum = sum(int(vms[i].memory_pressure * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            else:
                p_sum = sum(int(vms[i].io_pressure * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))

            p_load_j = model.new_int_var(0, _LOAD_SCALE, f"p_load_{node.name}")
            model.add_division_equality(p_load_j, p_sum, model.new_constant(100))
            p_load_vars.append(p_load_j)

        usage_gap = model.new_int_var(0, max_load_val, "usage_gap")
        if u_load_vars:
            max_u, min_u = model.new_int_var(0, max_load_val, "max_u"), model.new_int_var(0, max_load_val, "min_u")
            model.add_max_equality(max_u, u_load_vars), model.add_min_equality(min_u, u_load_vars)
            model.add(usage_gap == max_u - min_u)
        else: model.add(usage_gap == 0)

        psi_gap = model.new_int_var(0, _LOAD_SCALE, "psi_gap")
        if p_load_vars:
            max_p, min_p = model.new_int_var(0, _LOAD_SCALE, "max_p"), model.new_int_var(0, _LOAD_SCALE, "min_p")
            model.add_max_equality(max_p, p_load_vars), model.add_min_equality(min_p, p_load_vars)
            model.add(psi_gap == max_p - min_p)
        else: model.add(psi_gap == 0)
            
        load_gap = model.new_int_var(0, 10 * max_load_val, "load_gap")
        model.add(load_gap == w_u * usage_gap + w_p * psi_gap)

    else:
        for j, node in enumerate(nodes):
            if node.maintenance: continue
            if method == "cpu":
                cap = max(1, node.cpu_total - node.cpu_reserve)
                used = sum(int(vms[i].cpu_usage * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif method in ["cpu_psi", "memory_psi", "io_psi"]:
                cap = 100
                if method == "cpu_psi": p_vals = [v.cpu_pressure for v in vms]
                elif method == "memory_psi": p_vals = [v.memory_pressure for v in vms]
                else: p_vals = [v.io_pressure for v in vms]
                used = sum(int(p_vals[i] * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            else: # memory
                cap = max(1, (node.memory_total - node.memory_reserve) // _MB)
                used = sum((vms[i].memory // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            
            lv = model.new_int_var(0, max_load_val, f"load_{node.name}")
            model.add_division_equality(lv, used, model.new_constant(cap))
            load_vars.append(lv)

        load_gap = model.new_int_var(0, max_load_val, "load_gap")
        if load_vars:
            mx, mn = model.new_int_var(0, max_load_val, "mx"), model.new_int_var(0, max_load_val, "mn")
            model.add_max_equality(mx, load_vars), model.add_min_equality(mn, load_vars)
            model.add(load_gap == mx - mn)
        else: model.add(load_gap == 0)

    mig_bools = []
    for i, vm in enumerate(vms):
        if vm.name not in ignored_vms:
            mb = model.new_bool_var(f"mb_{vm.name}")
            model.add(mb == 1 - x[i][node_idx[vm.node]])
            mig_bools.append(mb)

    migration_count = model.new_int_var(0, len(vms), "migration_count")
    if mig_bools: model.add(migration_count == sum(mig_bools))
    else: model.add(migration_count == 0)

    model.minimize(eff_w_balance * load_gap + eff_w_stickiness * migration_count)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    t0 = time.monotonic()
    status = solver.solve(model)
    wall_time_ms = (time.monotonic() - t0) * 1000

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        blocking = _find_blocking_vms(cluster, time_limit_s) if cluster.evacuate_node else []
        return Solution(False, {}, [], SolverStats(solver.status_name(status), 0, 0.0, 0, wall_time_ms), blocking)

    placements = {v.name: nodes[j].name for i, v in enumerate(vms) for j in range(len(nodes)) if solver.value(x[i][j])}
    migrations = [Migration(v.name, v.node, placements[v.name]) for v in vms if placements[v.name] != v.node]

    return Solution(True, placements, migrations, SolverStats(solver.status_name(status), solver.objective_value, solver.value(load_gap) / _LOAD_SCALE if load_vars or method.endswith("smart") else 0.0, len(migrations), wall_time_ms))


def solve_reachable(
    cluster: Cluster,
    total_time_limit_s: float = 60.0,
    max_retries: int = 10,
    quiet: bool = True,
) -> tuple[Solution, MigrationPlan]:
    """Solve VM placement and ensure the solution is reachable."""
    forbidden = []
    last_solution = None
    last_plan = None
    start_time = time.monotonic()

    for attempt in range(max_retries + 1):
        elapsed = time.monotonic() - start_time
        remaining = total_time_limit_s - elapsed
        if remaining <= 0: break

        solution = solve(cluster, time_limit_s=max(1.0, remaining), forbidden_placements=forbidden)
        if not solution.feasible:
            if last_solution:
                import dataclasses
                return dataclasses.replace(last_solution, path_feasible=False), last_plan
            return solution, plan_migrations(cluster, solution)

        plan = plan_migrations(cluster, solution)
        last_solution, last_plan = solution, plan
        if plan.path_feasible: return solution, plan
        if not plan.unbreakable_cycle: break

        cycle_forbidden = {vm: solution.placements[vm] for vm in plan.unbreakable_cycle if vm in solution.placements}
        if not cycle_forbidden: break
        forbidden.append(cycle_forbidden)
        if not quiet: print(f"  [solve_reachable] Attempt {attempt+1}: Cycle {plan.unbreakable_cycle}. Retrying...")

    if last_solution:
        import dataclasses
        return dataclasses.replace(last_solution, path_feasible=False), last_plan
    return solution, plan_migrations(cluster, solution)
