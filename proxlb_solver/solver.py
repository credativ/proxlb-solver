"""
CP-SAT based VM placement solver.

This module translates the cluster state into a mathematical optimization
problem using Google OR-Tools CP-SAT.

IMPORTANT CONCEPT:
CP-SAT is an integer solver. It doesn't handle floats (like 0.5 cores or 12.5% PSI).
To overcome this, we scale all percentages and fractional values by _LOAD_SCALE
(10,000).
- 100% becomes 10,000.
- 0.5% becomes 50.
- 1.0 cores becomes 10,000 (when using usage metrics).
"""

from __future__ import annotations
import time
import logging
from typing import Any, Callable
from ortools.sat.python import cp_model

from .models import Balancing, Cluster, Method, Migration, Resource, Solution, SolverStats, MigrationPlan
from .planner import plan_migrations
from .validator import validate_and_merge_constraints, RuleConflictError

# Logger for diagnostic information
logger = logging.getLogger("ProxLB")

# Scale factors for integer arithmetic
_MB = 1024 * 1024
_LOAD_SCALE = 10000
_SOFT_PENALTY = 1000000 # Math cost for violating one soft constraint (affinity/anti-affinity)

# VMware DRS-style balanciness profiles: (w_balance, w_stickiness, threshold)
# - w_balance: Weight given to improving the cluster spread.
# - w_stickiness: Penalty for moving a VM (to avoid "ping-pong" migrations).
# - threshold: The minimum load gap required to trigger any migration.
_BALANCINESS_PROFILES = {
    1: (0, 1, 1.0),       # Conservative: Never move unless forced by constraints.
    2: (1, 50, 0.25),     # Low: Only move for very large imbalances.
    3: (10, 10, 0.15),    # Moderate: Balanced approach.
    4: (50, 5, 0.05),     # High: Actively seek better balance.
    5: (100, 1, 0.0),     # Aggressive: Move for even the slightest improvement.
}


def _get_node_load_and_capacity(cluster: Cluster, node_name: str, resource_type: Resource) -> tuple[float, float]:
    """
    Calculates raw usage and total capacity for a node and resource type.

    This function respects the balancing mode (Used vs Assigned) and applies
    priority weighting to the VM footprints.

    Returns:
        (weighted_usage, capacity) as floats.
    """
    bal = cluster.balancing
    node = next(n for n in cluster.nodes if n.name == node_name)
    vms_on_node = [vm for vm in cluster.vms if vm.node == node_name]

    usage: float
    if resource_type is Resource.Cpu:
        capacity = max(1.0, float(node.cpu_total - node.cpu_reserve))
        if bal.mode == "assigned":
            # Assigned Mode: Use sum of configured vCPUs
            usage = float(sum(vm.cpu * vm.priority for vm in vms_on_node))
        else:
            # Used Mode (Default): Use sum of actual CPU core usage
            usage = sum(vm.cpu_usage * vm.priority for vm in vms_on_node)

    elif resource_type is Resource.Memory:
        capacity = max(1.0, float(node.memory_total - node.memory_reserve))
        # RAM balancing always uses configured allocation (most critical resource)
        usage = float(sum(vm.memory * vm.priority for vm in vms_on_node))

    else:  # Resource.Disk
        # Disk balancing looks at the sum of all local virtual disks
        capacity = max(1.0, float(sum(node.storage_free.values())))
        usage = float(sum(sum(vm.disks.values()) * vm.priority for vm in vms_on_node))

    return usage, capacity


def _initial_load_gap_single(cluster: Cluster, resource_type: Resource, use_psi: bool = False) -> float:
    """
    Calculates the load gap (Spread) for a specific resource type.
    The gap is defined as the difference between the most loaded and least loaded node.

    If use_psi is True, Pressure Stall Information is used (relative to 100% capacity).
    Otherwise, standard usage or assigned metrics are used.
    """
    loads = []
    for node in cluster.nodes:
        if node.maintenance: continue

        usage: float
        capacity: float
        if use_psi:
            # Pressure is always relative to a fixed 100% capacity per node
            vms_on_node = [vm for vm in cluster.vms if vm.node == node.name]
            usage = float(sum(vm.pressure(resource_type) * vm.priority for vm in vms_on_node))
            capacity = 100.0
        else:
            usage, capacity = _get_node_load_and_capacity(cluster, node.name, resource_type)

        loads.append(usage / capacity)

    return max(loads) - min(loads) if loads else 0.0


