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

### 3.2 State Transitions
Migration is defined as a change in position between `t` and `t+1`. We introduce a transition variable `m[i, t]`:
```text
m[i, t] >= x[i, j, t] - x[i, j, t+1]  (for all nodes j)
```
This allows the solver to count migrations and enforce operational limits (like `max_parallel_migrations`) for every step.

---

## 4. Mathematical Constraints

### 4.1 Transient Capacity & Safety
In every step `t`, the node capacity must be respected. To ensure safety during the migration process, the model enforces:
```text
Sum(Usage[i] * x[i, j, t]) <= Capacity[j]  (for all nodes j, steps t)
```
This ensures that at no point in time—neither at the start, the middle, or the end of the plan—is a node oversubscribed.

### 4.2 Soft Capacity Slack
In 100% full clusters, a pure discrete model is too rigid. We introduce **Slack Variables** `s[j, t]`:
```text
Sum(Usage[i] * x[i, j, t]) <= Capacity[j] + s[j, t]
```
Slack allows a temporary, minimal overcommit at an extreme cost penalty. This "greases" the gears of the solver to allow swaps in congested environments.

### 4.3 Soft-Rule Repair Logic
If a cluster starts in an "illegal" state (e.g. affinity violation at t=0), a hard constraint would cause an `INFEASIBLE` result. We model rules as **Soft Constraints**:
```text
Penalty = Sum(Violations) * RulePenalty
```
Since the `RulePenalty` is much higher than the cost of a migration, the solver is mathematically "forced" to plan a move to fix the state.

---

## 5. Objective Function
The solver minimizes a composite cost function at state T:
```text
Minimize: (Weight_Balance * LoadGap[T]) 
        + (Weight_Stickiness * Sum(m[i, t] * (t + 1))) 
        + SlackPenalty 
        + RulePenalty
```

1.  **LoadGap**: Minimizes the difference between the most and least loaded nodes.
2.  **Time-Weighted Stickiness**: Multiplies migrations by `(t + 1)` to encourage the solver to perform moves as early as possible.
3.  **Penalties**: Ensure that capacity and rules are respected unless impossible.

---

## 6. Algorithmic Strategy: Iterative Deepening
Large `T` values exponentially increase the search space (`Guests * Nodes * T`).
The solver uses **Iterative Deepening**:
1.  Try `T=1` (Direct moves).
2.  If infeasible or no improvement, try `T=2` (Simple swaps).
3.  Expand to `T=4, 8, 16, 32` for complex cascades.

This strategy ensures that simple rebalancing stays fast (milliseconds), while complex deadlocks are resolved with deeper search (seconds).

---

## 7. Conclusion
The Unified Model transforms a heuristic pathfinding problem into a rigorous mathematical proof. By integrating the "how" into the "where", ProxLB achieves:
*   **Guaranteed Reachability**: Any plan found is executable by definition.
*   **Atomic Group Moves**: PVE-native affinity is handled as a single unit of change.
*   **Deadlock Resolution**: Automatic discovery of "parking" moves via spare nodes.

This architecture represents the next generation of data center scheduling, moving from "best-effort" heuristics to "provably-optimal" orchestration.
