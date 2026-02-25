"""Data models for the ProxLB CP-SAT solver."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    name: str
    cpu_total: int
    memory_total: int  # bytes
    storage_free: dict[str, int] = field(default_factory=dict)  # name -> free bytes
    cpu_reserve: int = 0  # cores
    memory_reserve: int = 0  # bytes
    storage_reserve: dict[str, int] = field(default_factory=dict)  # name -> bytes
    cpu_pressure: float = 0.0  # PSI 'some' percentage
    memory_pressure: float = 0.0
    io_pressure: float = 0.0
    maintenance: bool = False


@dataclass(frozen=True)
class VM:
    name: str
    node: str  # current placement
    cpu: int   # configured vCPUs (cores)
    memory: int  # bytes
    cpu_usage: float = 0.0  # actual load (e.g. 0.5 for 50% of 1 core)
    cpu_pressure: float = 0.0  # PSI 'some' percentage
    memory_pressure: float = 0.0
    io_pressure: float = 0.0
    disks: dict[str, int] = field(default_factory=dict)  # name -> required bytes
    priority: int = 2  # 1=Low, 2=Normal, 3=High
    vm_type: str = "vm"  # "vm" or "ct"


@dataclass(frozen=True)
class Constraints:
    affinity: list[dict] = field(default_factory=list)
    anti_affinity: list[dict] = field(default_factory=list)
    pin: list[dict] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Expect:
    feasible: bool = True
    constraints_satisfied: bool = True
    spread_improved: bool | None = None
    max_migrations: int | None = None
    placements: dict[str, str] = field(default_factory=dict)
    node_empty: str | None = None  # assert this node has 0 VMs after solve
    path_feasible: bool | None = None  # None = don't check; True/False = assert


@dataclass(frozen=True)
class Balancing:
    method: str = "memory"
    balanciness: int = 3  # 1-5, VMware DRS-style aggressiveness
    cpu_overcommit: float = 2.0
    w_balance: int | None = None  # override; derived from balanciness if None
    w_stickiness: int | None = None  # override; derived from balanciness if None
    w_cpu_usage: int = 1  # weighting factor for usage in cpu_smart mode
    w_cpu_psi: int = 1    # weighting factor for PSI in cpu_smart mode
    w_mem_usage: int = 1  # weighting factor for usage in memory_smart mode
    w_mem_psi: int = 1    # weighting factor for PSI in memory_smart mode
    w_io_usage: int = 1   # weighting factor for usage in io_smart mode
    w_io_psi: int = 1     # weighting factor for PSI in io_smart mode
    w_global_mem: int = 10 # Importance of RAM in global_smart mode
    w_global_cpu: int = 10 # Importance of CPU in global_smart mode
    w_global_io: int = 1   # Importance of IO in global_smart mode
    max_parallel_migrations: int | None = None # Global limit
    max_node_inflow: int = 1 # Max VMs migrating TO a node at once


@dataclass(frozen=True)
class Cluster:
    name: str
    description: str
    balancing: Balancing
    nodes: list[Node]
    vms: list[VM]
    constraints: Constraints
    expect: Expect
    evacuate_node: str | None = None  # node to evacuate (drain all VMs)


@dataclass(frozen=True)
class Migration:
    vm: str
    source: str
    target: str


@dataclass(frozen=True)
class MigrationStep:
    step: int                    # 1-based step number
    migrations: list[Migration]  # migrations executable in this step
    parallel: bool               # True if >1 migration can run concurrently


@dataclass(frozen=True)
class MigrationPlan:
    steps: list[MigrationStep]
    dependency_edges: list[tuple[str, str]]  # (vm_a, vm_b) = "a waits for b"
    temp_moves: list[str]                     # VMs that need a temp move
    path_feasible: bool = True                # False if cycles can't be broken
    unbreakable_cycle: list[str] = field(default_factory=list)  # VMs in unbreakable cycle


@dataclass(frozen=True)
class SolverStats:
    status: str
    objective: int
    load_gap: float
    migration_count: int
    wall_time_ms: float


@dataclass(frozen=True)
class Solution:
    feasible: bool
    placements: dict[str, str]  # vm_name -> node_name
    migrations: list[Migration]
    stats: SolverStats
    blocking_vms: list[str] = field(default_factory=list)  # VMs preventing evacuation
    path_feasible: bool = True  # False if feedback loop failed to find executable path
    reachability_attempts: int = 1  # how many solve_reachable iterations
