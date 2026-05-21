"""Unit tests for the migration planner."""

from __future__ import annotations
from typing import Any

from proxlb_solver.models import (
    Balancing,
    Cluster,
    Constraints,
    Expect,
    Migration,
    MigrationPlan,
    MigrationStep,
    Node,
    Solution,
    SolverStats,
    VM,
)
from proxlb_solver.planner import plan_migrations

_GB = 1024 * 1024 * 1024


def _make_stats(**kwargs: Any) -> SolverStats:
    defaults: Any = dict(
        status="OPTIMAL",
        objective=0,
        load_gap=0.0,
        migration_count=0,
        wall_time_ms=1.0,
    )
    defaults.update(kwargs)
    return SolverStats(**defaults)


def _make_cluster(nodes: list[Node], vms: list[VM], balancing: Balancing | None = None) -> Cluster:
    return Cluster(
        name="test",
        description="",
        balancing=balancing or Balancing(max_node_inflow=10),
        nodes=nodes,
        vms=vms,
        constraints=Constraints(),
        expect=Expect(),
    )


def test_no_migrations() -> None:
    """No migrations should return empty MigrationPlan."""
    cluster = _make_cluster(
        nodes=[Node(name="n1", cpu_total=16, memory_total=64 * _GB)],
        vms=[VM(name="v1", node="n1", cpu=2, memory=8 * _GB)],
    )
    sol = Solution(
        feasible=True,
        placements={"v1": "n1"},
        migrations=[],
        stats=_make_stats(),
    )
    result = plan_migrations(cluster, sol)
    assert isinstance(result, MigrationPlan)
    assert result.steps == []
    assert result.dependency_edges == []
    assert result.temp_moves == []


