# ProxLB CP-SAT Solver

The ProxLB Solver is a mathematically exact scheduler for Proxmox VE clusters. It uses Google's **OR-Tools CP-SAT** to find the provably global optimum for VM and Container placement, moving beyond simple greedy heuristics.

## Key Features

- **Global Optimization**: Unlike traditional balancers that move one VM at a time, the CP-SAT solver looks at the entire cluster state to find the best possible distribution in a single step.
- **Support for VMs and LXC**: Fully integrated support for both Virtual Machines and LXC Containers.
- **Multi-Resource Balancing**:
  - **Memory (Default)**: Prioritizes RAM distribution (most critical resource).
  - **CPU**: Balances based on actual usage or configured vCPUs.
  - **Disk**: Balances local storage occupancy across nodes.
  - **Smart Modes**: Combines usage metrics with **Pressure Stall Information (PSI)** to resolve resource contention before it impacts performance.
- **Operational Strategy (Modes)**:
  - **Used (Default)**: Balances based on real-time resource consumption.
  - **Assigned**: Balances based on configured resource limits (prevents risky overprovisioning).
  - **PSI**: Balances based on resource pressure/latency.
- **Rich Constraints**:
  - **Affinity / Anti-Affinity**: Both ProxLB-style (tags/pools) and native Proxmox HA groups.
  - **Node Pinning**: Direct VM-to-Node mapping via tags or HA restricted nodes.
  - **Maintenance Mode**: Automatically evacuates nodes while respecting all placement rules.
- **Deadlock-Free Planning**: Includes a migration planner that sequences moves to avoid capacity violations during transitions, including support for temporary "parking" moves to break circular dependencies.

---

## 1. Mathematical Model

The solver represents the cluster as a **Constraint Programming** problem. 

### Variables
For each VM $i$ and node $j$, we define a boolean variable $x_{i,j}$:
- $x_{i,j} = 1$ if VM $i$ is placed on node $j$.
- $x_{i,j} = 0$ otherwise.

Every VM must be assigned to exactly one node: $\sum_{j} x_{i,j} = 1$.

### Objective Function
The solver minimizes a weighted cost function:
$$\text{Minimize: } (w_\text{balance} \cdot \text{Spread}) + (w_\text{stickiness} \cdot \text{MigrationCost}) + \text{Penalty}_\text{SoftRules}$$

*   **Spread**: The difference between the most and least utilized node ($\text{Max} - \text{Min}$), scaled by total capacity.
*   **MigrationCost**: A weighted sum over all migrated VMs — see §2 below.
*   **Penalty**: A massive malus ($1{,}000{,}000$) for every violated soft constraint.

---

## 2. Migration Cost Model

Migrating a VM has a real cost: RAM must be copied live over the network (dirty-page tracking); local disk requires a full sequential copy. The cost model reflects this:

$$\text{cost}(\text{VM}) = \max(1,\ \lfloor \text{RAM} / 256\,\text{MiB} \rfloor) + 4 \times \lfloor \text{LocalDisk} / 256\,\text{MiB} \rfloor$$

The 256 MiB base unit gives enough granularity for the solver to distinguish between a 512 MiB VM (cost 2) and a 1 GiB VM (cost 4). The `max(1, …)` floor ensures tiny containers still have a non-zero weight.

The **4× local disk factor** reflects that copying a local disk (LVM/ZFS) is significantly slower than a RAM live-migration:
- A VM with 4 GiB RAM and no local disk: cost = 16 units
- A VM with 4 GiB RAM and 100 GiB local disk: cost = 16 + 1600 = 1616 units

**Consequence**: when multiple migrations achieve the same balance improvement, the solver automatically prefers moving the VM whose migration is cheapest — smaller RAM footprint and no local disk.

---

## 3. Resource Metrics & Strategy Modes

ProxLB supports multiple optimization dimensions via the `method` parameter:

| Method | Balance objective | Use Case |
| :--- | :--- | :--- |
| `memory` | RAM allocation | Classic memory-based balancing (default). |
| `cpu` | CPU load (cores) | Throughput optimization. |
| `disk` | Local storage usage | Disk capacity balancing. |
| `cpu_psi` | CPU stall time (PSI) | Latency optimization (PVE 9+). |
| `cpu_smart` | CPU load + PSI | Balance of throughput and responsiveness. |
| `global_smart` | RAM + CPU + IO | **Holistic cluster-wide optimization**. |

### RAM: configured allocation vs. actual RSS
The solver balances RAM by **configured allocation**, not actual RSS. This is correct for capacity planning — a VM configured with 4 GiB must be placed on a node that has 4 GiB reserved, regardless of whether it currently uses only 200 MiB.

### CPU: Usage vs. Assigned
- **Used Mode (Default)**: Balances based on the actual measured CPU load (`cpu_used` from Proxmox API). A VM with 8 vCPUs that is idle contributes almost nothing to the load score.
- **Assigned Mode**: Balances based on the *configured number of vCPUs*. This ensures that reserved compute capacity is distributed evenly across the cluster.

