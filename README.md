# ProxLB CP-SAT Solver

A constraint-programming VM placement solver for [ProxLB](https://github.com/gyptazy/ProxLB).
Uses Google OR-Tools CP-SAT to find optimal VM-to-node assignments while respecting
hard constraints (affinity, anti-affinity, pinning, maintenance, capacity) and
minimizing a weighted objective of load imbalance and migration cost.

## Features

- **CP-SAT optimization** — exact solver, not heuristics. Finds provably optimal placements.
- **DRS-style balanciness** (1–5) — from conservative (no voluntary migrations) to aggressive (chase perfect balance).
- **Hard constraints** — affinity, anti-affinity, pin-to-node, ignore, maintenance evacuation.
- **Migration planner** — orders migrations into executable steps respecting capacity dependencies, detects parallelizable moves, breaks cycles with temp-moves.
- **Reports** — rich terminal output, self-contained HTML with navigation and Mermaid dependency graphs, Markdown, JUnit XML.
- **Scenario-driven testing** — 27 YAML scenarios covering basic balancing, constraints, infeasible cases, migration chains, and regressions.

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
  method: memory
  balanciness: 3        # 1=conservative, 5=aggressive
  cpu_overcommit: 2.0

nodes:
  node-A:
    cpu_total: 16
    memory_total_gb: 64
  node-B:
    cpu_total: 16
    memory_total_gb: 64
    maintenance: false   # set true to evacuate

vms:
  web-01:
    node: node-A
    cpu: 4
    memory_gb: 32
    type: vm

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

## Project Structure

```
proxlb-solver/
  proxlb_solver/
    cli.py          CLI entry point
    loader.py       YAML scenario parser
    models.py       Dataclasses (Cluster, Node, VM, Migration, MigrationPlan, ...)
    planner.py      Migration ordering and step planning
    reporter.py     Report generation (terminal, HTML, Markdown, JUnit)
    solver.py       CP-SAT constraint model and solver
  scenarios/
    basic/          Balancing and rebalancing scenarios
    constraints/    Affinity, anti-affinity, pin, maintenance, evacuation
    infeasible/     Deliberately unsolvable scenarios
    migration/      Multi-step migration chains, cycles, parallel moves
    regression/     Bug regression tests
  tests/
    test_planner.py Unit tests for migration planner
    test_solver.py  Parameterized scenario tests
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
