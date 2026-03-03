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
import dataclasses
import logging
from ortools.sat.python import cp_model

from .models import Balancing, Cluster, Migration, Solution, SolverStats, MigrationPlan
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


def _initial_load_gap_single(cluster: Cluster, method: str) -> float:
    """
    Calculates the initial load gap for a specific resource type.
    The gap is defined as the difference between the most loaded and least loaded node.
    
    Priority weighting is applied to each VM's footprint.
    """
    bal = cluster.balancing
    usage_loads = []
    psi_loads = []
    
    for node in cluster.nodes:
        if node.maintenance:
            continue
            
        # 1. Determine resource metrics based on the balancing method
        if method == "cpu_smart" or method == "cpu":
            # Usage: Actual CPU load * Priority
            u_used = sum(vm.cpu_usage * vm.priority for vm in cluster.vms if vm.node == node.name)
            u_total = node.cpu_total
            # Pressure: CPU PSI * Priority
            p_used = sum(vm.cpu_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            w_u, w_p = bal.w_cpu_usage, bal.w_cpu_psi
            
        elif method == "memory_smart" or method == "memory":
            u_used = sum(vm.memory * vm.priority for vm in cluster.vms if vm.node == node.name)
            u_total = node.memory_total
            p_used = sum(vm.memory_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            w_u, w_p = bal.w_mem_usage, bal.w_mem_psi
            
        elif method == "io_smart" or method == "io_psi" or method == "disk":
            # For disk/IO, we look at the sum of all virtual disks
            u_used = sum(sum(vm.disks.values()) * vm.priority for vm in cluster.vms if vm.node == node.name)
            u_total = sum(node.storage_free.values()) or 1
            p_used = sum(vm.io_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            w_u, w_p = bal.w_io_usage, bal.w_io_psi
        else:
            return 0.0
            
        # 2. Store relative load per node (0.0 to 1.0+)
        usage_loads.append(u_used / u_total if u_total else 0)
        psi_loads.append(p_used / 100.0)
        
    if not usage_loads:
        return 0.0
        
    # 3. Calculate the gap (Max - Min)
    u_gap = max(usage_loads) - min(usage_loads)
    p_gap = max(psi_loads) - min(psi_loads)
    
    # 4. Return the weighted gap if using 'smart' mode
    if method.endswith("_smart"):
        total_w = w_u + w_p
        return (w_u * u_gap + w_p * p_gap) / total_w if total_w else u_gap
        
    # For PSI methods, return the pressure gap, otherwise usage gap
    is_psi_method = method.endswith("_psi") or method == "cpu_psi"
    return p_gap if is_psi_method else u_gap


def _initial_load_gap(cluster: Cluster) -> float:
    """
    Computes the initial load gap across the entire cluster using the sum 
    of all VM footprints. This is used to decide if rebalancing is needed.
    """
    method = cluster.balancing.method
    bal = cluster.balancing
    
    # Handle multi-resource balancing
    if method == "global_smart":
        m_gap = _initial_load_gap_single(cluster, "memory_smart")
        c_gap = _initial_load_gap_single(cluster, "cpu_smart")
        i_gap = _initial_load_gap_single(cluster, "io_smart")
        
        total_w = bal.w_global_mem + bal.w_global_cpu + bal.w_global_io
        return (bal.w_global_mem * m_gap + bal.w_global_cpu * c_gap + bal.w_global_io * i_gap) / total_w if total_w else m_gap
        
    if method in ["cpu_smart", "memory_smart", "io_smart"]:
        return _initial_load_gap_single(cluster, method)
        
    # Handle single-resource methods
    loads = []
    for node in cluster.nodes:
        if node.maintenance:
            continue
            
        if method == "cpu":
            used = sum(vm.cpu_usage * vm.priority for vm in cluster.vms if vm.node == node.name)
            total = node.cpu_total
        elif method == "cpu_psi":
            used = sum(vm.cpu_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            total = 100.0
        elif method == "memory_psi":
            used = sum(vm.memory_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            total = 100.0
        elif method == "io_psi":
            used = sum(vm.io_pressure * vm.priority for vm in cluster.vms if vm.node == node.name)
            total = 100.0
        elif method == "disk":
            used = sum(sum(vm.disks.values()) * vm.priority for vm in cluster.vms if vm.node == node.name)
            total = sum(node.storage_free.values()) or 1
        else: # Default: memory usage
            used = sum(vm.memory * vm.priority for vm in cluster.vms if vm.node == node.name)
            total = node.memory_total
            
        loads.append(used / total if total else 0)
        
    return max(loads) - min(loads) if loads else 0.0


def _resolve_balancing(bal: Balancing, current_gap: float) -> tuple[int, int]:
    """
    Translates the user-facing 'balanciness' (1-5) into internal 
    math weights for the optimizer.
    """
    level = max(1, min(5, bal.balanciness))
    prof_wb, prof_ws, threshold = _BALANCINESS_PROFILES[level]
    
    # Allow explicit overrides from the config if provided
    weight_balance = bal.w_balance if bal.w_balance is not None else prof_wb
    weight_stickiness = bal.w_stickiness if bal.w_stickiness is not None else prof_ws
    
    # If the cluster is already "well balanced" (below threshold), 
    # disable balancing to prevent unnecessary migrations.
    if current_gap < threshold:
        weight_balance = 0
        
    return weight_balance, weight_stickiness


def _find_blocking_vms(cluster: Cluster, time_limit_s: float) -> list[str]:
    """
    Small diagnostic solver: If a node evacuation fails, this function 
    tries to move each VM on that node individually. If a single VM cannot 
    be moved even when all others are ignored, it's a hard blocker (pinning).
    """
    evac_node = cluster.evacuate_node
    if not evac_node:
        return []
        
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vms_on_node = [v for v in vms if v.node == evac_node]
    
    if not vms_on_node:
        return []
        
    blockers = []
    for test_vm in vms_on_node:
        model = cp_model.CpModel()
        # Choice variables: Where can THIS vm go?
        y = [model.new_bool_var(f"y_{test_vm.name}_{n.name}") for n in nodes]
        model.add(sum(y) == 1) # Must be placed on exactly one node
        model.add(y[node_idx[evac_node]] == 0) # Cannot stay on the node we want to empty
        
        # Respect maintenance and pinning
        for j, node in enumerate(nodes):
            if node.maintenance:
                model.add(y[j] == 0)
        for rule in cons.pin:
            if rule["vm"] == test_vm.name:
                allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
                for j in range(len(nodes)):
                    if j not in allowed:
                        model.add(y[j] == 0)
                        
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(time_limit_s, 5.0)
        
        if solver.solve(model) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            blockers.append(test_vm.name)
            
    return blockers or [v.name for v in vms_on_node]


def solve(cluster: Cluster, time_limit_s: float = 30.0, forbidden_placements: list[dict[str, str]] | None = None) -> Solution:
    """
    The Core Solver. Translates the cluster into a CP-SAT model and finds 
    the mathematically optimal VM placement.
    """
    # 1. Validate constraints first
    try:
        validate_and_merge_constraints(cluster)
    except RuleConflictError as e:
        return Solution(False, {}, [], SolverStats(f"RULE_CONFLICT: {str(e)}", 0, 0.0, 0, 0))
        
    model = cp_model.CpModel()
    nodes, vms, bal, cons = cluster.nodes, cluster.vms, cluster.balancing, cluster.constraints
    
    # 2. Determine balancing aggressiveness
    current_gap = _initial_load_gap(cluster)
    eff_wb, eff_ws = _resolve_balancing(bal, current_gap)
    
    node_idx = {n.name: i for i, n in enumerate(nodes)}
    vm_idx = {v.name: i for i, v in enumerate(vms)}
    
    # 3. Decision Variables: x[i][j] is 1 if VM i is placed on Node j
    x = [[model.new_bool_var(f"x_{v.name}_{n.name}") for n in nodes] for v in vms]
    
    # 4. Forbidden Placements (used by the reachability loop to avoid deadlocks)
    if forbidden_placements:
        for forbidden in forbidden_placements:
            literals = [x[vm_idx[v]][node_idx[n]] for v, n in forbidden.items() if v in vm_idx and n in node_idx]
            if literals:
                # Disallow this specific combination of placements
                model.add(sum(literals) <= len(literals) - 1)
                
    # 5. Core Placement Constraints
    for i in range(len(vms)):
        # Every VM must be on exactly one node
        model.add(sum(x[i][j] for j in range(len(nodes))) == 1)
        
    for j, node in enumerate(nodes):
        if node.maintenance:
            # Maintenance nodes cannot host any guests
            for i in range(len(vms)):
                model.add(x[i][j] == 0)
                
    if cluster.evacuate_node:
        evac_idx = node_idx.get(cluster.evacuate_node)
        if evac_idx is not None:
            for i in range(len(vms)):
                model.add(x[i][evac_idx] == 0)
                
    for i, vm in enumerate(vms):
        if vm.name in cons.ignore:
            # Ignored VMs must stay on their current node
            model.add(x[i][node_idx[vm.node]] == 1)
            
    # 6. Resource Capacity Constraints
    for j, node in enumerate(nodes):
        # Memory Capacity (Hard Limit)
        model.add(sum((vms[i].memory // _MB) * x[i][j] for i in range(len(vms))) <= (node.memory_total - node.memory_reserve) // _MB)
        # CPU Capacity (considering Overcommit)
        usable_cpu = max(0, node.cpu_total - node.cpu_reserve)
        model.add(sum(vms[i].cpu * 1000 * x[i][j] for i in range(len(vms))) <= usable_cpu * int(bal.cpu_overcommit * 1000))
        
    # Storage Capacity (Hard Limit per individual storage pool)
    all_storages = {s for v in vms for s in v.disks.keys()}
    for sn in all_storages:
        for j, node in enumerate(nodes):
            model.add(sum((vms[i].disks.get(sn, 0) // _MB) * x[i][j] for i in range(len(vms))) <= max(0, (node.storage_free.get(sn, 0) - node.storage_reserve.get(sn, 0)) // _MB))

    # 7. Affinity / Anti-Affinity Rules
    soft_penalties = []
    
    for rule in cons.anti_affinity:
        indices = [vm_idx[n] for n in rule["vms"] if n in vm_idx]
        if len(indices) < 2: continue
        
        if rule.get("hard", True):
            # Hard Anti-Affinity: Max 1 VM from the group per node
            for j in range(len(nodes)):
                model.add(sum(x[i][j] for i in indices) <= 1)
        else:
            # Soft Anti-Affinity: Try to avoid, but allow if forced
            for j in range(len(nodes)):
                violated = model.new_bool_var(f"soft_aa_{rule.get('name','na')}_{nodes[j].name}")
                model.add(sum(x[i][j] for i in indices) <= 1 + len(indices) * violated)
                soft_penalties.append(violated)
                
    for rule in cons.affinity:
        indices = [vm_idx[n] for n in rule["vms"] if n in vm_idx]
        if len(indices) < 2: continue
        
        if rule.get("hard", True):
            # Hard Affinity: All VMs in the group must be on the same node
            for other in indices[1:]:
                for j in range(len(nodes)):
                    model.add(x[indices[0]][j] == x[other][j])
        else:
            # Soft Affinity: Try to keep them together
            for other in indices[1:]:
                for j in range(len(nodes)):
                    violated = model.new_bool_var(f"soft_aff_{rule.get('name','na')}_{vms[other].name}_{nodes[j].name}")
                    # If x[0][j] != x[other][j], then violated must be 1
                    model.add(x[indices[0]][j] - x[other][j] <= violated)
                    model.add(x[other][j] - x[indices[0]][j] <= violated)
                    soft_penalties.append(violated)
                    
    for rule in cons.pin:
        # Node Pinning: Forced list of allowed nodes for a specific VM
        vm_i = vm_idx.get(rule["vm"])
        if vm_i is not None:
            allowed = {node_idx[n] for n in rule["nodes"] if n in node_idx}
            for j in range(len(nodes)):
                if j not in allowed:
                    model.add(x[vm_i][j] == 0)

    # 8. Objective Function: Goal is to minimize the load gap and migration cost
    method = bal.method
    mode = bal.mode
    max_load_val = _LOAD_SCALE * 15
    load_vars = []
    
    def add_smart_gap(resource_type):
        """Helper to create balanced-load variables for smart rebalancing."""
        usage_vars, pressure_vars = [], []
        for j, node in enumerate(nodes):
            if node.maintenance: continue
            
            # Capacity and usage calculation based on mode (Used vs Assigned)
            if resource_type == "cpu":
                cap = max(1, node.cpu_total - node.cpu_reserve)
                if mode == "assigned":
                    # Sum of configured cores
                    used = sum(vms[i].cpu * vms[i].priority * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
                else:
                    # Sum of actual CPU usage
                    used = sum(int(vms[i].cpu_usage * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif resource_type == "memory":
                cap = max(1, (node.memory_total - node.memory_reserve) // _MB)
                used = sum((vms[i].memory * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            else: # disk
                cap = max(1, sum(node.storage_free.values()) // _MB)
                used = sum((sum(vms[i].disks.values()) * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            
            # Create a variable representing % load on this node
            uv = model.new_int_var(0, max_load_val, f"u_{resource_type}_{node.name}")
            model.add_division_equality(uv, used, model.new_constant(cap))
            usage_vars.append(uv)
            
            # Create a variable representing % PSI pressure on this node
            p_sum = sum(int((vms[i].cpu_pressure if resource_type == "cpu" else vms[i].memory_pressure if resource_type == "memory" else vms[i].io_pressure) * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            pv = model.new_int_var(0, 3 * _LOAD_SCALE, f"p_{resource_type}_{node.name}")
            model.add_division_equality(pv, p_sum, model.new_constant(100))
            pressure_vars.append(pv)
            
        # Calculate the Gap (Max - Min) for this resource type
        usage_gap = model.new_int_var(0, max_load_val, f"ug_{resource_type}")
        if usage_vars:
            mx, mn = model.new_int_var(0, max_load_val, f"mxu_{resource_type}"), model.new_int_var(0, max_load_val, f"mnu_{resource_type}")
            model.add_max_equality(mx, usage_vars), model.add_min_equality(mn, usage_vars)
            model.add(usage_gap == mx - mn)
        else: model.add(usage_gap == 0)
        
        pressure_gap = model.new_int_var(0, 3 * _LOAD_SCALE, f"pg_{resource_type}")
        if pressure_vars:
            mx, mn = model.new_int_var(0, 3 * _LOAD_SCALE, f"mxp_{resource_type}"), model.new_int_var(0, 3 * _LOAD_SCALE, f"mnp_{resource_type}")
            model.add_max_equality(mx, pressure_vars), model.add_min_equality(mn, pressure_vars)
            model.add(pressure_gap == mx - mn)
        else: model.add(pressure_gap == 0)
        
        return usage_gap, pressure_gap

    # Execute the objective logic based on the selected method
    if method == "global_smart":
        ug_m, pg_m = add_smart_gap("memory")
        ug_c, pg_c = add_smart_gap("cpu")
        ug_i, pg_i = add_smart_gap("io")
        load_gap = model.new_int_var(0, 50 * max_load_val, "global_gap")
        model.add(load_gap == bal.w_global_mem * (bal.w_mem_usage * ug_m + bal.w_mem_psi * pg_m) + 
                          bal.w_global_cpu * (bal.w_cpu_usage * ug_c + bal.w_cpu_psi * pg_c) + 
                          bal.w_global_io * (bal.w_io_usage * ug_i + bal.w_io_psi * pg_i))
    elif method in ["cpu_smart", "memory_smart", "io_smart"]:
        resource = method.split("_")[0]
        ug, pg = add_smart_gap(resource)
        w_u, w_p = (bal.w_cpu_usage, bal.w_cpu_psi) if resource == "cpu" else (bal.w_mem_usage, bal.w_mem_psi) if resource == "memory" else (bal.w_io_usage, bal.w_io_psi)
        load_gap = model.new_int_var(0, 10 * max_load_val, "smart_gap")
        model.add(load_gap == w_u * ug + w_p * pg)
    else:
        # Standard Single-Resource balancing
        for j, node in enumerate(nodes):
            if node.maintenance: continue
            
            # Logic for pure PSI balancing
            if mode == "psi" or method in ["cpu_psi", "memory_psi", "io_psi"]:
                res_type = method.split("_")[0] if "_" in method else method
                if res_type == "disk": res_type = "io"
                cap = 100
                def get_p(v): return v.cpu_pressure if res_type == "cpu" else v.memory_pressure if res_type == "memory" else v.io_pressure
                used = sum(int(get_p(vms[i]) * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif method == "cpu":
                cap = max(1, node.cpu_total - node.cpu_reserve)
                if mode == "assigned":
                    used = sum(vms[i].cpu * vms[i].priority * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
                else:
                    used = sum(int(vms[i].cpu_usage * vms[i].priority * _LOAD_SCALE) * x[i][j] for i in range(len(vms)))
            elif method == "disk":
                cap = max(1, sum(node.storage_free.values()) // _MB)
                used = sum((sum(vms[i].disks.values()) * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
            else: # memory
                cap = max(1, (node.memory_total - node.memory_reserve) // _MB)
                used = sum((vms[i].memory * vms[i].priority // _MB) * _LOAD_SCALE * x[i][j] for i in range(len(vms)))
                
            lv = model.new_int_var(0, max_load_val, f"l_{node.name}")
            model.add_division_equality(lv, used, model.new_constant(cap))
            load_vars.append(lv)
            
        load_gap = model.new_int_var(0, max_load_val, "gap")
        if load_vars:
            mx, mn = model.new_int_var(0, max_load_val, "mx"), model.new_int_var(0, max_load_val, "mn")
            model.add_max_equality(mx, load_vars), model.add_min_equality(mn, load_vars)
            model.add(load_gap == mx - mn)
        else: model.add(load_gap == 0)

    # 9. Migration Cost: Every move costs something to avoid unnecessary activity
    _GiB = 1024 ** 3
    _COST_UNIT = 256 * 1024 * 1024   # 256 MiB granularity
    _LOCAL_DISK_FACTOR = 4            # Local disk migrations are heavily penalized
    
    mig_count_list, mig_cost_terms = [], []
    for i, vm in enumerate(vms):
        if vm.name not in set(cons.ignore):
            # m_var is 1 if VM i is NOT on its original node
            m_var = model.new_bool_var(f"mvar_{vm.name}")
            model.add(m_var == 1 - x[i][node_idx[vm.node]])
            mig_count_list.append(m_var)
            
            # Weight migration by RAM size and local disk
            ram_cost  = max(1, vm.memory // _COST_UNIT)
            disk_cost = sum(vm.disks.values()) // _COST_UNIT if vm.disks else 0
            mig_cost_terms.append((ram_cost + _LOCAL_DISK_FACTOR * disk_cost) * m_var)
            
    migration_count = model.new_int_var(0, len(vms), "m_cnt")
    if mig_count_list: model.add(migration_count == sum(mig_count_list))
    else: model.add(migration_count == 0)
    
    migration_cost = model.new_int_var(0, len(vms) * 2048, "m_cost")
    if mig_cost_terms: model.add(migration_cost == sum(mig_cost_terms))
    else: model.add(migration_cost == 0)

    # 10. Penalty for soft constraint violations
    penalty_total = model.new_int_var(0, 100 * _SOFT_PENALTY, "penalty_total")
    if soft_penalties: model.add(penalty_total == sum(soft_penalties) * _SOFT_PENALTY)
    else: model.add(penalty_total == 0)

    # FINAL OPTIMIZATION TARGET:
    # Minimize: (Improvement * BalanceWeight) + (Migrations * StickinessWeight) + ConstraintPenalties
    model.minimize(eff_wb * load_gap + eff_ws * migration_cost + penalty_total)
    
    # 11. Run the CP-SAT Solver
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    t0 = time.monotonic()
    status = solver.solve(model)
    wall_ms = (time.monotonic() - t0) * 1000
    
    # 12. Format the solution
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        blockers = _find_blocking_vms(cluster, time_limit_s) if cluster.evacuate_node else []
        return Solution(False, {}, [], SolverStats(solver.status_name(status), 0, 0.0, 0, wall_ms), blockers)
        
    placements = {v.name: nodes[j].name for i, v in enumerate(vms) for j in range(len(nodes)) if solver.value(x[i][j])}
    migrations = [Migration(v.name, v.node, placements[v.name]) for v in vms if placements[v.name] != v.node]
    
    vm_by_name = {v.name: v for v in vms}
    cost_gib = sum(
        max(1, vm_by_name[m.vm].memory // _GiB) + _LOCAL_DISK_FACTOR * (sum(vm_by_name[m.vm].disks.values()) // _GiB if vm_by_name[m.vm].disks else 0)
        for m in migrations
    ) if migrations else 0
    
    return Solution(
        True, placements, migrations, 
        SolverStats(solver.status_name(status), solver.objective_value, solver.value(load_gap) / _LOAD_SCALE, len(migrations), wall_ms, cost_gib)
    )


def solve_reachable(cluster: Cluster, total_time_limit_s: float = 60.0, max_retries: int = 10, quiet: bool = True) -> tuple[Solution, MigrationPlan]:
    """
    Wrapper around solve() that ensures the found solution is reachable 
    via a sequence of migrations (no deadlocks).
    """
    forbidden, last_sol, last_plan, start = [], None, None, time.monotonic()
    for attempt in range(max_retries + 1):
        remaining_time = total_time_limit_s - (time.monotonic() - start)
        if remaining_time <= 0:
            break
            
        sol = solve(cluster, time_limit_s=max(1.0, remaining_time), forbidden_placements=forbidden)
        if not sol.feasible:
            if last_sol:
                # Fall back to the last known feasible (but perhaps blocked) solution
                return dataclasses.replace(last_sol, path_feasible=False, reachability_attempts=attempt + 1), last_plan
            return sol, plan_migrations(cluster, sol)
            
        plan = plan_migrations(cluster, sol)
        last_sol, last_plan = sol, plan
        
        if plan.path_feasible:
            return dataclasses.replace(sol, reachability_attempts=attempt + 1), plan
            
        # Planner found a cycle (e.g. A->B, B->A and no space to swap).
        # We forbid this specific placement combination and try again.
        if not plan.unbreakable_cycle:
            break
        forbidden.append({vm: sol.placements[vm] for vm in plan.unbreakable_cycle if vm in sol.placements})
        if not quiet:
            logger.info(f"  [solve_reachable] Attempt {attempt+1}: Cycle {plan.unbreakable_cycle}. Retrying...")
            
    if last_sol:
        return dataclasses.replace(last_sol, path_feasible=False, reachability_attempts=attempt + 1), last_plan
    return sol, plan_migrations(cluster, sol)
