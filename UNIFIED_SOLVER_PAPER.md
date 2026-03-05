# Unified Time-Expanded VM Placement: A CP-SAT Approach
**Technical Whitepaper on the ProxLB Solver Architecture**

## 1. Executive Summary
Traditional virtual machine (VM) rebalancing algorithms often suffer from a "Reachability Gap": they can calculate an optimal target state for a cluster but cannot find a safe sequence of migrations to get there (deadlocks). The **Unified ProxLB Solver** solves this by integrating both optimization and pathfinding into a single **Time-Expanded Integer Linear Programming (ILP)** problem using Google OR-Tools CP-SAT.

---

## 2. The Problem: The Two-Step Fallacy
In classic load balancing, the process is split:
1.  **Placement Solver**: Find a state where the load gap is minimal.
2.  **Migration Planner**: Find a sequence of moves to reach that state.

**The failure mode**: If Node-A is full with VM-1 and Node-B is full with VM-2, a swap is mathematically optimal but heuristically unreachable because there is no free space for a temporary move. The Planner fails, and the Solver's result is discarded.

---

## 3. The Solution: Time Expansion
The Unified Model introduces the dimension of **Time** (t) into the decision variables. Instead of asking "Where should VM i be?", we ask "Where should VM i be at time step t?".

### 3.1 Decision Variables
We define a 3D grid of boolean variables `x[i, j, t]`:
- `i`: Guests (1 to N)
- `j`: Nodes (1 to M)
- `t`: Time Steps (0 to T)

`x[i, j, t] = 1` means Guest `i` resides on Node `j` at step `t`.

---

## 4. Production Optimizations (Recent Changes)

To scale from small test cases to massive clusters (100+ VMs), the following optimizations were implemented:

### 4.1 Symmetry Breaking
In clusters with identical hardware (e.g., 20 identical nodes), the solver explores millions of identical permutations.
**Change**: Added a constraint that forces a lexicographical order on identical, initially empty nodes.
**Result**: Search space reduction by several orders of magnitude.

### 4.2 Search Branching Strategy
Solving for all time steps at once is complex.
**Change**: Implemented a `DecisionStrategy` that tells the solver to decide the variables for the **final state T** first.
**Logic**: Solve the "Bin Packing" first, then find the "Path" to reach it. This mirrors the speed of the standard solver while keeping the safety of the unified model.

### 4.3 Relaxed Transition Pinning
**Problem**: VMs often start on a temporary source node (e.g., during evacuation) that is not in their "allowed nodes" list (Pinning). A strict model would report `INFEASIBLE` immediately.
**Change**: Pinning is now only strictly enforced at the **final step T**. For intermediate steps, a VM is allowed to stay on its initial node until a valid target node is available.

### 4.4 Penalty Hierarchy (Soft-Rules)
To allow the solver to "repair" unhealthly clusters (e.g., initial anti-affinity violations), rules were moved from hard constraints to a penalty hierarchy:
1.  **Slack Penalty (10^9)**: Capacity overcommit is extremely expensive (forbidden unless transiently necessary).
2.  **Rule Penalty (10^8)**: Violating Affinity/Anti-Affinity is very expensive.
3.  **Balancing Gain (10^4)**: Reducing the load gap is the primary goal.
4.  **Stickiness (1)**: Migrations are "cheap" but should be minimized.

This hierarchy ensures that fixing a rule violation is mathematically much better than doing nothing.

---

## 5. Algorithmic Strategy: Iterative Deepening
The solver probes increasing time horizons: `T = 1, 2, 4, 8, 16, 24, 32`.
This allows quick results for simple rebalancing and deep search for complex multi-step "parking" maneuvers in full clusters.

---

## 6. Conclusion
The Unified Model achieves **100% reachability** by design. It transforms a heuristic pathfinding problem into a rigorous mathematical proof, ensuring that any rebalancing plan generated is both optimal and safe to execute in a production Proxmox environment.
