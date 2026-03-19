"""Simulate solver from a JSON data dump."""
import json
import sys
from proxlb_solver.adapter import from_proxlb_data
from proxlb_solver.solver import solve_reachable
from proxlb_solver.reporter import print_report

def run_simulation(dump_path: str) -> None:
    # Lazy: ProxLB is only needed when the simulator actually runs.
    from proxlb.utils.proxlb_data import ProxLbData

    with open(dump_path) as f:
        data = ProxLbData.model_validate(json.load(f))

    print(f"--- Loading data from {dump_path} ---")
    cluster = from_proxlb_data(data)

    print(f"--- Cluster: {cluster.name} ({len(cluster.nodes)} nodes, {len(cluster.vms)} VMs) ---")

    print("--- Calculating Optimal Reachable Placement ---")
    solution, plan = solve_reachable(cluster, quiet=False)

    print_report(cluster, solution)

    if solution and solution.feasible and plan and plan.steps:
        print("\n[Migration Plan Summary]")
        for step in plan.steps:
            par = " (parallel)" if step.parallel else ""
            print(f"Step {step.step}{par}:")
            for m in step.migrations:
                print(f"  - {m.vm}: {m.source} -> {m.target}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m proxlb_solver.simulate <proxlb_dump.json>")
        sys.exit(1)
    run_simulation(sys.argv[1])
