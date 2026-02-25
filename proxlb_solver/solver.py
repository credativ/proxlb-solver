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
# Penalty for violating a soft constraint
_SOFT_PENALTY = 1000000

# VMware DRS-style balanciness profiles
_BALANCINESS_PROFILES = {
    1: (0, 1, 1.0),       # Conservative
    2: (1, 50, 0.25),     # Low
    3: (10, 10, 0.15),    # Moderate
    4: (50, 5, 0.05),     # High
    5: (100, 1, 0.0),     # Aggressive
}


def _initial_load_gap_single(cluster: Cluster, method: str) -> float:
    """Helper to calculate initial gap for a specific smart or base method, considering VM priority."""
    bal = cluster.balancing
    usage_loads = []
    psi_loads = []
    for node in cluster.nodes:
        if node.maintenance: continue
        if method == "cpu_smart" or method == "cpu":
            u_used = sum(vm.cpu_usage * vm.priority for vm in cluster.vms if vm.node == node.name)
            u_total = node.cpu_total
            p_used = sum(vm.cpu_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            w_u, w_p = bal.w_cpu_usage, bal.w_cpu_psi
        elif method == "memory_smart" or method == "memory":
            u_used = sum(vm.memory * vm.priority for vm in cluster.vms if vm.node == node.name)
            u_total = node.memory_total
            p_used = sum(vm.memory_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            w_u, w_p = bal.w_mem_usage, bal.w_mem_psi
        elif method == "io_smart" or method == "io_psi":
            u_used = sum(sum(vm.disks.values()) * vm.priority for vm in cluster.vms if vm.node == node.name)
            u_total = sum(node.storage_free.values()) or 1
            p_used = sum(vm.io_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            w_u, w_p = bal.w_io_usage, bal.w_io_psi
        else: return 0.0
        usage_loads.append(u_used / u_total if u_total else 0)
        psi_loads.append(p_used / 100.0)
    if not usage_loads: return 0.0
    u_gap = max(usage_loads) - min(usage_loads)
    p_gap = max(psi_loads) - min(psi_loads)
    if method.endswith("_smart"):
        total_w = w_u + w_p
        return (w_u * u_gap + w_p * p_gap) / total_w if total_w else u_gap
    return u_gap if method != "cpu_psi" and not method.endswith("_psi") else p_gap


def _initial_load_gap(cluster: Cluster) -> float:
    """Compute the initial load gap using the sum of VM footprints."""
    method = cluster.balancing.method
    bal = cluster.balancing
    if method == "global_smart":
        m_gap = _initial_load_gap_single(cluster, "memory_smart")
        c_gap = _initial_load_gap_single(cluster, "cpu_smart")
        i_gap = _initial_load_gap_single(cluster, "io_smart")
        total_w = bal.w_global_mem + bal.w_global_cpu + bal.w_global_io
        return (bal.w_global_mem * m_gap + bal.w_global_cpu * c_gap + bal.w_global_io * i_gap) / total_w if total_w else m_gap
    if method in ["cpu_smart", "memory_smart", "io_smart"]:
        return _initial_load_gap_single(cluster, method)
    loads = []
    for node in cluster.nodes:
        if node.maintenance: continue
        if method == "cpu": used = sum(vm.cpu_usage * vm.priority for vm in cluster.vms if vm.node == node.name); total = node.cpu_total
        elif method == "cpu_psi": used = sum(vm.cpu_pressure * vm.priority for vm in cluster.vms if vm.node == node.name); total = 100.0
        elif method == "memory_psi": used = sum(vm.memory_pressure * vm.priority for vm in cluster.vms if vm.node == node.name); total = 100.0
        elif method == "io_psi": used = sum(vm.io_pressure * vm.priority for vm in cluster.vms if vm.node == node.name); total = 100.0
        else: used = sum(vm.memory * vm.priority for vm in cluster.vms if vm.node == node.name); total = node.memory_total
        pct = used / total if total else 0
        loads.append(pct)
    return max(loads) - min(loads) if loads else 0.0


def _resolve_balancing(bal: Balancing, current_gap: float) -> tuple[int, int]:
    level = max(1, min(5, bal.balanciness))
    prof_wb, prof_ws, threshold = _BALANCINESS_PROFILES[level]
    w_b = bal.w_balance if bal.w_balance is not None else prof_wb
    w_s = bal.w_stickiness if bal.w_stickiness is not None else prof_ws
    if current_gap < threshold: w_b = 0
    return w_b, w_s


def _find_blocking_vms(cluster: Cluster, time_limit_s: float) -> list[str]:
    evac_node = cluster.evacuate_node
    if not evac_node: return []
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vms_on_node = [v for v in vms if v.node == evac_node]
    if not vms_on_node: return []
    blockers = []
    for test_vm in vms_on_node:
        model = cp_model.CpModel()
        y = [model.new_bool_var(f"y_{test_vm.name}_{n.name}") for n in nodes]
        model.add(sum(y) == 1)
        model.add(y[node_idx[evac_node]] == 0)
        for j, node in enumerate(nodes):
            if node.maintenance: model.add(y[j] == 0)
        for rule in cons.pin:
            if rule["vm"] == test_vm.name:
                allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
                for j in range(len(nodes)):
                    if j not in allowed: model.add(y[j] == 0)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(time_limit_s, 5.0)
        if solver.solve(model) not in (cp_model.OPTIMAL, cp_model.FEASIBLE): blockers.append(test_vm.name)
    return blockers or [v.name for v in vms_on_node]


def solve(cluster: Cluster, time_limit_s: float = 30.0, forbidden_placements: list[dict[str, str]] | None = None) -> Solution:
    try: validate_and_merge_constraints(cluster)
    except RuleConflictError as e: return Solution(False, {}, [], SolverStats(f"RULE_CONFLICT: {str(e)}", 0, 0.0, 0, 0))
    model = cp_model.CpModel()
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}
    x = [[model.new_bool_var(f"x_{v.name}_{n.name}") for n in nodes] for v in vms]
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
    for i, vm in enumerate(vms):
        if vm.name in cons.ignore: model.add(x[i][node_idx[vm.node]] == 1)
    for j, node in enumerate(nodes):
        model.add(sum((vms[i].memory // _MB) * x[i][j] for i in range(len(vms))) <= (node.memory_total - node.memory_reserve) // _MB)
        model.add(sum(vms[i].cpu * 1000 * x[i][j] for i in range(len(vms))) <= max(0, node.cpu_total - node.cpu_reserve) * int(bal.cpu_overcommit * 1000))
    all_storages = {s for v in vms for s in v.disks.keys()}
    for sn in all_storages:
        for j, node in enumerate(nodes):
            model.add(sum((vms[i].disks.get(sn, 0) // _MB) * x[i][j] for i in range(len(vms))) <= max(0, (node.storage_free.get(sn, 0) - node.storage_reserve.get(sn, 0)) // _MB))

    soft_penalties = []
    for rule in cons.anti_affinity:
        indices = [vm_idx[n] for n in rule["vms"] if n in vm_idx]
        if len(indices) < 2: continue
        if rule.get("hard", True):
            for j in range(len(nodes)): model.add(sum(x[i][j] for i in indices) <= 1)
        else:
            for j in range(len(nodes)):
                violated = model.new_bool_var(f"soft_aa_{rule.get('name','na')}_{nodes[j].name}")
                model.add(sum(x[i][j] for i in indices) <= 1 + len(indices) * violated)
                soft_penalties.append(violated)
    for rule in cons.affinity:
        indices = [vm_idx[n] for n in rule["vms"] if n in vm_idx]
        if len(indices) < 2: continue
        if rule.get("hard", True):
            for other in indices[1:]:
                for j in range(len(nodes)): model.add(x[indices[0]][j] == x[other][j])
        else:
            for other in indices[1:]:
                for j in range(len(nodes)):
                    violated = model.new_bool_var(f"soft_aff_{rule.get('name','na')}_{vms[other].name}_{nodes[j].name}")
                    model.add(x[indices[0]][j] - x[other][j] <= violated)
                    model.add(x[other][j] - x[indices[0]][j] <= violated)
                    soft_penalties.append(violated)
    for rule in cons.pin:
        vi = vm_idx.get(rule["vm"])
        if vi is not None:
            allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
            for j in range(len(nodes)):
                if j not in allowed: model.add(x[vi][j] == 0)

    # ── Objective ──
    method = bal.method
    max_load_val = _LOAD_SCALE * 15
    
    def add_smart_gap(m_type):
        u_vars, p_vars = [], []
        for j, node in enumerate(nodes):
            if node.maintenance: continue
            if m_type == "cpu":
                cap, used = max(1, node.cpu_total - node.cpu_reserve), sum(int(vms[i].cpu_usage * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif m_type == "memory":
                cap, used = max(1, (node.memory_total - node.memory_reserve) // _MB), sum((vms[i].memory * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            else: # io
                cap, used = max(1, sum(node.storage_free.values()) // _MB), sum((sum(vms[i].disks.values()) * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            uv = model.new_int_var(0, max_load_val, f"u_{m_type}_{node.name}")
            model.add_division_equality(uv, used, model.new_constant(cap)), u_vars.append(uv)
            p_sum = sum(int((vms[i].cpu_pressure if m_type == "cpu" else vms[i].memory_pressure if m_type == "memory" else vms[i].io_pressure) * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            pv = model.new_int_var(0, 3 * _LOAD_SCALE, f"p_{m_type}_{node.name}")
            model.add_division_equality(pv, p_sum, model.new_constant(100)), p_vars.append(pv)
        ug = model.new_int_var(0, max_load_val, f"ug_{m_type}")
        if u_vars:
            mxu, mnu = model.new_int_var(0, max_load_val, f"mxu_{m_type}"), model.new_int_var(0, max_load_val, f"mnu_{m_type}")
            model.add_max_equality(mxu, u_vars), model.add_min_equality(mnu, u_vars), model.add(ug == mxu - mnu)
        else: model.add(ug == 0)
        pg = model.new_int_var(0, 3 * _LOAD_SCALE, f"pg_{m_type}")
        if p_vars:
            mxp, mnp = model.new_int_var(0, 3 * _LOAD_SCALE, f"mxp_{m_type}"), model.new_int_var(0, 3 * _LOAD_SCALE, f"mnp_{m_type}")
            model.add_max_equality(mxp, p_vars), model.add_min_equality(mnp, p_vars), model.add(pg == mxp - mnp)
        else: model.add(pg == 0)
        return ug, pg

    if method == "global_smart":
        ug_m, pg_m = add_smart_gap("memory")
        ug_c, pg_c = add_smart_gap("cpu")
        ug_i, pg_i = add_smart_gap("io")
        load_gap = model.new_int_var(0, 50 * max_load_val, "global_gap")
        model.add(load_gap == bal.w_global_mem * (bal.w_mem_usage * ug_m + bal.w_mem_psi * pg_m) + 
                          bal.w_global_cpu * (bal.w_cpu_usage * ug_c + bal.w_cpu_psi * pg_c) + 
                          bal.w_global_io * (bal.w_io_usage * ug_i + bal.w_io_psi * pg_i))
    elif method in ["cpu_smart", "memory_smart", "io_smart"]:
        ug, pg = add_smart_gap(method.split("_")[0])
        w_u, w_p = (bal.w_cpu_usage, bal.w_cpu_psi) if method == "cpu_smart" else (bal.w_mem_usage, bal.w_mem_psi) if method == "memory_smart" else (bal.w_io_usage, bal.w_io_psi)
        load_gap = model.new_int_var(0, 10 * max_load_val, "smart_gap")
        model.add(load_gap == w_u * ug + w_p * pg)
    else:
        lvars = []
        for j, node in enumerate(nodes):
            if node.maintenance: continue
            if method == "cpu": cap, used = max(1, node.cpu_total - node.cpu_reserve), sum(int(vms[i].cpu_usage * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif method in ["cpu_psi", "memory_psi", "io_psi"]:
                cap, used = 100, sum(int((vms[i].cpu_pressure if method == "cpu_psi" else vms[i].memory_pressure if method == "memory_psi" else vms[i].io_pressure) * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            else: cap, used = max(1, (node.memory_total - node.memory_reserve) // _MB), sum((vms[i].memory * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            lv = model.new_int_var(0, max_load_val, f"l_{node.name}")
            model.add_division_equality(lv, used, model.new_constant(cap)), lvars.append(lv)
        load_gap = model.new_int_var(0, max_load_val, "gap")
        if lvars:
            mx, mn = model.new_int_var(0, max_load_val, "mx"), model.new_int_var(0, max_load_val, "mn")
            model.add_max_equality(mx, lvars), model.add_min_equality(mn, lvars), model.add(load_gap == mx - mn)
        else: model.add(load_gap == 0)

    mig_count_list = []
    for i, vm in enumerate(vms):
        if vm.name not in set(cons.ignore):
            m_var = model.new_bool_var(f"mvar_{vm.name}")
            model.add(m_var == 1 - x[i][node_idx[vm.node]]), mig_count_list.append(m_var)
    migration_count = model.new_int_var(0, len(vms), "m_cnt")
    if mig_count_list: model.add(migration_count == sum(mig_count_list))
    else: model.add(migration_count == 0)

    penalty_total = model.new_int_var(0, 100 * _SOFT_PENALTY, "penalty_total")
    if soft_penalties: model.add(penalty_total == sum(soft_penalties) * _SOFT_PENALTY)
    else: model.add(penalty_total == 0)

    current_gap = _initial_load_gap(cluster)
    eff_wb, eff_ws = _resolve_balancing(bal, current_gap)
    model.minimize(eff_wb * load_gap + eff_ws * migration_count + penalty_total)
    
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    t0 = time.monotonic()
    status = solver.solve(model)
    wall_ms = (time.monotonic() - t0) * 1000
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution(False, {}, [], SolverStats(solver.status_name(status), 0, 0.0, 0, wall_ms), _find_blocking_vms(cluster, time_limit_s) if cluster.evacuate_node else [])
    placements = {v.name: nodes[j].name for i, v in enumerate(vms) for j in range(len(nodes)) if solver.value(x[i][j])}
    migrations = [Migration(v.name, v.node, placements[v.name]) for v in vms if placements[v.name] != v.node]
    return Solution(True, placements, migrations, SolverStats(solver.status_name(status), solver.objective_value, solver.value(load_gap) / _LOAD_SCALE, len(migrations), wall_ms))


def solve_reachable(cluster: Cluster, total_time_limit_s: float = 60.0, max_retries: int = 10, quiet: bool = True) -> tuple[Solution, MigrationPlan]:
    import dataclasses
    forbidden, last_sol, last_plan, start = [], None, None, time.monotonic()
    for attempt in range(max_retries + 1):
        rem = total_time_limit_s - (time.monotonic() - start)
        if rem <= 0: break
        sol = solve(cluster, time_limit_s=max(1.0, rem), forbidden_placements=forbidden)
        if not sol.feasible:
            if last_sol:
                return dataclasses.replace(last_sol, path_feasible=False, reachability_attempts=attempt + 1), last_plan
            return sol, plan_migrations(cluster, sol)
        plan = plan_migrations(cluster, sol)
        last_sol, last_plan = sol, plan
        if plan.path_feasible:
            return dataclasses.replace(sol, reachability_attempts=attempt + 1), plan
        if not plan.unbreakable_cycle: break
        forbidden.append({vm: sol.placements[vm] for vm in plan.unbreakable_cycle if vm in sol.placements})
        if not quiet: print(f"  [solve_reachable] Attempt {attempt+1}: Cycle {plan.unbreakable_cycle}. Retrying...")
    if last_sol:
        return dataclasses.replace(last_sol, path_feasible=False, reachability_attempts=attempt + 1), last_plan
    return sol, plan_migrations(cluster, sol)