def _initial_load_gap(cluster: Cluster) -> float:
    """
    Computes the overall initial load gap based on the selected balancing method.
    Used to decide if the cluster is already 'balanced enough' according to
    the balanciness profile.
    """
    bal = cluster.balancing
    method = bal.method

    # Handle multi-resource 'smart' modes
    if method is Method.GlobalSmart:
        m_gap = _initial_load_gap_single(cluster, Resource.Memory)
        c_gap = _initial_load_gap_single(cluster, Resource.Cpu)
        i_gap = _initial_load_gap_single(cluster, Resource.Disk)
        total_w = bal.w_global_mem + bal.w_global_cpu + bal.w_global_io
        return (bal.w_global_mem * m_gap + bal.w_global_cpu * c_gap + bal.w_global_io * i_gap) / total_w if total_w else m_gap

    res = method.resource
    if method.is_smart:
        u_gap = _initial_load_gap_single(cluster, res, use_psi=False)
        p_gap = _initial_load_gap_single(cluster, res, use_psi=True)
        w_u, w_p = _smart_weights(bal, res)
        return (w_u * u_gap + w_p * p_gap) / (w_u + w_p) if (w_u + w_p) else u_gap

    # Standard single-resource modes
    use_psi = method.is_psi or bal.mode == "psi"
    return _initial_load_gap_single(cluster, res, use_psi=use_psi)


def _smart_weights(bal: Balancing, resource: Resource) -> tuple[int, int]:
    """(usage weight, pressure weight) for a ``*_smart`` method on *resource*."""
    if resource is Resource.Cpu:
        return bal.w_cpu_usage, bal.w_cpu_psi
    if resource is Resource.Memory:
        return bal.w_mem_usage, bal.w_mem_psi
    return bal.w_io_usage, bal.w_io_psi


def _resolve_balancing(bal: Balancing, current_gap: float, cluster: Cluster) -> tuple[int, int]:
    """
    Translates user-facing configuration (Balanciness, Thresholds) into
    optimizer weights.

    If thresholds are not reached or the gap is too small, rebalancing
    is suppressed (weight_balance = 0).
    """
    level = max(1, min(5, bal.balanciness))
    prof_wb, prof_ws, gap_threshold = _BALANCINESS_PROFILES[level]

    weight_balance = bal.w_balance if bal.w_balance is not None else prof_wb
    weight_stickiness = bal.w_stickiness if bal.w_stickiness is not None else prof_ws

    # 1. Gap Check: Is rebalancing worth the effort?
    if current_gap < gap_threshold:
        weight_balance = 0

    # 2. Absolute Utilization Threshold Check:
    # Only rebalance if at least one node exceeds the configured threshold.
    any_threshold_exceeded = False
    active_thresholds = False

    threshold_mapping = [
        (Resource.Cpu, bal.cpu_threshold),
        (Resource.Memory, bal.memory_threshold),
        (Resource.Disk, bal.disk_threshold)
    ]

    for res_type, limit in threshold_mapping:
        if limit is None: continue
        active_thresholds = True
        for node in cluster.nodes:
            if node.maintenance: continue

            # Threshold check always uses raw values (no priority inflation)
            vms_on_node = [vm for vm in cluster.vms if vm.node == node.name]
            if res_type is Resource.Cpu:
                usage, cap = sum(vm.cpu_usage for vm in vms_on_node), node.cpu_total
            elif res_type is Resource.Memory:
                usage, cap = sum(vm.memory for vm in vms_on_node), node.memory_total
            else: # disk
                usage, cap = sum(sum(vm.disks.values()) for vm in vms_on_node), sum(node.storage_free.values()) or 1

            if cap > 0 and (usage / cap) * 100 > limit:
                any_threshold_exceeded = True; break
        if any_threshold_exceeded: break

    if active_thresholds and not any_threshold_exceeded:
        weight_balance = 0

    return weight_balance, weight_stickiness


