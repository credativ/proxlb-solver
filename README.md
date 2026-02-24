# ProxLB CP-SAT Solver

A constraint-programming VM placement solver for [ProxLB](https://github.com/gyptazy/ProxLB).
Uses Google OR-Tools CP-SAT to find optimal VM-to-node assignments while respecting
hard constraints (affinity, anti-affinity, pinning, maintenance, capacity) and
minimizing a weighted objective of load imbalance and migration cost.

## Features

- **CP-SAT optimization** — exact solver, not heuristics. Finds provably optimal placements.
- **DRS-style balanciness** (1–5) — from conservative (no voluntary migrations) to aggressive (chase perfect balance).
- **Multi-faceted CPU Strategy** — vCPUs for hard limits, actual load/usage for balancing.
- **PSI-based balancing** — optimize for minimal resource contention (CPU, RAM, IO pressure).
- **Named Storage Support** — respects multiple local storage pools (ZFS, LVM, etc.) during placement.
- **Resource Reservations** — reserve host capacity for overhead/host system stability.
- **Hard & Soft Constraints** — Affinity and Anti-Affinity rules can be marked as `hard: false` to allow violations in case of resource exhaustion.
- **Strict Pinning** — Pinning rules are always treated as hard constraints to ensure hardware/locality requirements are never violated.
- **VM Priorities** — assign priorities (1-3) to VMs to weight their resource contribution and prioritize important guests.
- **Iterative Feedback Loop** — solver and planner collaborate to find reachable paths for every solution.
- **Migration planner** — orders migrations into executable steps respecting capacity dependencies, detects parallelizable moves, breaks cycles with temp-moves.
- **Reports** — rich terminal output, self-contained HTML with navigation and Mermaid dependency graphs, Markdown, JUnit XML.
- **Live Simulation** — tool to test solver against real cluster snapshots.
- **Constraint validator** — detects conflicts (affinity vs. anti-affinity, pin intersections) before solving, with transitive affinity merging.
- **Scenario-driven testing** — 57 YAML scenarios and 78 total tests covering basic balancing, constraints, infeasible cases, migration chains, and regressions.

## Quick Start

```bash
# Setup
make install

# Run all scenarios and generate reports
make report

# Run tests
make test
```

This produces:

| File | Format | Description |
|------|--------|-------------|
| `results.html` | HTML | Interactive report with sidebar navigation, progress bars, Mermaid graphs |
| `results.md` | Markdown | Full report with tables and code blocks |
| `results.xml` | JUnit XML | CI-compatible test results |

## Usage

```bash
# All reports
python -m proxlb_solver.cli --html results.html --markdown results.md --junit results.xml

# Custom scenario directory
python -m proxlb_solver.cli --scenarios path/to/scenarios --html report.html

# Quiet mode (no terminal output)
python -m proxlb_solver.cli --html results.html --quiet
```

## Scenario Format

Scenarios are YAML files that describe a cluster state and expected outcomes:

```yaml
name: "My Scenario"
description: "Two overloaded nodes, one empty."

balancing:
  method: memory          # memory, cpu, cpu_psi, memory_psi, io_psi, cpu_smart, ...
  balanciness: 3          # 1=conservative, 5=aggressive
  cpu_overcommit: 2.0

nodes:
  node-A:
    cpu_total: 16
    memory_total_gb: 64
    storage_free:         # optional, per named storage pool
      local: 500
      ceph: 1000
    reserve:              # optional host resource reservations
      cpu: 2
      memory_gb: 4
      storage_gb:
        local: 50
    cpu_pressure: 12.5    # optional PSI metrics
    memory_pressure: 3.0
    io_pressure: 0.5
  node-B:
    cpu_total: 16
    memory_total_gb: 64
    maintenance: false     # set true to evacuate

vms:
  web-01:
    node: node-A
    cpu: 4
    memory_gb: 32
    cpu_usage: 2.5         # optional actual CPU load
    disks:                 # optional storage requirements
      local: 50
    type: vm               # vm or ct

constraints:
  affinity:
    - name: web-group
      vms: [web-01, web-02]
  anti_affinity:
    - name: db-spread
      vms: [db-primary, db-replica]
  pin:
    - vm: monitor
      nodes: [node-A]
  ignore: [legacy-app]

expect:
  feasible: true
  constraints_satisfied: true
  spread_improved: true
  max_migrations: 3
  node_empty: node-B          # for evacuation scenarios
  path_feasible: true         # false if migration path is expected to be blocked
  placements:
    web-01: node-A
    web-02: "== web-01"       # same node as web-01
```

## Balanciness Levels

| Level | Name | Behavior |
|-------|------|----------|
| 1 | Conservative | Only mandatory migrations (maintenance, constraints) |
| 2 | Low | Migrate only if load gap > 25% |
| 3 | **Moderate** | Default — balanced cost/benefit, threshold 15% |
| 4 | High | Active rebalancing, threshold 5% |
| 5 | Aggressive | Chase near-perfect balance |

The solver minimizes: `w_balance x LoadGap + w_stickiness x MigrationCount`

## CPU Balancing Strategy (VMware-style)

ProxLB CP-SAT uses a two-tiered approach for CPU management to ensure both stability and performance:

1.  **Hard Capacity Constraint (vCPUs/Cores)**: The solver ensures that the sum of configured vCPUs on a node never exceeds the physical core count multiplied by the `cpu_overcommit` factor. This prevents "logical overloading" where too many VMs are cramped onto a host, even if they are currently idle.
2.  **Soft Optimization Objective (CPU Load)**: When rebalancing, the solver uses the *actual* historical CPU usage (e.g., 1-hour average). It strives to distribute the real compute pressure evenly across the cluster to minimize CPU-Ready time and Steal time.

### Why not just use current usage?
Relying solely on current CPU usage leads to "ghost migrations"—moving VMs to react to short-lived spikes. By using vCPUs as a hard limit and historical load for balancing, we achieve a stable distribution that respects physical limits while optimizing for actual performance.

### PSI-based Balancing (Pressure Stall Information)

ProxLB CP-SAT supports balancing based on **PSI**, a Linux kernel feature that provides a canonical way to measure resource contention. Unlike raw utilization, PSI tells us how long tasks were actually *stalled* waiting for CPU, Memory, or IO.

- **CPU PSI**: Measures stalls due to CPU contention (many processes competing for cycles).
- **Memory PSI**: Measures stalls due to memory pressure (e.g., paging/swapping).
- **IO PSI**: Measures stalls due to storage throughput or latency bottlenecks.

#### The PSI Algorithm in the Solver
Since PSI is an *intensive* metric (it doesn't sum up like RAM bytes), the solver uses an additive contribution model for rebalancing:
1.  **VM Contribution**: Each VM's individual pressure metric (e.g., `some` 10s average) is treated as its "pressure footprint".
2.  **Node Aggregation**: The solver calculates the projected pressure on a node as the sum of its assigned VMs' footprints.
3.  **Optimization**: The solver minimizes the `LoadGap` between node pressure values. This effectively moves "high-pressure" VMs away from nodes that are already experiencing stalls.

### Smart Balancing (Weighted Usage + PSI)

Smart methods combine utilization and PSI into a single composite score:

```yaml
balancing:
  method: cpu_smart      # or memory_smart, io_smart
  w_cpu_usage: 2         # weight for CPU utilization
  w_cpu_psi: 1           # weight for CPU PSI
```

Available methods: `memory`, `cpu`, `cpu_psi`, `memory_psi`, `io_psi`, `cpu_smart`, `memory_smart`, `io_smart`.

#### Further Reading
- [Linux Kernel Documentation: PSI](https://www.kernel.org/doc/html/latest/accounting/psi.html)
- [Proxmox VE: Pressure Stall Information](https://pve.proxmox.com/wiki/Performance_Optimization#PSI)
- [Facebook Engineering: A new way to monitor resource pressure](https://engineering.fb.com/2018/11/20/ml-applications/psi-open-source/)

### Global Smart Balancing (Multi-Resource Optimization)

The `global_smart` method is the most advanced balancing mode. It optimizes RAM, CPU, and IO simultaneously by calculating a composite objective function.

#### Weight Hierarchy
Optimization is controlled by a three-tiered weight system:

1.  **Global Level (`w_global_*`)**: Determines the relative importance of resource pools.
    *   Example: `w_global_mem: 10`, `w_global_cpu: 5` — RAM balance is twice as important as CPU balance.
2.  **Resource Level (`w_*_usage` vs `w_*_psi`)**: Within each resource, determines the balance between raw usage and pressure stalls.
    *   Example: `w_cpu_usage: 1`, `w_cpu_psi: 10` — Solving CPU stalls is prioritized over smoothing out average load.
3.  **Solver Level (`balanciness`)**: Determines the overall aggressiveness (Stickiness vs. Balance).

#### How it works
The solver calculates two "Gaps" (Max minus Min node utilization) for every resource type (one for Usage, one for PSI). It then minimizes the weighted sum of all these gaps. This ensures that a move to improve RAM balance doesn't inadvertently create a CPU bottleneck that is weighted as more critical.

## Live Simulation

You can test the solver against a real Proxmox cluster without performing any actual migrations. This is done in two steps to ensure compatibility with your existing ProxLB installation.

### Step 1: Export Cluster Data
Run the exporter script (located in `scripts/export_proxlb_data.py`) from within your ProxLB directory to create a JSON snapshot:

```bash
cd path/to/ProxLB/proxlb
python3 /path/to/proxlb-solver/scripts/export_proxlb_data.py /path/to/proxlb.yaml /tmp/cluster_dump.json
```

### Step 2: Run Simulation
Use the `simulate` tool to see how the CP-SAT solver would optimize your cluster:

```bash
python3 -m proxlb_solver.simulate /tmp/cluster_dump.json
```

The simulator will show:
- Current vs. Optimal node utilization.
- A list of proposed migrations.
- A step-by-step execution plan (including parallel moves and cycle breaking).

## Migration Planner

The planner takes the solver's flat migration list and produces an executable step plan:

1. **Dependency graph** — VM-A can't move to node-X until VM-B frees space there
2. **Cycle detection** — circular dependencies are broken with temp-moves to a third node
3. **Layered scheduling** (Kahn's algorithm) — independent migrations run in parallel
4. **State tracking** — node utilization is tracked through each step

Example output (HTML report):

```
Step 1 (parallel):
  cache-01:   pve-03 -> pve-05  (80 GB)
  db-replica: pve-02 -> pve-04  (80 GB)
Step 2:
  app-api-02: pve-02 -> pve-03  (48 GB)
Step 3 (parallel):
  app-api-01: pve-01 -> pve-02  (48 GB)
  monitoring: pve-04 -> pve-02  (32 GB)
```

## Reachable Solver (Feedback Loop)

Not every optimal placement is reachable via migrations. The `solve_reachable()` function implements a feedback loop:

1. **Solve** — find optimal placement
2. **Plan** — check if migrations can be ordered without unbreakable cycles
3. **If blocked** — forbid the problematic placement and re-solve
4. **Repeat** until a reachable solution is found or retries are exhausted

This ensures that the final solution is not just valid in the target state, but also has an executable migration path from the current state.

## Constraint Validator

Before solving, the validator checks constraints for logical conflicts:

- **Transitive affinity merging** — if A↔B and B↔C are in affinity, the groups are merged into A↔B↔C
- **Affinity vs. anti-affinity conflicts** — VMs that must be together and apart simultaneously
- **Pin intersection checks** — affinity groups where pins have no common nodes

Conflicts are reported as `RuleConflictError` and surfaced in reports.

## Project Structure

```
proxlb-solver/
  proxlb_solver/
    adapter.py      Converts live ProxLB data to solver models
    cli.py          CLI entry point
    loader.py       YAML scenario parser
    models.py       Dataclasses (Cluster, Node, VM, Migration, MigrationPlan, ...)
    planner.py      Migration ordering and step planning
    reporter.py     Report generation (terminal, HTML, Markdown, JUnit)
    simulate.py     Run solver against real cluster JSON snapshots
    solver.py       CP-SAT constraint model and solver
    validator.py    Constraint conflict detection and affinity merging
  scenarios/
    basic/          Balancing and rebalancing scenarios
    constraints/    Affinity, anti-affinity, pin, maintenance, evacuation
    infeasible/     Deliberately unsolvable scenarios
    migration/      Multi-step migration chains, cycles, parallel moves
    regression/     Bug regression tests
  tests/
    test_integration.py  Integration tests for solve_reachable feedback loop
    test_planner.py      Unit tests for migration planner
    test_solver.py       Parameterized scenario tests
  Makefile
  pyproject.toml
```

## Development

```bash
make install    # create venv, install with dev deps
make test       # pytest with JUnit output
make lint       # flake8
make report     # generate all reports
make clean      # remove venv and generated files
```

## Dependencies

- [Google OR-Tools](https://developers.google.com/optimization) (>= 9.9) — CP-SAT solver
- [PyYAML](https://pyyaml.org/) (>= 6.0) — scenario parsing
- [Rich](https://rich.readthedocs.io/) (>= 13.0) — terminal output
