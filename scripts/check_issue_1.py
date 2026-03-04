#!/usr/bin/env python3
import sys
import os
from pathlib import Path

# Add current dir to path
sys.path.append(os.getcwd() + "/proxlb-solver")

from proxlb_solver.loader import load_scenario
from proxlb_solver.solver import solve_reachable
from proxlb_solver.unified_solver import solve_unified

f = Path("proxlb-solver/scenarios/migration/long_cycle_full_nodes.yaml")
cluster = load_scenario(f)

print("=== Standard Model (Solver + Planner) ===")
sol_std, plan_std = solve_reachable(cluster, quiet=False)
print("Feasible:", sol_std.feasible)
if sol_std.feasible:
    print("Path Feasible:", plan_std.path_feasible)
    for s in plan_std.steps:
        m_strs = [m.vm + ": " + m.source + " -> " + m.target for m in s.migrations]
        print("  Step {}: {}".format(s.step, m_strs))

print("\n=== Unified Model (Iterative Deepening) ===")
sol_uni, plan_uni = solve_unified(cluster, time_limit_s=20.0)
print("Feasible:", sol_uni.feasible)
if sol_uni.feasible:
    for s in plan_uni.steps:
        m_strs = [m.vm + ": " + m.source + " -> " + m.target for m in s.migrations]
        print("  Step {}: {}".format(s.step, m_strs))