### The PSI Footprint Model (CPU, RAM, IO)
[PSI (Pressure Stall Information)](https://www.kernel.org/doc/html/latest/accounting/psi.html) measures resource contention. Since PSI is an *intensive* metric (it doesn't sum up like RAM), the solver uses an **additive footprint model**:
1. Each VM has an individual pressure contribution (e.g., 10% stall time).
2. The solver tries to spread these contributions so that the aggregate pressure on each node stays as low and uniform as possible.

---

## 4. Weight Hierarchy

Optimization is fine-tuned via three distinct tiers:

1.  **Global Level (`w_global_*`)**: Importance of resource pools (e.g., "RAM balance is 10x more important than IO").
2.  **Resource Level (`w_*_usage` vs `w_*_psi`)**: Weighting raw utilization against dynamic pressure stalls.
3.  **VM Level (`priority`)**:
    *   **Priority 3 (High)**: Contribution counts 3× towards the spread calculation.
    *   **Priority 1 (Low)**: Contribution counts 1×.
    *   *Result*: Important VMs "force" their way onto the least loaded nodes.

---

## 5. Constraints

### Hard Constraints (Strict)
Violations result in `INFEASIBLE`.
- **Capacity**: RAM, CPU cores (with overcommit), and named storage pools (ZFS, LVM).
- **Pinning**: Binding VMs to specific hardware. **Pinning is always hard.**
- **Maintenance**: Nodes in maintenance mode are forbidden targets.
- **Hard Rules**: Affinity/Anti-Affinity marked as `hard: true`.

### Rule Origins & Specialized Handling
The solver distinguishes between rules based on their `origin`:

| Origin | Type | Handling | Rationale |
| :--- | :--- | :--- | :--- |
| `pve` | Native HA | **Atomic / Strict** | Proxmox enforces these rules automatically. |
| `plb` | Internal Tags | **Granular / Soft** | ProxLB manages these; allows flexible transitions. |

1.  **PVE Affinity (Atomic)**: Members of a native Proxmox affinity group are moved in the **same execution step**, even if this exceeds `max_parallel_migrations`.
2.  **PVE Anti-Affinity (Strict Ordering)**: If two VMs have native anti-affinity, the planner ensures they **never share a node** even for a split second.
3.  **Internal Rules (Flexible)**: Internal affinity groups (`plb`) are scheduled member-by-member to respect safety limits.

### Soft Constraints (Preferred)
Violated only if resources are exhausted.
- **Soft Rules**: Affinity/Anti-Affinity marked as `hard: false`.

---

## 6. Reachability Guarantee

An optimal state is worthless if it cannot be executed (e.g., no buffer space for a swap).
1. The **Planner** verifies every solution for an executable migration path.
2. It detects dependencies (VM-A must move before VM-B can fit).
3. It detects cycles (A → B → A) and breaks them using **temp-moves** to spare nodes.
4. If a cycle is unbreakable, the state is marked as **"No-Good"**, and the solver searches for the next-best reachable solution.

---

## 7. ProxLB Integration (Shadow & Active Mode)

The solver integrates with ProxLB via two operating modes, configured with `solver.mode`:

### Shadow mode (default, read-only)
The solver runs alongside ProxLB's built-in balancer without changing anything. Every run produces a structured **JSONL log** and an **HTML report** comparing what the solver would have done against what ProxLB actually did.

### Active mode
The solver takes over execution. ProxLB's `Balancing()` class is still used for the actual API calls, but the solver determines which VMs move where. A feedback loop handles migration failures by pinning failed VMs and re-solving.

---

## Administrator Guide: Configuration & Defaults

The ProxLB Solver is tuned for **Stability over Agility** by default.

#### 1. Operational Safety
*   **`max_node_inflow` (Default: 1)**: Only one VM at a time can migrate *into* a host. This prevents memory or CPU peaks that could trigger OOM on the target host.
*   **`max_parallel_migrations` (Default: 2)**: Limits how many migrations can happen simultaneously across the entire cluster. 
*   **`balanciness` (Default: 3 — Moderate)**:
    *   Level 1–2: Only moves VMs for maintenance or hard rule violations.
    *   Level 3: Rebalances only if the spread exceeds ~15%.
    *   Level 5: Chases perfect balance, which may cause frequent low-value migrations.

#### 2. Resource Balancing Strategy
*   **`method` (Default: `memory`)**: RAM is usually the hardest bottleneck. Start with memory balancing before exploring CPU or Smart modes.
*   **`cpu_overcommit` (Default: 2.0)**: Allows assigning more vCPUs than physical cores exist.

---

## Usage for Developers

### Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Running Tests
```bash
pytest
```

### Internal Architecture
- **`models.py`**: Strict type definitions for the cluster state.
- **`adapter.py`**: Bridge between ProxLB's runtime data and the solver models.
- **`solver.py`**: Mathematical model and CP-SAT integration.
- **`planner.py`**: Topological sort and dependency resolution for migrations.
- **`shadow.py`**: Non-intrusive "shadow mode" for live cluster observation.
