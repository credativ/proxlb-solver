"""
Data models for the ProxLB CP-SAT solver.

This module defines the objects used to represent the cluster state, 
user constraints, and the resulting migration plans. 

Units used throughout the project:
- Memory: Bytes (int)
- Storage: Bytes (int) 
- CPU Load: Float (0.0 to N.0, where 1.0 is 100% of one core)
- PSI Pressure: Float (0.0 to 100.0, representing percentage of stall time)
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    """Represents a physical Proxmox host."""
    name: str
    cpu_total: int        # Number of physical CPU cores
    memory_total: int     # Total RAM in bytes
    
    # Storage capacities: Maps storage name (e.g. 'local-lvm') to free bytes
    storage_free: dict[str, int] = field(default_factory=dict)
    
    # Host Reservations: Resources set aside for the hypervisor/OS
    cpu_reserve: int = 0
    memory_reserve: int = 0
    storage_reserve: dict[str, int] = field(default_factory=dict)
    
    # Real-time metrics (Pressure Stall Information)
    cpu_pressure: float = 0.0
    memory_pressure: float = 0.0
    io_pressure: float = 0.0
    
    maintenance: bool = False # If True, no VMs can reside here


@dataclass(frozen=True)
class VM:
    """Represents a Virtual Machine or LXC Container."""
    name: str
    node: str             # Current host name
    cpu: int              # Configured vCPUs (cores)
    memory: int           # Configured RAM in bytes
    
    # Actual resource footprints
    cpu_usage: float = 0.0    # Actual CPU load (e.g. 0.5 = 50% of one core)
    cpu_pressure: float = 0.0 # CPU stall time percentage
    memory_pressure: float = 0.0
    io_pressure: float = 0.0
    
    # Storage requirements: Maps storage name to required bytes
    disks: dict[str, int] = field(default_factory=dict)
    
    # Priority: Used to weight the importance of this VM during balancing
    # 1 = Low, 2 = Normal (default), 3 = High
    priority: int = 2
    
    vm_type: str = "vm"   # "vm" or "ct"


@dataclass(frozen=True)
class Constraints:
    """Container for placement rules.
    
    Rules (affinity, anti_affinity) are dictionaries with:
    - name: str
    - vms: list[str]
    - hard: bool (default True)
    - origin: str (e.g. 'pve' for native HA rules, 'plb' for internal rules)
    """
    affinity: list[dict] = field(default_factory=list)
    anti_affinity: list[dict] = field(default_factory=list)
    pin: list[dict] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Balancing:
    """Configuration for the balancing algorithm."""
    method: str = "memory"      # Recommended default: RAM is the most critical resource.
    balanciness: int = 3        # 1-5 level. 3 (Moderate) avoids excessive 'ping-pong' moves.
    cpu_overcommit: float = 2.0 # Standard industry default for safe overbooking.
    
    # Optional weight overrides (usually derived from balanciness)
    w_balance: int | None = None
    w_stickiness: int | None = None

    # Weights for 'smart' modes (Usage vs Pressure)
    # We prioritize PSI (contention) over raw usage to fix performance issues first.
    w_cpu_usage: int = 1
    w_cpu_psi: int = 2
    w_mem_usage: int = 1
    w_mem_psi: int = 2
    w_io_usage: int = 1
    w_io_psi: int = 2
    
    # Global resource weights (Relative importance of resource pools)
    # Memory is prioritized (10) as exhaustion is fatal (OOM). 
    # CPU is secondary (5), and IO is weighted lowest (1) as it's often pool-limited.
    w_global_mem: int = 10
    w_global_cpu: int = 5
    w_global_io: int = 1
    
    # Operational Safety Limits
    # max_parallel_migrations: Limits concurrent network/storage load during moves.
    max_parallel_migrations: int | None = 2 
    # max_node_inflow: Prevents transient RAM/CPU peaks by allowing only one entry per host.
    max_node_inflow: int = 1


@dataclass(frozen=True)
class Cluster:
    """The complete state of the cluster to be optimized."""
    name: str
    description: str
    balancing: Balancing
    nodes: list[Node]
    vms: list[VM]
    constraints: Constraints
    expect: Expect
    evacuate_node: str | None = None


@dataclass(frozen=True)
class Migration:
    vm: str
    source: str
    target: str


@dataclass(frozen=True)
class MigrationStep:
    step: int
    migrations: list[Migration]
    parallel: bool


@dataclass(frozen=True)
class MigrationPlan:
    steps: list[MigrationStep]
    dependency_edges: list[tuple[str, str]]
    temp_moves: list[str]
    path_feasible: bool = True
    unbreakable_cycle: list[str] = field(default_factory=list)
    pve_deferred: list[str] = field(default_factory=list)
    """VMs whose migration is delegated to PVE HA (one per affinity group is
    migrated explicitly; the rest are expected to follow automatically)."""


@dataclass(frozen=True)
class SolverStats:
    """Meta-information about the solver execution."""
    status: str        # e.g. "OPTIMAL", "INFEASIBLE"
    objective: int     # Internal math score (lower is better)
    load_gap: float    # The calculated gap (Max node load - Min node load)
    migration_count: int
    wall_time_ms: float


@dataclass(frozen=True)
class Solution:
    """The resulting target state found by the solver."""
    feasible: bool
    placements: dict[str, str] # vm_name -> node_name
    migrations: list[Migration]
    stats: SolverStats
    blocking_vms: list[str] = field(default_factory=list)
    path_feasible: bool = True # False if the planner couldn't find a way to get there
    reachability_attempts: int = 1 # How many times we retried solver vs planner


@dataclass(frozen=True)
class Expect:
    """Used in YAML scenarios to define what the result SHOULD look like."""
    feasible: bool = True
    constraints_satisfied: bool = True
    spread_improved: bool | None = None
    max_migrations: int | None = None
    placements: dict[str, str] = field(default_factory=dict)
    node_empty: str | None = None
    path_feasible: bool | None = None
