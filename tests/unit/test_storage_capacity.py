"""
Tests for the per-storage capacity constraint (inflow-only semantics).

A node's storage_free already excludes the disks of resident guests, so the
constraint must charge only guests moving IN. Space freed by guests moving
away must not be credited (it only materialises after their migration
completes).

Also covers the placement effects that fall out of per-storage capacities:
guests whose storage id exists nowhere else are pinned, and several incoming
guests must jointly fit into the target storage.
"""

from __future__ import annotations

from proxlb_solver.models import Balancing, Cluster, Constraints, Expect, Node, VM
from proxlb_solver.solver import solve

_GB = 1024 ** 3


def _node(name: str, storage_free: dict[str, int]) -> Node:
    return Node(name=name, cpu_total=32, memory_total=128 * _GB,
                storage_free=storage_free)


def _vm(name: str, node: str, disks: dict[str, int]) -> VM:
    return VM(name=name, node=node, cpu=2, memory=4 * _GB, cpu_usage=0.1,
              disks=disks)


def _cluster(nodes: list[Node], vms: list[VM], evacuate: str | None = None) -> Cluster:
    return Cluster(
        name="storage-capacity", description="",
        balancing=Balancing(),
        nodes=nodes, vms=vms,
        constraints=Constraints(), expect=Expect(),
        evacuate_node=evacuate,
    )


def test_resident_guest_larger_than_avail_stays_feasible():
    """A thin-provisioned guest whose disks exceed its own node's remaining
    avail must not make the model infeasible: its bytes are already excluded
    from storage_free and it is not charged while staying put."""
    cluster = _cluster(
        nodes=[_node("node1", {"s1": 10 * _GB}), _node("node2", {"s1": 100 * _GB})],
        vms=[_vm("vm-big", "node1", {"s1": 50 * _GB})],
    )
    solution = solve(cluster, time_limit_s=5.0)
    assert solution.feasible
    assert solution.placements["vm-big"] == "node1"


def test_evacuation_blocked_when_target_storage_too_small():
    """An incoming guest is charged against the target storage: evacuation
    must fail when its disks do not fit anywhere."""
    cluster = _cluster(
        nodes=[_node("node1", {"s1": 100 * _GB}), _node("node2", {"s1": 20 * _GB})],
        vms=[_vm("vm-big", "node1", {"s1": 50 * _GB})],
        evacuate="node1",
    )
    solution = solve(cluster, time_limit_s=5.0)
    assert not solution.feasible
    assert "vm-big" in solution.blocking_vms


def test_evacuation_succeeds_when_target_storage_fits():
    cluster = _cluster(
        nodes=[_node("node1", {"s1": 100 * _GB}), _node("node2", {"s1": 60 * _GB})],
        vms=[_vm("vm-big", "node1", {"s1": 50 * _GB})],
        evacuate="node1",
    )
    solution = solve(cluster, time_limit_s=5.0)
    assert solution.feasible
    assert solution.placements["vm-big"] == "node2"


def test_multiple_incoming_guests_must_jointly_fit():
    """Two evacuating guests (30G + 40G) exceed any single target storage,
    so the solver must split them across targets — greedy per-guest checks
    cannot see this."""
    cluster = _cluster(
        nodes=[
            _node("node1", {"s1": 100 * _GB}),
            _node("node2", {"s1": 60 * _GB}),
            _node("node3", {"s1": 45 * _GB}),
        ],
        vms=[
            _vm("vm-a", "node1", {"s1": 30 * _GB}),
            _vm("vm-b", "node1", {"s1": 40 * _GB}),
        ],
        evacuate="node1",
    )
    solution = solve(cluster, time_limit_s=5.0)
    assert solution.feasible
    targets = {solution.placements["vm-a"], solution.placements["vm-b"]}
    assert "node1" not in targets
    assert targets == {"node2", "node3"}  # 70G into one storage is impossible
    # vm-b (40G) cannot land on node3 (45G would fit) — both fit either way
    # except 30+40 on a single node; verify per-target inflow is within avail.
    inflow: dict[str, int] = {}
    for vm in cluster.vms:
        tgt = solution.placements[vm.name]
        inflow[tgt] = inflow.get(tgt, 0) + vm.disks["s1"]
    for node in cluster.nodes:
        if node.name != "node1":
            assert inflow.get(node.name, 0) <= node.storage_free["s1"]


def test_guest_pinned_when_storage_exists_nowhere_else():
    """Per-node unique storage ids (node-local dir storages): without remap
    the guest's storage does not exist on other nodes, so evacuation is
    impossible and the guest is reported as blocking."""
    cluster = _cluster(
        nodes=[
            _node("node1", {"local-n1": 100 * _GB}),
            _node("node2", {"local-n2": 100 * _GB}),
        ],
        vms=[_vm("vm-pinned", "node1", {"local-n1": 10 * _GB})],
        evacuate="node1",
    )
    solution = solve(cluster, time_limit_s=5.0)
    assert not solution.feasible
    assert "vm-pinned" in solution.blocking_vms


def test_shared_only_guest_moves_freely():
    """A guest whose disks were all on shared storage arrives with an empty
    disk map and must be placeable anywhere despite tiny local storages."""
    cluster = _cluster(
        nodes=[_node("node1", {"local-n1": 1 * _GB}), _node("node2", {"local-n2": 1 * _GB})],
        vms=[_vm("vm-ceph", "node1", {})],
        evacuate="node1",
    )
    solution = solve(cluster, time_limit_s=5.0)
    assert solution.feasible
    assert solution.placements["vm-ceph"] == "node2"


def test_disk_assigned_mode_warns_about_degradation(caplog):
    """method=disk has no assigned/used distinction; the silent fallback to
    allocated bytes must be surfaced as a warning."""
    import logging

    cluster = _cluster(
        nodes=[_node("node1", {"s1": 100 * _GB}), _node("node2", {"s1": 100 * _GB})],
        vms=[_vm("vm-1", "node1", {"s1": 10 * _GB})],
    )
    cluster = cluster.model_copy(update={"balancing": Balancing(method="disk", mode="assigned")})
    with caplog.at_level(logging.WARNING, logger="ProxLB"):
        solve(cluster, time_limit_s=5.0)
    assert any("ignores mode=assigned" in r.message for r in caplog.records)

    caplog.clear()
    cluster = cluster.model_copy(update={"balancing": Balancing(method="disk", mode="used")})
    with caplog.at_level(logging.WARNING, logger="ProxLB"):
        solve(cluster, time_limit_s=5.0)
    assert not any("ignores mode=assigned" in r.message for r in caplog.records)