def test_simple_chain() -> None:
    """VM-A -> n2, VM-B -> n3: VM-B must move first if n2 is full."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=64 * _GB),
        Node(name="n2", cpu_total=16, memory_total=64 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=48 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=48 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n3"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n3"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert isinstance(result, MigrationPlan)
    assert len(result.steps) == 2
    # Step 1 should be vm-b (frees space on n2)
    assert result.steps[0].migrations[0].vm == "vm-b"
    # Step 2 should be vm-a (moves to n2)
    assert result.steps[1].migrations[0].vm == "vm-a"


def test_step_ordering() -> None:
    """Chain A depends on B: Step 1 = B, Step 2 = A."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=64 * _GB),
        Node(name="n2", cpu_total=16, memory_total=64 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=48 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=48 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n3"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n3"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    # Dependency: vm-a waits for vm-b
    assert ("vm-a", "vm-b") in result.dependency_edges
    # Step numbers are sequential
    assert result.steps[0].step == 1
    assert result.steps[1].step == 2


def test_independent_migrations() -> None:
    """Independent migrations should all appear."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=64 * _GB),
        Node(name="n2", cpu_total=16, memory_total=64 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-b", node="n2", cpu=2, memory=8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n3"),
        Migration(vm="vm-b", source="n2", target="n3"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n3", "vm-b": "n3"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert isinstance(result, MigrationPlan)
    all_vms = {m.vm for s in result.steps for m in s.migrations}
    assert all_vms == {"vm-a", "vm-b"}


def test_parallel_detection() -> None:
    """Two independent moves should be in 1 step with parallel=True."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=64 * _GB),
        Node(name="n2", cpu_total=16, memory_total=64 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-b", node="n2", cpu=2, memory=8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n3"),
        Migration(vm="vm-b", source="n2", target="n3"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n3", "vm-b": "n3"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    # Both should be in the same step (no dependencies)
    assert len(result.steps) == 1
    assert result.steps[0].parallel is True
    assert len(result.steps[0].migrations) == 2


def test_cycle_detection() -> None:
    """Circular dependency: vm-a→n2, vm-b→n1 with both nodes full."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),  # spare node for cycle breaking
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert isinstance(result, MigrationPlan)
    # Should have temp moves
    assert len(result.temp_moves) > 0
    # All original VMs should reach their final targets
    final = {}
    for step in result.steps:
        for m in step.migrations:
            final[m.vm] = m.target
    assert final["vm-a"] == "n2"
    assert final["vm-b"] == "n1"


def test_cycle_temp_steps() -> None:
    """Cycle temp move should be in its own step."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    # First step should be a temp move (to n3)
    first_step = result.steps[0]
    temp_vm = result.temp_moves[0]
    temp_mig = first_step.migrations[0]
    assert temp_mig.vm == temp_vm
    assert temp_mig.target == "n3"  # temp node


def test_infeasible_passthrough() -> None:
    """Infeasible solution should return empty MigrationPlan."""
    cluster = _make_cluster(
        nodes=[Node(name="n1", cpu_total=16, memory_total=64 * _GB)],
        vms=[VM(name="v1", node="n1", cpu=2, memory=8 * _GB)],
    )
    sol = Solution(
        feasible=False,
        placements={},
        migrations=[],
        stats=_make_stats(status="INFEASIBLE"),
    )
    result = plan_migrations(cluster, sol)
    assert isinstance(result, MigrationPlan)
    assert result.steps == []


def test_state_tracking() -> None:
    """Node utilization should be correct after each step."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=64 * _GB),
        Node(name="n2", cpu_total=16, memory_total=64 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=32 * _GB),
        VM(name="vm-b", node="n1", cpu=4, memory=16 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n1", target="n3"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n3"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    # Both should be parallel (no deps), single step
    assert len(result.steps) == 1

    # Simulate state tracking
    from collections import defaultdict
    node_used: dict[str, int] = defaultdict(int)
    for vm in cluster.vms:
        node_used[vm.node] += vm.memory

    # Initial: n1=48GB, n2=0, n3=0
    assert node_used["n1"] == 48 * _GB
    assert node_used["n2"] == 0
    assert node_used["n3"] == 0

    vm_map = {v.name: v for v in vms}
    for step in result.steps:
        for m in step.migrations:
            vm = vm_map[m.vm]
            node_used[m.source] -= vm.memory
            node_used[m.target] += vm.memory

    # After: n1=0, n2=32GB, n3=16GB
    assert node_used["n1"] == 0
    assert node_used["n2"] == 32 * _GB
    assert node_used["n3"] == 16 * _GB


def test_max_parallel_splits_layer() -> None:
    """max_parallel=1 should split 4 independent moves into 4 steps."""
    nodes = [
        Node(name="n1", cpu_total=32, memory_total=128 * _GB),
        Node(name="n2", cpu_total=32, memory_total=128 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-b", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-c", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-d", node="n1", cpu=2, memory=8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n1", target="n2"),
        Migration(vm="vm-c", source="n1", target="n2"),
        Migration(vm="vm-d", source="n1", target="n2"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n2", "vm-c": "n2", "vm-d": "n2"},
        migrations=migrations,
        stats=_make_stats(migration_count=4),
    )

    # Without limit: all 4 in one parallel step
    cluster = cluster.model_copy(update={"balancing": cluster.balancing.model_copy(update={"max_parallel_migrations": 100})})
    unlimited = plan_migrations(cluster, sol)
    assert len(unlimited.steps) == 1
    assert len(unlimited.steps[0].migrations) == 4
    assert unlimited.steps[0].parallel is True

    # max_parallel=1: each migration gets its own step
    result = plan_migrations(cluster, sol, max_parallel=1)
    assert len(result.steps) == 4
    for step in result.steps:
        assert len(step.migrations) == 1
        assert step.parallel is False

    # max_parallel=2: two steps of 2
    result2 = plan_migrations(cluster, sol, max_parallel=2)
    assert len(result2.steps) == 2
    for step in result2.steps:
        assert len(step.migrations) == 2
        assert step.parallel is True

    # All VMs still present
    all_vms = {m.vm for s in result.steps for m in s.migrations}
    assert all_vms == {"vm-a", "vm-b", "vm-c", "vm-d"}


def test_max_parallel_with_dependencies() -> None:
    """max_parallel should not merge across dependency layers."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=64 * _GB),
        Node(name="n2", cpu_total=16, memory_total=64 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=48 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=48 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n3"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n3"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    # max_parallel=10 (high limit) — still 2 steps because of dependency
    result = plan_migrations(cluster, sol, max_parallel=10)
    assert len(result.steps) == 2
    assert result.steps[0].migrations[0].vm == "vm-b"
    assert result.steps[1].migrations[0].vm == "vm-a"


def test_max_parallel_none_is_unlimited() -> None:
    """max_parallel=None should behave the same as no limit."""
    nodes = [
        Node(name="n1", cpu_total=32, memory_total=128 * _GB),
        Node(name="n2", cpu_total=32, memory_total=128 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-b", node="n1", cpu=2, memory=8 * _GB),
        VM(name="vm-c", node="n1", cpu=2, memory=8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n1", target="n2"),
        Migration(vm="vm-c", source="n1", target="n2"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n2", "vm-c": "n2"},
        migrations=migrations,
        stats=_make_stats(migration_count=3),
    )

    cluster = cluster.model_copy(update={"balancing": cluster.balancing.model_copy(update={"max_parallel_migrations": 100})})
    result = plan_migrations(cluster, sol, max_parallel=None)
    assert len(result.steps) == 1
    assert len(result.steps[0].migrations) == 3


# ── Constraint-aware temp node selection tests ──────────────────────


def _make_cluster_with_constraints(nodes: list[Node], vms: list[VM], constraints: Constraints | None = None, **kwargs: Any) -> Cluster:
    return Cluster(
        name="test",
        description="",
        balancing=Balancing(**kwargs.get("balancing_kwargs", {})),
        nodes=nodes,
        vms=vms,
        constraints=constraints or Constraints(),
        expect=Expect(),
        evacuate_node=kwargs.get("evacuate_node"),
    )


def test_temp_move_respects_pin() -> None:
    """Cycle-breaking temp node must respect pin constraints.

    vm-a is pinned to {n1, n2} — temp move must NOT go to n4,
    it must go to n3 (or wherever pin allows, excluding source/target).
    """
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
        Node(name="n4", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    # Pin vm-a to only n1, n2, n3 (not n4)
    constraints = Constraints(
        pin=[{"vm": "vm-a", "nodes": ["n1", "n2", "n3"]}]
    )
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert len(result.temp_moves) > 0

    # The temp move target must be n3 (n4 is not in pin list)
    temp_mig = result.steps[0].migrations[0]
    assert temp_mig.vm in result.temp_moves
    assert temp_mig.target == "n3"


def test_temp_move_respects_anti_affinity() -> None:
    """Temp node must not host an anti-affinity partner.

    vm-a and vm-c are anti-affine. vm-c is on n3.
    So temp move for vm-a must NOT go to n3.
    """
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
        Node(name="n4", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
        VM(name="vm-c", node="n3", cpu=2, memory=4 * _GB),  # anti-affinity partner on n3
    ]
    constraints = Constraints(
        anti_affinity=[{"name": "spread", "vms": ["vm-a", "vm-c"]}]
    )
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert len(result.temp_moves) > 0

    # Temp move must go to n4, not n3 (anti-affinity with vm-c)
    temp_mig = result.steps[0].migrations[0]
    assert temp_mig.target == "n4"


def test_temp_move_respects_ignore() -> None:
    """Ignored VMs must never be temp-moved.

    If an ignored VM is in a cycle, it cannot be temp-moved.
    The planner should try another VM in the cycle instead.
    """
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    constraints = Constraints(ignore=["vm-a"])
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    # vm-a is ignored so cannot be temp-moved; vm-b should be temp-moved
    if result.temp_moves:
        assert "vm-a" not in result.temp_moves
        assert "vm-b" in result.temp_moves


def test_temp_move_respects_maintenance() -> None:
    """Temp node must not be a maintenance node."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB, maintenance=True),
        Node(name="n4", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    cluster = _make_cluster_with_constraints(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert len(result.temp_moves) > 0

    # Temp move must go to n4, not n3 (maintenance)
    temp_mig = result.steps[0].migrations[0]
    assert temp_mig.target == "n4"


def test_temp_move_respects_ram_capacity() -> None:
    """Temp node must have enough RAM for the VM."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),  # has 60GB used already
        Node(name="n4", cpu_total=16, memory_total=64 * _GB),  # empty — fits
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
        VM(name="vm-c", node="n3", cpu=2, memory=60 * _GB),  # fills n3
    ]
    cluster = _make_cluster_with_constraints(nodes, vms)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert len(result.temp_moves) > 0

    # n3 has only 4GB free (64-60), vm needs 50GB — must use n4
    temp_mig = result.steps[0].migrations[0]
    assert temp_mig.target == "n4"


def test_temp_move_respects_cpu_capacity() -> None:
    """Temp node must have enough CPU (with overcommit) for the VM."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=8, memory_total=128 * _GB),   # only 8 CPUs, already 7 used
        Node(name="n4", cpu_total=16, memory_total=128 * _GB),  # 16 CPUs, empty
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
        VM(name="vm-c", node="n3", cpu=7, memory=4 * _GB),  # uses 7 of 8 CPUs on n3
    ]
    # cpu_overcommit=1.0 means no overcommit: n3 has 1 CPU free, vm needs 4
    cluster = _make_cluster_with_constraints(
        nodes, vms,
        balancing_kwargs={"cpu_overcommit": 1.0},
    )

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert len(result.temp_moves) > 0

    # n3 can't fit 4 CPUs (only 1 free with overcommit=1.0) — must use n4
    temp_mig = result.steps[0].migrations[0]
    assert temp_mig.target == "n4"


def test_unbreakable_cycle() -> None:
    """Cycle where no VM can be temp-moved → path_feasible=False."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB, maintenance=True),  # only spare, but maintenance
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    # Both pinned away from n3
    constraints = Constraints(
        pin=[
            {"vm": "vm-a", "nodes": ["n1", "n2"]},
            {"vm": "vm-b", "nodes": ["n1", "n2"]},
        ]
    )
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert result.path_feasible is False
    assert set(result.unbreakable_cycle) == {"vm-a", "vm-b"}
    assert result.steps == []


def test_long_cycle_with_full_nodes_is_path_infeasible() -> None:
    """Regression test for issue #1: long cycle where node-spare (2 GB) cannot
    accommodate vm-0 (3 GB), making the migration sequence unreachable even
    though the solver found a valid final placement.

    The iterative cycle-breaker should exhaust all parking options and return
    path_feasible=False, triggering solve_reachable to retry or report failure.
    """
    nodes = [
        Node(name="node-spare-init", cpu_total=2, memory_total=2 * _GB),
        Node(name="node-0", cpu_total=4, memory_total=4 * _GB),
        Node(name="node-1", cpu_total=4, memory_total=4 * _GB),
        Node(name="node-spare", cpu_total=2, memory_total=2 * _GB),
    ]
    vms = [
        VM(name="vm-0", node="node-1", cpu=3, memory=3 * _GB),
        VM(name="vm-1", node="node-0", cpu=1, memory=1 * _GB),
        VM(name="vm-2", node="node-0", cpu=1, memory=1 * _GB),
        VM(name="vm-3", node="node-1", cpu=1, memory=1 * _GB),
        VM(name="vm-4", node="node-0", cpu=2, memory=2 * _GB),
        VM(name="vm-spare", node="node-spare-init", cpu=2, memory=2 * _GB),
    ]
    constraints = Constraints(pin=[
        {"vm": "vm-0", "nodes": ["node-0", "node-spare"]},
        {"vm": "vm-1", "nodes": ["node-1", "node-spare"]},
        {"vm": "vm-2", "nodes": ["node-1", "node-spare"]},
        {"vm": "vm-3", "nodes": ["node-0", "node-spare"]},
        {"vm": "vm-4", "nodes": ["node-1", "node-spare"]},
        {"vm": "vm-spare", "nodes": ["node-spare"]},
    ])
    cluster = Cluster(
        name="long cycle with full nodes",
        description="",
        balancing=Balancing(cpu_overcommit=1, max_node_inflow=10),
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=Expect(),
    )
    # Valid final placement found by solver
    sol = Solution(
        feasible=True,
        placements={
            "vm-0": "node-0", "vm-1": "node-1", "vm-2": "node-1",
            "vm-3": "node-0", "vm-4": "node-1", "vm-spare": "node-spare",
        },
        migrations=[
            Migration(vm="vm-0", source="node-1", target="node-0"),
            Migration(vm="vm-1", source="node-0", target="node-1"),
            Migration(vm="vm-2", source="node-0", target="node-1"),
            Migration(vm="vm-3", source="node-1", target="node-0"),
            Migration(vm="vm-4", source="node-0", target="node-1"),
            Migration(vm="vm-spare", source="node-spare-init", target="node-spare"),
        ],
        stats=_make_stats(migration_count=6),
    )

    result = plan_migrations(cluster, sol)
    assert result.path_feasible is False, (
        "Plan should be path_feasible=False: node-spare (2 GB) cannot park "
        "vm-0 (3 GB), so the cycle cannot be fully broken."
    )


def test_temp_move_respects_evacuate_node() -> None:
    """Temp node must not be the evacuate node."""
    nodes = [
        Node(name="n1", cpu_total=16, memory_total=60 * _GB),
        Node(name="n2", cpu_total=16, memory_total=60 * _GB),
        Node(name="n3", cpu_total=16, memory_total=64 * _GB),
        Node(name="n4", cpu_total=16, memory_total=64 * _GB),
    ]
    vms = [
        VM(name="vm-a", node="n1", cpu=4, memory=50 * _GB),
        VM(name="vm-b", node="n2", cpu=4, memory=50 * _GB),
    ]
    cluster = _make_cluster_with_constraints(
        nodes, vms, evacuate_node="n3",
    )

    migrations = [
        Migration(vm="vm-a", source="n1", target="n2"),
        Migration(vm="vm-b", source="n2", target="n1"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n1"},
        migrations=migrations,
        stats=_make_stats(migration_count=2),
    )

    result = plan_migrations(cluster, sol)
    assert len(result.temp_moves) > 0

    # n3 is evacuate node — must use n4
    temp_mig = result.steps[0].migrations[0]
    assert temp_mig.target == "n4"
