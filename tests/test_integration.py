"""Integration tests for the solver-planner loop."""

from pathlib import Path
from proxlb_solver.loader import load_scenario
from proxlb_solver.solver import solve_reachable

def test_loop_required_scenario():
    """Test that the feedback loop correctly bypasses a deadlock."""
    scenario_path = Path(__file__).parent.parent / "scenarios" / "migration" / "loop_required.yaml"
    cluster = load_scenario(scenario_path)
    
    # solve_reachable should find the suboptimal but reachable solution
    solution, plan = solve_reachable(cluster, quiet=False)
    
    assert solution.feasible is True
    assert plan.path_feasible is True
    
    # Verify we didn't land in the deadlock
    # Optimal (but bad) would be vm-a: node-B, vm-b: node-A
    # Reachable must be vm-a: node-D, vm-b: node-A
    assert solution.placements["vm-a"] == "node-D"
    assert solution.placements["vm-b"] == "node-A"
    
    # Verify migrations
    # vm-b moves node-B -> node-A (40GB left on A after a leaves? No, sequence!)
    # Sequence should be: vm-a: A -> D (A is empty), then vm-b: B -> A
    assert len(solution.migrations) == 2
