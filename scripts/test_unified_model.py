#!/usr/bin/env python3
import sys
import os
from pathlib import Path

# Add current dir to path
sys.path.append(os.getcwd() + "/proxlb-solver")

try:
    from proxlb_solver.loader import load_scenario
    from proxlb_solver.unified_solver import solve_unified
except ImportError:
    # Try with absolute path if needed
    sys.path.append(os.getcwd())
    from proxlb_solver.loader import load_scenario
    from proxlb_solver.unified_solver import solve_unified

def run_test(scenario_path):
    print(f"\n>>> Testing Scenario: {scenario_path.name}")
    cluster = load_scenario(scenario_path)
    
    # solve_unified now handles time steps automatically via Iterative Deepening
    solution, plan = solve_unified(cluster, time_limit_s=10.0)
    
    if solution.feasible:
        print(f"Status: {solution.stats.status}")
        print(f"Migrations: {solution.stats.migration_count}")
        print(f"Final Gap: {solution.stats.load_gap:.4f}")
        for step in plan.steps:
            m_strs = [f"{m.vm}: {m.source} -> {m.target}" for m in step.migrations]
            # Match the Step attribute name (it's 'step' in models.py, but index in my output)
            idx = getattr(step, "step", getattr(step, "index", 0))
            print(f"  Step {idx}: {m_strs}")
    else:
        print(f"Status: {solution.stats.status} (FAILED)")

if __name__ == "__main__":
    scenarios = [
        "proxlb-solver/scenarios/basic/simple_rebalance.yaml",
        "proxlb-solver/scenarios/migration/swap_with_spare.yaml",
        "proxlb-solver/scenarios/migration/evacuate_with_spare.yaml",
        "proxlb-solver/scenarios/migration/circular_swap.yaml",
        "proxlb-solver/scenarios/migration/chain_dependency.yaml"
    ]
    for s in scenarios:
        p = Path(s)
        if p.exists():
            run_test(p)
