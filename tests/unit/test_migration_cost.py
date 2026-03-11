"""Tests for the migration cost model in the CP-SAT solver.

The cost model uses 256 MiB base units so that sub-GiB VMs are genuinely
cheaper to migrate than larger ones. Local disk incurs a 4× penalty.
"""

from proxlb_solver.models import Balancing, Cluster, Constraints, Expect, Node, VM
from proxlb_solver.solver import solve

_GiB = 1024 ** 3
_MiB = 1024 * 1024


def _cluster(nodes, vms, balanciness=5, w_stickiness=1, constraints=None):
    """Helper: build a minimal Cluster for cost-model testing."""
    return Cluster(
        name="test",
        description="",
        balancing=Balancing(
            method="memory",
            balanciness=balanciness,
            w_stickiness=w_stickiness,
        ),
        nodes=nodes,
        vms=vms,
        constraints=constraints or Constraints(),
        expect=Expect(),
    )


# ---------------------------------------------------------------------------
# Prefer smaller migration when balance gain is equal
# ---------------------------------------------------------------------------

class TestPreferSmallerMigration:
    """vm-large (8 GiB) and vm-small (512 MiB) are both on node-A; node-B empty.
    Both migrations achieve the same RAM balance, but vm-small is cheaper."""

    def setup_method(self):
        nodes = [
            Node(name="node-A", cpu_total=4, memory_total=32 * _GiB),
            Node(name="node-B", cpu_total=4, memory_total=32 * _GiB),
        ]
        vms = [
            VM(name="vm-large", node="node-A", cpu=2, memory=8 * _GiB),
            VM(name="vm-small", node="node-A", cpu=1, memory=512 * _MiB),
        ]
        self.solution = solve(_cluster(nodes, vms))

    def test_feasible(self):
        assert self.solution.feasible

    def test_exactly_one_migration(self):
        assert len(self.solution.migrations) == 1

    def test_small_vm_migrated_not_large(self):
        migrated = {m.vm for m in self.solution.migrations}
        assert "vm-small" in migrated, (
            "solver should migrate the cheaper 512 MiB VM, not the 8 GiB VM"
        )
        assert "vm-large" not in migrated

    def test_cost_gib_reflects_small_vm(self):
        # vm-small is < 1 GiB so display cost rounds to 1 GiB (floor at 1)
        assert self.solution.stats.migration_cost_gib == 1


# ---------------------------------------------------------------------------
# Local disk avoidance: RAM-only VM should be preferred over disk-heavy VM
# ---------------------------------------------------------------------------

class TestLocalDiskAvoidance:
    """vm-ramonly (4 GiB, no disk) and vm-diskvm (4 GiB, 100 GiB local disk)
    both on node-A; node-B empty.  Same balance improvement, but vm-diskvm
    costs 4× more due to the local-disk penalty.
    Both nodes have local-lvm storage so the constraint allows vm-diskvm to
    go to node-B — the cost model alone must determine the decision."""

    def setup_method(self):
        nodes = [
            Node(name="node-A", cpu_total=4, memory_total=64 * _GiB,
                 storage_free={"local-lvm": 200 * _GiB}),
            Node(name="node-B", cpu_total=4, memory_total=64 * _GiB,
                 storage_free={"local-lvm": 200 * _GiB}),
        ]
        vms = [
            VM(name="vm-ramonly", node="node-A", cpu=2, memory=4 * _GiB),
            VM(name="vm-diskvm",  node="node-A", cpu=2, memory=4 * _GiB,
               disks={"local-lvm": 100 * _GiB}),
        ]
        self.solution = solve(_cluster(nodes, vms))

    def test_feasible(self):
        assert self.solution.feasible

    def test_exactly_one_migration(self):
        assert len(self.solution.migrations) == 1

    def test_ram_only_vm_migrated(self):
        migrated = {m.vm for m in self.solution.migrations}
        assert "vm-ramonly" in migrated, (
            "solver should avoid migrating the VM with 100 GiB local disk"
        )
        assert "vm-diskvm" not in migrated

    def test_cost_gib_reflects_ram_only(self):
        # vm-ramonly: 4 GiB RAM, no disk → display cost = 4
        assert self.solution.stats.migration_cost_gib == 4


# ---------------------------------------------------------------------------
# Cost GiB field accuracy
# ---------------------------------------------------------------------------

