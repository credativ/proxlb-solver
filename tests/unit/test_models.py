"""
Tests for proxlb_solver.models — Pydantic model validation, immutability,
defaults, type coercion, and model_copy behaviour.
"""
import pytest
from pydantic import ValidationError

from proxlb_solver.models import (
    Node, VM, Constraints, Balancing, Cluster,
    Migration, MigrationStep, MigrationPlan,
    SolverStats, Solution, Expect,
)

_GB = 1024 ** 3


# ── Node ─────────────────────────────────────────────────────────────────────

class TestNode:
    def test_required_fields(self):
        n = Node(name="pve-01", cpu_total=16, memory_total=64 * _GB)
        assert n.name == "pve-01"
        assert n.cpu_total == 16
        assert n.memory_total == 64 * _GB

    def test_defaults(self):
        n = Node(name="n", cpu_total=8, memory_total=16 * _GB)
        assert n.storage_free == {}
        assert n.cpu_reserve == 0
        assert n.memory_reserve == 0
        assert n.storage_reserve == {}
        assert n.cpu_pressure == 0.0
        assert n.memory_pressure == 0.0
        assert n.io_pressure == 0.0
        assert n.maintenance is False

    def test_maintenance_flag(self):
        n = Node(name="n", cpu_total=4, memory_total=8 * _GB, maintenance=True)
        assert n.maintenance is True

    def test_storage_free(self):
        n = Node(name="n", cpu_total=4, memory_total=8 * _GB,
                 storage_free={"local-lvm": 500 * _GB})
        assert n.storage_free["local-lvm"] == 500 * _GB

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            Node(name="n", cpu_total=4)  # memory_total missing

    def test_immutable(self):
        n = Node(name="n", cpu_total=4, memory_total=8 * _GB)
        with pytest.raises(Exception):  # ValidationError or TypeError
            n.name = "other"  # type: ignore[misc]

    def test_model_copy_with_update(self):
        n = Node(name="n", cpu_total=4, memory_total=8 * _GB)
        n2 = n.model_copy(update={"maintenance": True})
        assert n2.maintenance is True
        assert n.maintenance is False  # original unchanged

    def test_type_coercion_cpu_total(self):
        """Pydantic coerces string-int to int."""
        n = Node(name="n", cpu_total="16", memory_total=8 * _GB)  # type: ignore[arg-type]
        assert n.cpu_total == 16
        assert isinstance(n.cpu_total, int)

    def test_independent_default_dicts(self):
        """Each Node instance must get its own storage_free dict."""
        n1 = Node(name="n1", cpu_total=4, memory_total=8 * _GB)
        n2 = Node(name="n2", cpu_total=4, memory_total=8 * _GB)
        assert n1.storage_free is not n2.storage_free


# ── VM ────────────────────────────────────────────────────────────────────────

class TestVM:
    def test_required_fields(self):
        vm = VM(name="my-vm", node="pve-01", cpu=4, memory=8 * _GB)
        assert vm.name == "my-vm"
        assert vm.node == "pve-01"
        assert vm.cpu == 4
        assert vm.memory == 8 * _GB

    def test_defaults(self):
        vm = VM(name="v", node="n", cpu=2, memory=4 * _GB)
        assert vm.cpu_usage == 0.0
        assert vm.cpu_pressure == 0.0
        assert vm.memory_pressure == 0.0
        assert vm.io_pressure == 0.0
        assert vm.disks == {}
        assert vm.priority == 2
        assert vm.vm_type == "vm"

    def test_priority_range(self):
        for p in (1, 2, 3):
            vm = VM(name="v", node="n", cpu=1, memory=_GB, priority=p)
            assert vm.priority == p

    def test_vm_type_ct(self):
        vm = VM(name="ct-01", node="n", cpu=1, memory=_GB, vm_type="ct")
        assert vm.vm_type == "ct"

    def test_immutable(self):
        vm = VM(name="v", node="n", cpu=2, memory=4 * _GB)
        with pytest.raises(Exception):
            vm.node = "other"  # type: ignore[misc]

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            VM(name="v", node="n", cpu=2)  # memory missing

    def test_independent_default_dicts(self):
        vm1 = VM(name="v1", node="n", cpu=1, memory=_GB)
        vm2 = VM(name="v2", node="n", cpu=1, memory=_GB)
        assert vm1.disks is not vm2.disks


