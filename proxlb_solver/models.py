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
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class Node(BaseModel):
    """Represents a physical Proxmox host."""
    model_config = ConfigDict(frozen=True)

    name: str
    cpu_total: int        # Number of physical CPU cores
    memory_total: int     # Total RAM in bytes

    # Storage capacities: Maps storage name (e.g. 'local-lvm') to free bytes
    storage_free: dict[str, int] = Field(default_factory=dict)

    # Host Reservations: Resources set aside for the hypervisor/OS
    cpu_reserve: int = 0
    memory_reserve: int = 0
    storage_reserve: dict[str, int] = Field(default_factory=dict)

    # Real-time metrics (Pressure Stall Information)
    cpu_pressure: float = 0.0
    memory_pressure: float = 0.0
    io_pressure: float = 0.0

    maintenance: bool = False  # If True, no VMs can reside here


class VM(BaseModel):
    """Represents a Virtual Machine or LXC Container."""
    model_config = ConfigDict(frozen=True)

    name: str
    node: str             # Current host name
    cpu: int              # Configured vCPUs (cores)
    memory: int           # Configured RAM in bytes

    # Actual resource footprints
    cpu_usage: float = 0.0    # Actual CPU load (e.g. 0.5 = 50% of one core)
    cpu_pressure: float = 0.0  # CPU stall time percentage
    memory_pressure: float = 0.0
    io_pressure: float = 0.0

    # Storage requirements: Maps storage name to required bytes
    disks: dict[str, int] = Field(default_factory=dict)

    # Priority: Used to weight the importance of this VM during balancing
    # 1 = Low, 2 = Normal (default), 3 = High
    priority: int = 2

    vm_type: str = "vm"   # "vm" or "ct"


class Constraints(BaseModel):
    """Container for placement rules.

    Rules (affinity, anti_affinity) are dictionaries with:
    - name: str
    - vms: list[str]
    - hard: bool (default True)
    - origin: str (e.g. 'pve' for native HA rules, 'plb' for internal rules)
    """
    model_config = ConfigDict(frozen=True)

    affinity: list[dict] = Field(default_factory=list)
    anti_affinity: list[dict] = Field(default_factory=list)
    pin: list[dict] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)


class Balancing(BaseModel):
    """Configuration for the balancing algorithm."""
    model_config = ConfigDict(frozen=True)

    method: str = "memory"      # Recommended default: RAM is the most critical resource.
    mode: str = "used"          # Balancing mode: "used", "assigned", or "psi"
    balanciness: int = 3        # 1-5 level. 3 (Moderate) avoids excessive 'ping-pong' moves.
    cpu_overcommit: float = 2.0  # Standard industry default for safe overbooking.

    # Thresholds: No migrations are triggered unless at least one node
    # exceeds these utilization percentages (0-100).
    memory_threshold: Optional[float] = None
    cpu_threshold: Optional[float] = None
    disk_threshold: Optional[float] = None

    # Optional weight overrides (usually derived from balanciness)
    w_balance: Optional[int] = None
    w_stickiness: Optional[int] = None

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
    max_parallel_migrations: Optional[int] = 2
    # max_node_inflow: Prevents transient RAM/CPU peaks by allowing only one entry per host.
    max_node_inflow: int = 1


class Cluster(BaseModel):
    """The complete state of the cluster to be optimized."""
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    balancing: Balancing
    nodes: list[Node]
    vms: list[VM]
    constraints: Constraints
    expect: Expect
    evacuate_node: Optional[str] = None


class Migration(BaseModel):
    model_config = ConfigDict(frozen=True)

    vm: str
    source: str
    target: str


class MigrationStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: int
    migrations: list[Migration]
    parallel: bool


class MigrationPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    steps: list[MigrationStep]
    dependency_edges: list[tuple[str, str]]
    temp_moves: list[str]
    path_feasible: bool = True
    unbreakable_cycle: list[str] = Field(default_factory=list)
    pve_deferred: list[str] = Field(default_factory=list)
    """VMs whose migration is delegated to PVE HA (one per affinity group is
    migrated explicitly; the rest are expected to follow automatically)."""


class SolverStats(BaseModel):
    """Meta-information about the solver execution."""
    model_config = ConfigDict(frozen=True)

    status: str        # e.g. "OPTIMAL", "INFEASIBLE"
    objective: int     # Internal math score (lower is better)
    load_gap: float    # The calculated gap (Max node load - Min node load)
    migration_count: int
    wall_time_ms: float
    migration_cost_gib: int = 0  # Weighted migration cost: RAM GiB + 4×local-disk GiB


class Solution(BaseModel):
    """The resulting target state found by the solver."""
    model_config = ConfigDict(frozen=True)

    feasible: bool
    placements: dict[str, str]  # vm_name -> node_name
    migrations: list[Migration]
    stats: SolverStats
    blocking_vms: list[str] = Field(default_factory=list)
    path_feasible: bool = True  # False if the planner couldn't find a way to get there
    reachability_attempts: int = 1  # How many times we retried solver vs planner


class Expect(BaseModel):
    """Used in YAML scenarios to define what the result SHOULD look like."""
    model_config = ConfigDict(frozen=True)

    feasible: bool = True
    constraints_satisfied: bool = True
    spread_improved: Optional[bool] = None
    max_migrations: Optional[int] = None
    placements: dict[str, str] = Field(default_factory=dict)
    node_empty: Optional[str] = None
    path_feasible: Optional[bool] = None