class TestCostGibField:
    """Verify that migration_cost_gib reports actual GiB of migrated data,
    not the optimizer's internal 256-MiB units."""

    def test_cost_gib_for_8gib_vm(self):
        # Two 8 GiB VMs both on node-A; solver migrates one to node-B.
        nodes = [
            Node(name="node-A", cpu_total=4, memory_total=32 * _GiB),
            Node(name="node-B", cpu_total=4, memory_total=32 * _GiB),
        ]
        vms = [
            VM(name="vm-1", node="node-A", cpu=2, memory=8 * _GiB),
            VM(name="vm-2", node="node-A", cpu=2, memory=8 * _GiB),
        ]
        sol = solve(_cluster(nodes, vms))
        assert sol.feasible
        assert len(sol.migrations) == 1
        assert sol.stats.migration_cost_gib == 8

    def test_cost_gib_includes_local_disk_factor(self):
        # vm-disk (4 GiB RAM, 10 GiB local disk) is pinned-out of node-A by
        # pinning vm-other there, forcing vm-disk to migrate to node-B.
        # display cost = max(1, 4) + 4 * 10 = 44
        nodes = [
            Node(name="node-A", cpu_total=4, memory_total=64 * _GiB,
                 storage_free={"local-lvm": 200 * _GiB}),
            Node(name="node-B", cpu_total=4, memory_total=64 * _GiB,
                 storage_free={"local-lvm": 200 * _GiB}),
        ]
        vms = [
            VM(name="vm-disk",  node="node-A", cpu=2, memory=4 * _GiB,
               disks={"local-lvm": 10 * _GiB}),
            VM(name="vm-other", node="node-A", cpu=2, memory=4 * _GiB),
        ]
        # Pin vm-other to node-A so vm-disk is the only VM that can move.
        cons = Constraints(pin=[{"vm": "vm-other", "nodes": ["node-A"]}])
        sol = solve(_cluster(nodes, vms, constraints=cons))
        assert sol.feasible
        migrated = {m.vm for m in sol.migrations}
        assert "vm-disk" in migrated
        assert sol.stats.migration_cost_gib == 44   # 4 + 4*10

    def test_no_migrations_zero_cost(self):
        # Already balanced — no migrations expected
        nodes = [
            Node(name="node-A", cpu_total=4, memory_total=16 * _GiB),
            Node(name="node-B", cpu_total=4, memory_total=16 * _GiB),
        ]
        vms = [
            VM(name="vm-1", node="node-A", cpu=1, memory=4 * _GiB),
            VM(name="vm-2", node="node-B", cpu=1, memory=4 * _GiB),
        ]
        sol = solve(_cluster(nodes, vms, balanciness=3))
        # Balanced already — solver may choose 0 migrations
        if not sol.migrations:
            assert sol.stats.migration_cost_gib == 0


# ---------------------------------------------------------------------------
# Three-VM scenario: mirror of the real-world awi-0x cluster observation
# ---------------------------------------------------------------------------

class TestThreeVmCostPreference:
    """Mirrors the real cluster: two 1 GiB VMs + one 512 MiB VM all on
    node-A, two empty nodes.  With 2 migrations needed, the solver should
    prefer moving the 512 MiB VM + one 1 GiB VM (cost 6) over moving
    the two 1 GiB VMs (cost 8)."""

    def setup_method(self):
        nodes = [
            Node(name="node-A", cpu_total=4, memory_total=16 * _GiB),
            Node(name="node-B", cpu_total=4, memory_total=16 * _GiB),
            Node(name="node-C", cpu_total=4, memory_total=16 * _GiB),
        ]
        vms = [
            VM(name="vm-heavy-1", node="node-A", cpu=2, memory=1 * _GiB),
            VM(name="vm-heavy-2", node="node-A", cpu=2, memory=1 * _GiB),
            VM(name="vm-idle",    node="node-A", cpu=1, memory=512 * _MiB),
        ]
        self.solution = solve(_cluster(nodes, vms))

    def test_feasible(self):
        assert self.solution.feasible

    def test_vm_idle_is_migrated(self):
        migrated = {m.vm for m in self.solution.migrations}
        assert "vm-idle" in migrated, (
            "512 MiB VM should be included in migrations (cheaper than moving "
            "both 1 GiB VMs)"
        )

    def test_not_both_heavy_vms_migrated(self):
        migrated = {m.vm for m in self.solution.migrations}
        both_heavy = {"vm-heavy-1", "vm-heavy-2"}.issubset(migrated)
        assert not both_heavy, (
            "moving both 1 GiB VMs (cost 8) should not be preferred over "
            "moving one 1 GiB + the 512 MiB VM (cost 6)"
        )