# ── Balancing ─────────────────────────────────────────────────────────────────

class TestBalancing:
    def test_defaults(self):
        b = Balancing()
        assert b.method == "memory"
        assert b.mode == "used"
        assert b.balanciness == 3
        assert b.cpu_overcommit == 2.0
        assert b.memory_threshold is None
        assert b.cpu_threshold is None
        assert b.disk_threshold is None
        assert b.w_balance is None
        assert b.w_stickiness is None
        assert b.max_parallel_migrations == 2
        assert b.max_node_inflow == 1

    def test_custom_values(self):
        b = Balancing(method="cpu", balanciness=5, memory_threshold=80.0,
                      w_balance=100, max_parallel_migrations=None)
        assert b.method == "cpu"
        assert b.balanciness == 5
        assert b.memory_threshold == 80.0
        assert b.w_balance == 100
        assert b.max_parallel_migrations is None

    def test_immutable(self):
        b = Balancing()
        with pytest.raises(Exception):
            b.balanciness = 1  # type: ignore[misc]

    def test_model_copy_for_override(self):
        b = Balancing(balanciness=3)
        b2 = b.model_copy(update={"balanciness": 5, "max_parallel_migrations": 10})
        assert b2.balanciness == 5
        assert b2.max_parallel_migrations == 10
        assert b.balanciness == 3  # original unchanged


# ── Constraints ───────────────────────────────────────────────────────────────

class TestConstraints:
    def test_empty_defaults(self):
        c = Constraints()
        assert c.affinity == []
        assert c.anti_affinity == []
        assert c.pin == []
        assert c.ignore == []

    def test_anti_affinity_rule(self):
        c = Constraints(anti_affinity=[
            {"name": "db-sep", "vms": ["db-primary", "db-replica"], "hard": True}
        ])
        assert len(c.anti_affinity) == 1
        assert c.anti_affinity[0]["name"] == "db-sep"

    def test_pin_rule(self):
        c = Constraints(pin=[{"vm": "db-primary", "nodes": ["pve-01"]}])
        assert c.pin[0]["vm"] == "db-primary"

    def test_ignore_list(self):
        c = Constraints(ignore=["backup-vm", "legacy-ct"])
        assert "backup-vm" in c.ignore

    def test_independent_default_lists(self):
        c1 = Constraints()
        c2 = Constraints()
        assert c1.affinity is not c2.affinity


# ── Migration ─────────────────────────────────────────────────────────────────

class TestMigration:
    def test_fields(self):
        m = Migration(vm="my-vm", source="pve-01", target="pve-02")
        assert m.vm == "my-vm"
        assert m.source == "pve-01"
        assert m.target == "pve-02"

    def test_immutable(self):
        m = Migration(vm="v", source="a", target="b")
        with pytest.raises(Exception):
            m.target = "c"  # type: ignore[misc]

    def test_missing_field_raises(self):
        with pytest.raises(ValidationError):
            Migration(vm="v", source="a")  # target missing


# ── MigrationStep ─────────────────────────────────────────────────────────────

class TestMigrationStep:
    def test_fields(self):
        m = Migration(vm="v", source="a", target="b")
        step = MigrationStep(step=1, migrations=[m], parallel=False)
        assert step.step == 1
        assert len(step.migrations) == 1
        assert step.parallel is False

    def test_parallel_step(self):
        migrations = [
            Migration(vm="v1", source="a", target="b"),
            Migration(vm="v2", source="c", target="d"),
        ]
        step = MigrationStep(step=2, migrations=migrations, parallel=True)
        assert step.parallel is True
        assert len(step.migrations) == 2


# ── MigrationPlan ─────────────────────────────────────────────────────────────

