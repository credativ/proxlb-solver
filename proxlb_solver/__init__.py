"""ProxLB CP-SAT VM Scheduler.

Public API
----------
Core types:
    Cluster, Node, VM, Constraints, Balancing,
    Solution, Migration, MigrationPlan, SolverStats

Solver functions:
    solve(cluster, time_limit_s)        — single-shot solve
    solve_reachable(cluster, ...)       — solve with planner feedback loop

ProxLB integration:
    from_proxlb_data(proxlb_data, ...)  — convert live ProxLB data to Cluster
    run_shadow(proxlb_data, cfg)        — shadow-mode observer (read-only)
"""

__version__ = "0.1.1"

from .models import (
    Cluster,
    Node,
    VM,
    Constraints,
    Balancing,
    Solution,
    Migration,
    MigrationPlan,
    SolverStats,
)
from .solver import solve, solve_reachable
from .adapter import from_proxlb_data
from .shadow import run_shadow

__all__ = [
    "__version__",
    # Data models
    "Cluster",
    "Node",
    "VM",
    "Constraints",
    "Balancing",
    "Solution",
    "Migration",
    "MigrationPlan",
    "SolverStats",
    # Solver
    "solve",
    "solve_reachable",
    # ProxLB integration
    "from_proxlb_data",
    "run_shadow",
]
