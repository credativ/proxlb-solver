"""Integration tests for the solver-planner loop."""

from pathlib import Path
from proxlb_solver.loader import load_scenario
from proxlb_solver.solver import solve_reachable

def test_loop_required_scenario():
    """Test that the feedback loop correctly bypasses a deadlock.

    The optimal solution is a swap (vm-a↔vm-b) giving near-perfect balance,
    but both nodes are too full and no temp node is available. The planner
    detects an unbreakable cycle. On retry, the solver falls back to the
    current (suboptimal) placement as the best reachable state.
    """
    scenario_path = Path(__file__).parent.parent / "scenarios" / "migration" / "loop_required.yaml"
    cluster = load_scenario(scenario_path)

    # solve_reachable should detect the cycle and retry
    solution, plan = solve_reachable(cluster, quiet=False)

    assert solution.feasible is True
    assert solution.path_feasible is True

    # Must have taken more than 1 attempt (retry loop triggered)
    assert solution.reachability_attempts > 1, (
        f"Expected retry loop to trigger, but solved in {solution.reachability_attempts} attempt(s)"
    )

    # After retry, the swap is forbidden so VMs stay put (no moves possible)
    assert solution.placements["vm-a"] == "node-B"
    assert solution.placements["vm-b"] == "node-A"
    assert len(solution.migrations) == 0