class TestMigrationPlan:
    def test_empty_plan(self):
        plan = MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])
        assert plan.steps == []
        assert plan.path_feasible is True
        assert plan.unbreakable_cycle == []
        assert plan.pve_deferred == []

    def test_path_infeasible(self):
        plan = MigrationPlan(
            steps=[], dependency_edges=[], temp_moves=[],
            path_feasible=False,
            unbreakable_cycle=["vm-a", "vm-b"],
        )
        assert plan.path_feasible is False
        assert "vm-a" in plan.unbreakable_cycle

    def test_with_steps(self):
        step = MigrationStep(
            step=1,
            migrations=[Migration(vm="v", source="a", target="b")],
            parallel=False,
        )
        plan = MigrationPlan(
            steps=[step],
            dependency_edges=[("v", "other")],
            temp_moves=[],
        )
        assert len(plan.steps) == 1
        assert plan.dependency_edges[0] == ("v", "other")

    def test_independent_default_lists(self):
        p1 = MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])
        p2 = MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])
        assert p1.unbreakable_cycle is not p2.unbreakable_cycle


# ── SolverStats ───────────────────────────────────────────────────────────────

class TestSolverStats:
    def test_fields(self):
        s = SolverStats(
            status="OPTIMAL", objective=12345, load_gap=0.15,
            migration_count=3, wall_time_ms=42.5,
        )
        assert s.status == "OPTIMAL"
        assert s.objective == 12345
        assert s.load_gap == 0.15
        assert s.migration_count == 3
        assert s.wall_time_ms == 42.5
        assert s.migration_cost_gib == 0

    def test_migration_cost_gib(self):
        s = SolverStats(
            status="FEASIBLE", objective=0, load_gap=0.0,
            migration_count=1, wall_time_ms=5.0, migration_cost_gib=8,
        )
        assert s.migration_cost_gib == 8

    def test_infeasible_status(self):
        s = SolverStats(
            status="INFEASIBLE", objective=0, load_gap=0.0,
            migration_count=0, wall_time_ms=1.0,
        )
        assert s.status == "INFEASIBLE"


# ── Solution ──────────────────────────────────────────────────────────────────

class TestSolution:
    def _stats(self):
        return SolverStats(
            status="OPTIMAL", objective=0, load_gap=0.0,
            migration_count=0, wall_time_ms=1.0,
        )

    def test_feasible_no_migrations(self):
        sol = Solution(
            feasible=True, placements={"v1": "n1"}, migrations=[],
            stats=self._stats(),
        )
        assert sol.feasible is True
        assert sol.placements == {"v1": "n1"}
        assert sol.path_feasible is True
        assert sol.reachability_attempts == 1
        assert sol.blocking_vms == []

    def test_infeasible(self):
        sol = Solution(
            feasible=False, placements={}, migrations=[],
            stats=self._stats(),
            blocking_vms=["vm-huge"],
        )
        assert sol.feasible is False
        assert "vm-huge" in sol.blocking_vms

    def test_model_copy_for_reachability(self):
        """model_copy is used in solve_reachable to update attempt count."""
        sol = Solution(
            feasible=True, placements={}, migrations=[],
            stats=self._stats(),
        )
        sol2 = sol.model_copy(update={"path_feasible": False, "reachability_attempts": 2})
        assert sol2.path_feasible is False
        assert sol2.reachability_attempts == 2
        assert sol.path_feasible is True  # original unchanged
        assert sol.reachability_attempts == 1

    def test_immutable(self):
        sol = Solution(
            feasible=True, placements={}, migrations=[],
            stats=self._stats(),
        )
        with pytest.raises(Exception):
            sol.feasible = False  # type: ignore[misc]


# ── Expect ────────────────────────────────────────────────────────────────────

class TestExpect:
    def test_defaults(self):
        e = Expect()
        assert e.feasible is True
        assert e.constraints_satisfied is True
        assert e.spread_improved is None
        assert e.max_migrations is None
        assert e.placements == {}
        assert e.node_empty is None
        assert e.path_feasible is None

    def test_custom(self):
        e = Expect(
            feasible=False,
            constraints_satisfied=False,
            spread_improved=True,
            max_migrations=0,
            path_feasible=False,
        )
        assert e.feasible is False
        assert e.max_migrations == 0
        assert e.path_feasible is False

    def test_placements(self):
        e = Expect(placements={"vm-a": "node-1", "vm-b": "node-2"})
        assert e.placements["vm-a"] == "node-1"