def _find_blocking_vms(cluster: Cluster, time_limit_s: float) -> list[str]:
    """
    Small diagnostic solver: If a node evacuation fails, this function
    tries to move each VM on that node individually. If a single VM cannot
    be moved even when all others are ignored, it's a hard blocker (pinning).
    """
    evac_node = cluster.evacuate_node
    if not evac_node: return []
    nodes, vms, cons = cluster.nodes, cluster.vms, cluster.constraints
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


_Guard = Callable[[dict[str, Any]], "cp_model.IntVar"]


def _add_hard_constraints(model: cp_model.CpModel, x: list[list[cp_model.IntVar]],
                          cluster: Cluster, guard: _Guard | None = None) -> None:
    """Add every hard placement rule of *cluster* to *model*.

    This is the single definition of the hard constraints, shared by solve()
    and explain_infeasibility() so the two models cannot drift apart.

    Without *guard*, every rule is added unconditionally. With *guard*, each
    blameable rule group calls ``guard(description)`` once and is enforced only
    if the returned literal is true. The exactly-one-node placement constraint
    is structural and never guarded.
    """
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}

    def rule(description: dict[str, Any]) -> Callable[[cp_model.Constraint], None]:
        lit = guard(description) if guard is not None else None

        def enforce(ct: cp_model.Constraint) -> None:
            if lit is not None:
                ct.only_enforce_if(lit)
        return enforce

    # Structural: every VM lands on exactly one node.
    for i in range(len(vms)):
        model.add(sum(x[i][j] for j in range(len(nodes))) == 1)

    # Maintenance nodes are forbidden.
    for j, node in enumerate(nodes):
        if node.maintenance:
            enforce = rule({"type": "maintenance", "node": node.name})
            for i in range(len(vms)):
                enforce(model.add(x[i][j] == 0))

    if cluster.evacuate_node:
        evj = node_idx.get(cluster.evacuate_node)
        if evj is not None:
            enforce = rule({"type": "evacuate", "node": cluster.evacuate_node})
            for i in range(len(vms)):
                enforce(model.add(x[i][evj] == 0))

    # Ignored guests must stay on their source node. If that node is gone,
    # the rule cannot be satisfied.
    for i, vm in enumerate(vms):
        if vm.name in cons.ignore:
            enforce = rule({"type": "ignore", "vm": vm.name, "node": vm.node})
            if vm.node in node_idx:
                enforce(model.add(x[i][node_idx[vm.node]] == 1))
            else:
                enforce(model.add(sum(x[i]) == 0))

    # Resource capacity (memory and storage scaled to MiB, CPU in milli-cores
    # considering overcommit). A reserve larger than the total leaves the
    # node unusable rather than negative.
    for j, node in enumerate(nodes):
        enforce = rule({"type": "capacity_memory", "node": node.name})
        cap_mib = max(0, (node.memory_total - node.memory_reserve) // _MB)
        enforce(model.add(sum((vms[i].memory // _MB) * x[i][j] for i in range(len(vms))) <= cap_mib))

        enforce = rule({"type": "capacity_cpu", "node": node.name})
        usable_cpu = max(0, node.cpu_total - node.cpu_reserve)
        cap_mcpu = usable_cpu * int(bal.cpu_overcommit * 1000)
        enforce(model.add(sum(vms[i].cpu * 1000 * x[i][j] for i in range(len(vms))) <= cap_mcpu))

    all_storages = sorted({s for v in vms for s in v.disks.keys()})
    for sn in all_storages:
        for j, node in enumerate(nodes):
            enforce = rule({"type": "capacity_storage", "node": node.name, "pool": sn})
            cap_mib = max(0, (node.storage_free.get(sn, 0) - node.storage_reserve.get(sn, 0)) // _MB)
            enforce(model.add(sum((vms[i].disks.get(sn, 0) // _MB) * x[i][j] for i in range(len(vms))) <= cap_mib))

    for r in cons.anti_affinity:
        indices = [vm_idx[n] for n in r["vms"] if n in vm_idx]
        if len(indices) < 2 or not r.get("hard", True):
            continue
        enforce = rule({"type": "anti_affinity", "name": r.get("name"), "vms": list(r["vms"])})
        for j in range(len(nodes)):
            enforce(model.add(sum(x[i][j] for i in indices) <= 1))

    for r in cons.affinity:
        indices = [vm_idx[n] for n in r["vms"] if n in vm_idx]
        if len(indices) < 2 or not r.get("hard", True):
            continue
        enforce = rule({"type": "affinity", "name": r.get("name"), "vms": list(r["vms"])})
        for other in indices[1:]:
            for j in range(len(nodes)):
                enforce(model.add(x[indices[0]][j] == x[other][j]))

    for r in cons.pin:
        vi = vm_idx.get(r["vm"])
        if vi is None:
            continue
        enforce = rule({"type": "pin", "vm": r["vm"], "nodes": list(r["nodes"])})
        allowed = {node_idx[n] for n in r["nodes"] if n in node_idx}
        for j in range(len(nodes)):
            if j not in allowed:
                enforce(model.add(x[vi][j] == 0))


def explain_infeasibility(cluster: Cluster, time_limit_s: float = 10.0) -> list[dict[str, Any]]:
    """Return a structured blame list explaining why *cluster* is infeasible.

    Builds a feasibility-only CP-SAT model from the same hard constraints as
    solve() (via _add_hard_constraints), guarding each rule with an assumption
    literal. CP-SAT then reports a subset of literals that suffices to prove
    infeasibility. That subset is not guaranteed to be minimal, so a deletion
    pass re-solves without each rule in turn and drops any rule the rest can
    prove infeasibility without.

    If the deletion pass completes within *time_limit_s*, the result is
    minimal: removing any one listed rule makes the model feasible. If time
    runs out, the result is still sufficient but may contain extra rules.

    Returns an empty list when the feasibility model is actually satisfiable
    (e.g. the original infeasibility was caused by a planner feedback term
    that this function does not model), or when the first solve does not
    finish within the time limit.
    """
    nodes, vms = cluster.nodes, cluster.vms
    if not nodes or not vms:
        return []

    model = cp_model.CpModel()
    x = [[model.new_bool_var(f"x_{v.name}_{n.name}") for n in nodes] for v in vms]

    blame_by_index: dict[int, dict[str, Any]] = {}
    lit_by_index: dict[int, cp_model.IntVar] = {}

    def guard(description: dict[str, Any]) -> cp_model.IntVar:
        lit = model.new_bool_var(f"a_{len(blame_by_index)}")
        blame_by_index[lit.index] = description
        lit_by_index[lit.index] = lit
        return lit

    _add_hard_constraints(model, x, cluster, guard)

    deadline = time.monotonic() + max(1.0, min(time_limit_s, 10.0))

    def solve_assuming(indices: list[int]) -> tuple[cp_model.CpSolverStatus, list[int]]:
        """Solve with *indices* assumed true; return (status, core indices)."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return cp_model.UNKNOWN, []
        model.clear_assumptions()
        model.add_assumptions([lit_by_index[k] for k in indices])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        status = solver.solve(model)
        if status != cp_model.INFEASIBLE:
            return status, []
        return status, [k for k in solver.sufficient_assumptions_for_infeasibility() if k in lit_by_index]

    status, core = solve_assuming(list(lit_by_index))
    if status != cp_model.INFEASIBLE:
        return []
    if not core:
        core = list(lit_by_index)

    # Deletion pass: try dropping each rule; keep it only if the rest is feasible.
    i = 0
    while i < len(core):
        trial = core[:i] + core[i + 1:]
        status, sub = solve_assuming(trial)
        if status == cp_model.INFEASIBLE:
            keep = set(sub) if sub else set(trial)
            core = [k for k in trial if k in keep]
            i = min(i, len(core))
        else:
            # Feasible without it (needed), or out of time (keep, stay sufficient).
            i += 1

    return [blame_by_index[k] for k in core]


def solve(cluster: Cluster, time_limit_s: float = 30.0, forbidden_placements: list[dict[str, str]] | None = None) -> Solution:
    """
    The Core Solver. Translates the cluster into a CP-SAT model and finds
    the mathematically optimal guest placement.

    Hard constraints live in _add_hard_constraints(), which explain_infeasibility()
    shares; add new hard rules there.
    """
    # 1. Validate and prep constraints first
    try:
        validate_and_merge_constraints(cluster)
    except RuleConflictError as e:
        return Solution(feasible=False, placements={}, migrations=[],
                        stats=SolverStats(status=f"RULE_CONFLICT: {str(e)}", objective=0,
                                         load_gap=0.0, migration_count=0, wall_time_ms=0))

    model = cp_model.CpModel()
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints

    # 2. Determine balancing strategy and weights
    current_gap = _initial_load_gap(cluster)
    eff_wb, eff_ws = _resolve_balancing(bal, current_gap, cluster)

    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}

    # 3. Decision Variables: x[i][j] is 1 if Guest i is placed on Node j
    x = [[model.new_bool_var(f"x_{v.name}_{n.name}") for n in nodes] for v in vms]

    # 4. Handle forbidden combinations (Feedback from Reachability Planner)
    if forbidden_placements:
        for forbidden in forbidden_placements:
            literals = [x[vm_idx[v]][node_idx[n]] for v, n in forbidden.items() if v in vm_idx and n in node_idx]
            if literals: model.add(sum(literals) <= len(literals) - 1)

    # 5. Hard rules: placement, maintenance, evacuation, ignore, capacity,
    #    hard (anti-)affinity and pinning.
    _add_hard_constraints(model, x, cluster)

    # 6. Soft Affinity / Anti-Affinity
    soft_penalties = []
    for rule in cons.anti_affinity:
        indices = [vm_idx[n] for n in rule["vms"] if n in vm_idx]
        if len(indices) < 2 or rule.get("hard", True): continue
        for j in range(len(nodes)):
            violated = model.new_bool_var(f"soft_aa_{rule.get('name','na')}_{nodes[j].name}")
            model.add(sum(x[i][j] for i in indices) <= 1 + len(indices) * violated)
            soft_penalties.append(violated)

    for rule in cons.affinity:
        indices = [vm_idx[n] for n in rule["vms"] if n in vm_idx]
        if len(indices) < 2 or rule.get("hard", True): continue
        for other in indices[1:]:
            for j in range(len(nodes)):
                violated = model.new_bool_var(f"soft_aff_{rule.get('name','na')}_{vms[other].name}_{nodes[j].name}")
                model.add(x[indices[0]][j] - x[other][j] <= violated)
                model.add(x[other][j] - x[indices[0]][j] <= violated)
                soft_penalties.append(violated)

    # 8. Optimization Objective (Gap Minimization)
    max_load_val = _LOAD_SCALE * 15

    def add_resource_gap(resource_type: Resource, use_psi: bool = False) -> "cp_model.IntVar":
        """Helper to create balanced-load variables."""
        node_loads = []
        for j, node in enumerate(nodes):
            if node.maintenance: continue

            if use_psi:
                cap = 100
                coeffs = [int(vms[i].pressure(resource_type) * vms[i].priority * _LOAD_SCALE) for i in range(len(vms))]
                used_expr = sum(coeffs[i] * x[i][j] for i in range(len(vms)))
            else:
                if resource_type is Resource.Cpu:
                    cap = max(1, node.cpu_total - node.cpu_reserve)
                    if bal.mode == "assigned":
                        coeffs = [vms[i].cpu * vms[i].priority * _LOAD_SCALE for i in range(len(vms))]
                    else:
                        coeffs = [int(vms[i].cpu_usage * vms[i].priority * _LOAD_SCALE) for i in range(len(vms))]
                    used_expr = sum(coeffs[i] * x[i][j] for i in range(len(vms)))
                elif resource_type is Resource.Memory:
                    cap = max(1, (node.memory_total - node.memory_reserve) // _MB)
                    coeffs = [(vms[i].memory * vms[i].priority // _MB) * _LOAD_SCALE for i in range(len(vms))]
                    used_expr = sum(coeffs[i] * x[i][j] for i in range(len(vms)))
                else:  # Resource.Disk
                    cap = max(1, sum(node.storage_free.values()) // _MB)
                    coeffs = [(sum(vms[i].disks.values()) * vms[i].priority // _MB) * _LOAD_SCALE for i in range(len(vms))]
                    used_expr = sum(coeffs[i] * x[i][j] for i in range(len(vms)))

            # add_division_equality requires an IntVar numerator in ortools 9.10+;
            # a bare LinearExpr causes MODEL_INVALID.  Materialise with explicit bounds.
            max_used = max(1, sum(abs(c) for c in coeffs))
            used_var = model.new_int_var(0, max_used, f"used_{resource_type}_{'psi' if use_psi else 'u'}_{node.name}")
            model.add(used_var == used_expr)

            lv = model.new_int_var(0, max_load_val, f"l_{resource_type}_{'psi' if use_psi else 'u'}_{node.name}")
            model.add_division_equality(lv, used_var, cap)
            node_loads.append(lv)

        gap = model.new_int_var(0, max_load_val, f"gap_{resource_type}_{'psi' if use_psi else 'u'}")
        if node_loads:
            mx, mn = model.new_int_var(0, max_load_val, "mx"), model.new_int_var(0, max_load_val, "mn")
            model.add_max_equality(mx, node_loads), model.add_min_equality(mn, node_loads)
            model.add(gap == mx - mn)
        else: model.add(gap == 0)
        return gap

    method = bal.method
    if method is Method.GlobalSmart:
        ug_m, pg_m = add_resource_gap(Resource.Memory), add_resource_gap(Resource.Memory, True)
        ug_c, pg_c = add_resource_gap(Resource.Cpu), add_resource_gap(Resource.Cpu, True)
        ug_i, pg_i = add_resource_gap(Resource.Disk), add_resource_gap(Resource.Disk, True)
        load_gap = model.new_int_var(0, 50 * max_load_val, "global_gap")
        model.add(load_gap == bal.w_global_mem * (bal.w_mem_usage * ug_m + bal.w_mem_psi * pg_m) +
                          bal.w_global_cpu * (bal.w_cpu_usage * ug_c + bal.w_cpu_psi * pg_c) +
                          bal.w_global_io * (bal.w_io_usage * ug_i + bal.w_io_psi * pg_i))
    elif method.is_smart:
        res = method.resource
        ug, pg = add_resource_gap(res), add_resource_gap(res, True)
        w_u, w_p = _smart_weights(bal, res)
        load_gap = model.new_int_var(0, 10 * max_load_val, "smart_gap")
        model.add(load_gap == w_u * ug + w_p * pg)
    else:
        use_psi = method.is_psi or bal.mode == "psi"
        load_gap = add_resource_gap(method.resource, use_psi=use_psi)

    # 9. Migration Cost (Stickiness)
    _GiB, _COST_UNIT, _LOCAL_DISK_FACTOR = 1024 ** 3, 256 * 1024 * 1024, 4
    mig_count_list, mig_cost_terms = [], []
    for i, vm in enumerate(vms):
        if vm.name not in set(cons.ignore):
            m_var = model.new_bool_var(f"mvar_{vm.name}")
            if vm.node in node_idx:
                model.add(m_var == 1 - x[i][node_idx[vm.node]])
            else:
                model.add(m_var == 1)  # node gone — VM must migrate
            mig_count_list.append(m_var)
            ram_cost, disk_cost = max(1, vm.memory // _COST_UNIT), sum(vm.disks.values()) // _COST_UNIT if vm.disks else 0
            mig_cost_terms.append((ram_cost + _LOCAL_DISK_FACTOR * disk_cost) * m_var)

    migration_cost = model.new_int_var(0, len(vms) * 2048, "m_cost")
    if mig_cost_terms: model.add(migration_cost == sum(mig_cost_terms))
    else: model.add(migration_cost == 0)

    # 10. Penalties for constraint violations
    penalty_total = model.new_int_var(0, 100 * _SOFT_PENALTY, "penalty_total")
    if soft_penalties: model.add(penalty_total == sum(soft_penalties) * _SOFT_PENALTY)
    else: model.add(penalty_total == 0)

    # 11. Final Minimization Target
    model.minimize(eff_wb * load_gap + eff_ws * migration_cost + penalty_total)

    # 12. Execution
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    t0 = time.monotonic(); status = solver.solve(model); wall_ms = (time.monotonic() - t0) * 1000

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution(feasible=False, placements={}, migrations=[],
                        stats=SolverStats(status=solver.status_name(status), objective=0,
                                         load_gap=0.0, migration_count=0, wall_time_ms=wall_ms),
                        blocking_vms=_find_blocking_vms(cluster, time_limit_s) if cluster.evacuate_node else [])

    placements = {v.name: nodes[j].name for i, v in enumerate(vms) for j in range(len(nodes)) if solver.value(x[i][j])}
    migrations = [Migration(vm=v.name, source=v.node, target=placements[v.name]) for v in vms if placements[v.name] != v.node]
    vm_by_name = {v.name: v for v in vms}
    cost_gib = sum(max(1, vm_by_name[m.vm].memory // _GiB) + _LOCAL_DISK_FACTOR * (sum(vm_by_name[m.vm].disks.values()) // _GiB if vm_by_name[m.vm].disks else 0) for m in migrations)

    return Solution(feasible=True, placements=placements, migrations=migrations,
                    stats=SolverStats(status=solver.status_name(status), objective=int(round(solver.objective_value)),
                                      load_gap=solver.value(load_gap) / _LOAD_SCALE,
                                      migration_count=len(migrations), wall_time_ms=wall_ms,
                                      migration_cost_gib=cost_gib))


def solve_reachable(cluster: Cluster, total_time_limit_s: float = 60.0, max_retries: int = 10, quiet: bool = True) -> tuple[Solution, MigrationPlan]:
    """
    Ensures that the found solution is reachable via a safe sequence
    of migrations (no deadlocks).
    """
    forbidden: list[dict[str, str]] = []
    last_sol: Solution | None = None
    last_plan: MigrationPlan | None = None
    start = time.monotonic()
    for attempt in range(max_retries + 1):
        rem = total_time_limit_s - (time.monotonic() - start)
        if rem <= 0: break

        # 1. Try to find an optimal distribution
        sol = solve(cluster, time_limit_s=max(1.0, rem), forbidden_placements=forbidden)
        if not sol.feasible:
            if last_sol:
                assert last_plan is not None
                return last_sol.model_copy(update={"path_feasible": False, "reachability_attempts": attempt + 1}), last_plan
            return sol, plan_migrations(cluster, sol)

        # 2. Check if there is an executable path
        plan = plan_migrations(cluster, sol); last_sol, last_plan = sol, plan
        if plan.path_feasible: return sol.model_copy(update={"reachability_attempts": attempt + 1}), plan

        # 3. Handle cycles: Forbid this placement and re-solve
        if not plan.unbreakable_cycle: break
        forbidden.append({vm: sol.placements[vm] for vm in plan.unbreakable_cycle if vm in sol.placements})
        if not quiet: logger.info(f"  [solve_reachable] Attempt {attempt+1}: Cycle {plan.unbreakable_cycle}. Retrying...")

    if last_sol:
        assert last_plan is not None
        return last_sol.model_copy(update={"path_feasible": False, "reachability_attempts": attempt + 1}), last_plan
    return sol, plan_migrations(cluster, sol)
