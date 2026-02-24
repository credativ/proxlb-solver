"""Unit tests for the migration planner."""

from __future__ import annotations

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


def _make_stats(**kwargs):
    defaults = dict(
        status="OPTIMAL",
        objective=0,
        load_gap=0.0,
        migration_count=0,
        wall_time_ms=1.0,
    )
    defaults.update(kwargs)
    return SolverStats(**defaults)


def _make_cluster(nodes, vms):
    return Cluster(
        name="test",
        description="",
        balancing=Balancing(),
        nodes=nodes,
        vms=vms,
        constraints=Constraints(),
        expect=Expect(),
    )


def test_no_migrations():
    """No migrations should return empty MigrationPlan."""
    cluster = _make_cluster(
        nodes=[Node("n1", 16, 64 * _GB)],
        vms=[VM("v1", "n1", 2, 8 * _GB)],
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


def test_simple_chain():
    """VM-A -> n2, VM-B -> n3: VM-B must move first if n2 is full."""
    nodes = [
        Node("n1", 16, 64 * _GB),
        Node("n2", 16, 64 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 48 * _GB),
        VM("vm-b", "n2", 4, 48 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n3"),
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


def test_step_ordering():
    """Chain A depends on B: Step 1 = B, Step 2 = A."""
    nodes = [
        Node("n1", 16, 64 * _GB),
        Node("n2", 16, 64 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 48 * _GB),
        VM("vm-b", "n2", 4, 48 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n3"),
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


def test_independent_migrations():
    """Independent migrations should all appear."""
    nodes = [
        Node("n1", 16, 64 * _GB),
        Node("n2", 16, 64 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 2, 8 * _GB),
        VM("vm-b", "n2", 2, 8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n3"),
        Migration("vm-b", "n2", "n3"),
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


def test_parallel_detection():
    """Two independent moves should be in 1 step with parallel=True."""
    nodes = [
        Node("n1", 16, 64 * _GB),
        Node("n2", 16, 64 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 2, 8 * _GB),
        VM("vm-b", "n2", 2, 8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n3"),
        Migration("vm-b", "n2", "n3"),
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


def test_cycle_detection():
    """Circular dependency: vm-a→n2, vm-b→n1 with both nodes full."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),  # spare node for cycle breaking
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_cycle_temp_steps():
    """Cycle temp move should be in its own step."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_infeasible_passthrough():
    """Infeasible solution should return empty MigrationPlan."""
    cluster = _make_cluster(
        nodes=[Node("n1", 16, 64 * _GB)],
        vms=[VM("v1", "n1", 2, 8 * _GB)],
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


def test_state_tracking():
    """Node utilization should be correct after each step."""
    nodes = [
        Node("n1", 16, 64 * _GB),
        Node("n2", 16, 64 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 32 * _GB),
        VM("vm-b", "n1", 4, 16 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n1", "n3"),
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
    node_used = defaultdict(int)
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


def test_max_parallel_splits_layer():
    """max_parallel=1 should split 4 independent moves into 4 steps."""
    nodes = [
        Node("n1", 32, 128 * _GB),
        Node("n2", 32, 128 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 2, 8 * _GB),
        VM("vm-b", "n1", 2, 8 * _GB),
        VM("vm-c", "n1", 2, 8 * _GB),
        VM("vm-d", "n1", 2, 8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n1", "n2"),
        Migration("vm-c", "n1", "n2"),
        Migration("vm-d", "n1", "n2"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n2", "vm-c": "n2", "vm-d": "n2"},
        migrations=migrations,
        stats=_make_stats(migration_count=4),
    )

    # Without limit: all 4 in one parallel step
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


def test_max_parallel_with_dependencies():
    """max_parallel should not merge across dependency layers."""
    nodes = [
        Node("n1", 16, 64 * _GB),
        Node("n2", 16, 64 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 48 * _GB),
        VM("vm-b", "n2", 4, 48 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n3"),
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


def test_max_parallel_none_is_unlimited():
    """max_parallel=None should behave the same as no limit."""
    nodes = [
        Node("n1", 32, 128 * _GB),
        Node("n2", 32, 128 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 2, 8 * _GB),
        VM("vm-b", "n1", 2, 8 * _GB),
        VM("vm-c", "n1", 2, 8 * _GB),
    ]
    cluster = _make_cluster(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n1", "n2"),
        Migration("vm-c", "n1", "n2"),
    ]
    sol = Solution(
        feasible=True,
        placements={"vm-a": "n2", "vm-b": "n2", "vm-c": "n2"},
        migrations=migrations,
        stats=_make_stats(migration_count=3),
    )

    result = plan_migrations(cluster, sol, max_parallel=None)
    assert len(result.steps) == 1
    assert len(result.steps[0].migrations) == 3


# ── Constraint-aware temp node selection tests ──────────────────────


def _make_cluster_with_constraints(nodes, vms, constraints=None, **kwargs):
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


def test_temp_move_respects_pin():
    """Cycle-breaking temp node must respect pin constraints.

    vm-a is pinned to {n1, n2} — temp move must NOT go to n4,
    it must go to n3 (or wherever pin allows, excluding source/target).
    """
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),
        Node("n4", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
    ]
    # Pin vm-a to only n1, n2, n3 (not n4)
    constraints = Constraints(
        pin=[{"vm": "vm-a", "nodes": ["n1", "n2", "n3"]}]
    )
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_temp_move_respects_anti_affinity():
    """Temp node must not host an anti-affinity partner.

    vm-a and vm-c are anti-affine. vm-c is on n3.
    So temp move for vm-a must NOT go to n3.
    """
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),
        Node("n4", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
        VM("vm-c", "n3", 2, 4 * _GB),  # anti-affinity partner on n3
    ]
    constraints = Constraints(
        anti_affinity=[{"name": "spread", "vms": ["vm-a", "vm-c"]}]
    )
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_temp_move_respects_ignore():
    """Ignored VMs must never be temp-moved.

    If an ignored VM is in a cycle, it cannot be temp-moved.
    The planner should try another VM in the cycle instead.
    """
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
    ]
    constraints = Constraints(ignore=["vm-a"])
    cluster = _make_cluster_with_constraints(nodes, vms, constraints)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_temp_move_respects_maintenance():
    """Temp node must not be a maintenance node."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB, maintenance=True),
        Node("n4", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
    ]
    cluster = _make_cluster_with_constraints(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_temp_move_respects_ram_capacity():
    """Temp node must have enough RAM for the VM."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),  # has 60GB used already
        Node("n4", 16, 64 * _GB),  # empty — fits
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
        VM("vm-c", "n3", 2, 60 * _GB),  # fills n3
    ]
    cluster = _make_cluster_with_constraints(nodes, vms)

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_temp_move_respects_cpu_capacity():
    """Temp node must have enough CPU (with overcommit) for the VM."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 8, 128 * _GB),   # only 8 CPUs, already 7 used
        Node("n4", 16, 128 * _GB),  # 16 CPUs, empty
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
        VM("vm-c", "n3", 7, 4 * _GB),  # uses 7 of 8 CPUs on n3
    ]
    # cpu_overcommit=1.0 means no overcommit: n3 has 1 CPU free, vm needs 4
    cluster = _make_cluster_with_constraints(
        nodes, vms,
        balancing_kwargs={"cpu_overcommit": 1.0},
    )

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_unbreakable_cycle():
    """Cycle where no VM can be temp-moved → path_feasible=False."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB, maintenance=True),  # only spare, but maintenance
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
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
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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


def test_temp_move_respects_evacuate_node():
    """Temp node must not be the evacuate node."""
    nodes = [
        Node("n1", 16, 60 * _GB),
        Node("n2", 16, 60 * _GB),
        Node("n3", 16, 64 * _GB),
        Node("n4", 16, 64 * _GB),
    ]
    vms = [
        VM("vm-a", "n1", 4, 50 * _GB),
        VM("vm-b", "n2", 4, 50 * _GB),
    ]
    cluster = _make_cluster_with_constraints(
        nodes, vms, evacuate_node="n3",
    )

    migrations = [
        Migration("vm-a", "n1", "n2"),
        Migration("vm-b", "n2", "n1"),
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
