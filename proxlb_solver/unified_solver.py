"""
Unified CP-SAT Solver - Production Grade.
Models VM placement across discrete time steps with iterative deepening.
Optimized weights to prevent 'fake success' via Slack and ensure high quality.
"""

from __future__ import annotations
import time
import logging
import dataclasses
from ortools.sat.python import cp_model
from collections import defaultdict

from .models import Cluster, Node, VM, Solution, Migration, SolverStats, MigrationPlan, MigrationStep

logger = logging.getLogger("ProxLB")

_MB = 1024 * 1024
_LOAD_SCALE = 10000
# EXTREME penalty: Slack must be more expensive than any possible rebalancing gain.
_SLACK_PENALTY = 1000000000 

def _get_pve_affinity_groups(cluster: Cluster) -> list[set[str]]:
    groups = []
    for rule in cluster.constraints.affinity:
        if rule.get("origin") == "pve" and rule.get("hard", True):
            groups.append(set(rule["vms"]))
    return groups

def _solve_fixed_t(cluster: Cluster, T: int, time_limit_s: float) -> tuple[Solution, MigrationPlan, dict]:
    model = cp_model.CpModel()
    nodes, vms, bal = cluster.nodes, cluster.vms, cluster.balancing
    
    # ── Weights & Thresholds ──
    from .solver import _initial_load_gap, _resolve_balancing
    current_gap = _initial_load_gap(cluster)
    eff_wb, eff_ws = _resolve_balancing(bal, current_gap, cluster)
    
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}
    pve_groups = _get_pve_affinity_groups(cluster)
    
    # ── Variables ──
    x = {} 
    for i in range(len(vms)):
        for j in range(len(nodes)):
            for t in range(T + 1):
                x[i, j, t] = model.new_bool_var(f"x_v{i}_n{j}_t{t}")

    for i, vm in enumerate(vms):
        for j in range(len(nodes)):
            model.add(x[i, j, 0] == (1 if j == node_idx[vm.node] else 0))

    m = {} 
    for i in range(len(vms)):
        for t in range(T):
            m[i, t] = model.new_bool_var(f"m_v{i}_t{t}")
            for j in range(len(nodes)):
                model.add(x[i, j, t] - x[i, j, t+1] <= m[i, t])
                model.add(x[i, j, t+1] - x[i, j, t] <= m[i, t])

    # ── Soft Capacity with Slack ──
    total_slack_vars = []
    for t in range(1, T + 1):
        for j, node in enumerate(nodes):
            if node.maintenance or node.name == cluster.evacuate_node:
                for i in range(len(vms)): model.add(x[i, j, t] == 0)
                continue
            
            cap_mib = (node.memory_total - node.memory_reserve) // _MB
            usage_mib = sum((vms[i].memory // _MB) * x[i, j, t] for i in range(len(vms)))
            slack_mem = model.new_int_var(0, 1000000, f"slack_mem_n{j}_t{t}")
            model.add(usage_mib <= cap_mib + slack_mem)
            total_slack_vars.append(slack_mem)
            
            usable_cpu = max(0, node.cpu_total - node.cpu_reserve)
            cap_cpu_scaled = int(usable_cpu * bal.cpu_overcommit * 1000)
            usage_cpu_scaled = sum(vms[i].cpu * 1000 * x[i, j, t] for i in range(len(vms)))
            slack_cpu = model.new_int_var(0, 1000000, f"slack_cpu_n{j}_t{t}")
            model.add(usage_cpu_scaled <= cap_cpu_scaled + slack_cpu)
            total_slack_vars.append(slack_cpu)

    # ── Constraints ──
    for t in range(1, T + 1):
        for i in range(len(vms)): model.add(sum(x[i, j, t] for j in range(len(nodes))) == 1)
        for rule in cluster.constraints.pin:
            vi = vm_idx.get(rule["vm"])
            if vi is not None:
                allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
                for j in range(len(nodes)):
                    if j not in allowed: model.add(x[vi, j, t] == 0)
        
        for group in pve_groups:
            indices = [vm_idx[vn] for vn in group if vn in vm_idx]
            if len(indices) < 2: continue
            for other in indices[1:]:
                for j in range(len(nodes)): model.add(x[indices[0], j, t] == x[other, j, t])

        if t == T:
            for rule in cluster.constraints.affinity:
                if rule.get("origin") == "pve": continue 
                if not rule.get("hard", True): continue
                indices = [vm_idx[vn] for vn in rule["vms"] if vn in vm_idx]
                if len(indices) < 2: continue
                for other in indices[1:]:
                    for j in range(len(nodes)): model.add(x[indices[0], j, T] == x[other, j, T])

    max_p, max_i = bal.max_parallel_migrations or 10, bal.max_node_inflow or 5
    for t in range(T):
        model.add(sum(m[i, t] for i in range(len(vms))) <= max_p)
        for j in range(len(nodes)):
            lands = [model.new_bool_var(f"l_v{i}_n{j}_t{t}") for i in range(len(vms))]
            for i in range(len(vms)): model.add(x[i, j, t+1] - x[i, j, t] <= lands[i])
            model.add(sum(lands) <= max_i)

    # ── Objective ──
    def add_res_gap(m_type, use_psi=False):
        node_vars = []
        for j, node in enumerate(nodes):
            if node.maintenance or node.name == cluster.evacuate_node: continue
            if use_psi:
                cap = 100
                res_p = lambda v: v.cpu_pressure if m_type == "cpu" else v.memory_pressure if m_type == "memory" else v.io_pressure
                used = sum(int(res_p(vms[i]) * vms[i].priority * _LOAD_SCALE) * x[i, j, T] for i in range(len(vms)))
            else:
                if m_type == "cpu":
                    cap = max(1, node.cpu_total - node.cpu_reserve)
                    if bal.mode == "assigned": used = sum(vms[i].cpu * vms[i].priority * _LOAD_SCALE * x[i, j, T] for i in range(len(vms)))
                    else: used = sum(int(vms[i].cpu_usage * vms[i].priority * _LOAD_SCALE) * x[i, j, T] for i in range(len(vms)))
                elif m_type == "memory":
                    cap = max(1, (node.memory_total - node.memory_reserve) // _MB)
                    used = sum((vms[i].memory * vms[i].priority // _MB) * _LOAD_SCALE * x[i, j, T] for i in range(len(vms)))
                else: # disk
                    cap = max(1, sum(node.storage_free.values()) // _MB)
                    used = sum((sum(vms[i].disks.values()) * vms[i].priority // _MB) * _LOAD_SCALE * x[i, j, T] for i in range(len(vms)))
            lv = model.new_int_var(0, _LOAD_SCALE * 100, f"l_{m_type}_{j}")
            model.add_division_equality(lv, used, model.new_constant(cap)), node_vars.append(lv)
        gap = model.new_int_var(0, _LOAD_SCALE * 100, f"gap_{m_type}")
        if node_vars:
            mx, mn = model.new_int_var(0, _LOAD_SCALE * 100, f"mx_{m_type}"), model.new_int_var(0, _LOAD_SCALE * 100, f"mn_{m_type}")
            model.add_max_equality(mx, node_vars), model.add_min_equality(mn, node_vars), model.add(gap == mx - mn)
        else: model.add(gap == 0)
        return gap

    method = bal.method
    if method == "global_smart":
        ug_m, pg_m = add_res_gap("memory"), add_res_gap("memory", True)
        ug_c, pg_c = add_res_gap("cpu"), add_res_gap("cpu", True)
        ug_i, pg_i = add_res_gap("io"), add_res_gap("io", True)
        w_gm, w_gc, w_gi = bal.w_global_mem or 10, bal.w_global_cpu or 1, bal.w_global_io or 1
        w_mu, w_mp = bal.w_mem_usage or 1, bal.w_mem_psi or 1
        w_cu, w_cp = bal.w_cpu_usage or 1, bal.w_cpu_psi or 1
        w_iu, w_ip = bal.w_io_usage or 1, bal.w_io_psi or 1
        load_gap = model.new_int_var(0, 50 * _LOAD_SCALE * 100, "global_gap")
        model.add(load_gap == w_gm * (w_mu * ug_m + w_mp * pg_m) + w_gc * (w_cu * ug_c + w_cp * pg_c) + w_gi * (w_iu * ug_i + w_ip * pg_i))
    elif method.endswith("_smart"):
        res = method.split("_")[0]
        ug, pg = add_res_gap(res), add_res_gap(res, True)
        w_u = getattr(bal, f"w_{res if res != 'io' else 'io'}_usage", 1) or 1
        w_p = getattr(bal, f"w_{res if res != 'io' else 'io'}_psi", 1) or 1
        load_gap = model.new_int_var(0, 10 * _LOAD_SCALE * 100, "smart_gap")
        model.add(load_gap == w_u * ug + w_p * pg)
    else:
        load_gap = add_res_gap(method.split("_")[0] if "_" in method else method, method.endswith("_psi") or bal.mode == "psi")

    total_migs_weighted = sum(m[i, t] * (t + 1) for i in range(len(vms)) for t in range(T))
    penalty_slack = sum(total_slack_vars) * _SLACK_PENALTY
    model.minimize(10000 * eff_wb * load_gap + eff_ws * total_migs_weighted + penalty_slack)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    t0 = time.monotonic(); status = solver.solve(model); dur = (time.monotonic()-t0)*1000
    slack_val = sum(solver.value(s) for s in total_slack_vars) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0
    bench = {"status": solver.status_name(status), "duration_ms": dur, "branches": solver.num_branches, "conflicts": solver.num_conflicts, "gap": solver.value(load_gap) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0, "slack": slack_val}
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution(False, {}, [], SolverStats(solver.status_name(status), 0, 0, 0, dur)), MigrationPlan([], [], []), bench
    steps = []
    for t in range(T):
        step_migs = []
        for i in range(len(vms)):
            if solver.value(m[i, t]):
                src = next(nodes[j].name for j in range(len(nodes)) if solver.value(x[i, j, t]))
                dst = next(nodes[j].name for j in range(len(nodes)) if solver.value(x[i, j, t + 1]))
                step_migs.append(Migration(vms[i].name, src, dst))
        if step_migs: steps.append(MigrationStep(t + 1, step_migs, len(step_migs) > 1))
    final_placements = {v.name: nodes[j].name for i, v in enumerate(vms) for j in range(len(nodes)) if solver.value(x[i, j, T])}
    all_migs = [mig for s in steps for mig in s.migrations]
    sol = Solution(True, final_placements, all_migs, SolverStats(solver.status_name(status), int(solver.objective_value), solver.value(load_gap)/_LOAD_SCALE, len(all_migs), dur))
    return sol, MigrationPlan(steps, [], []), bench

def solve_unified(cluster: Cluster, time_limit_s: float = 30.0) -> tuple[Solution, MigrationPlan]:
    start_time = time.monotonic()
    overall_bench = []
    for T in [1, 2, 4, 8, 16, 24, 32]: # Deepening to T=32
        rem = time_limit_s - (time.monotonic() - start_time)
        if rem <= 2.0: break
        sol, plan, bench = _solve_fixed_t(cluster, T, rem)
        bench["steps"] = T
        overall_bench.append(bench)
        if sol.feasible:
            new_stats = dataclasses.replace(sol.stats, benchmark=overall_bench)
            return dataclasses.replace(sol, stats=new_stats), plan
    return Solution(False, {}, [], SolverStats("INFEASIBLE", 0, 0, 0, (time.monotonic()-start_time)*1000, benchmark=overall_bench)), MigrationPlan([], [], [])
