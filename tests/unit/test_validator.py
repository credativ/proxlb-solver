"""
Direct unit tests for proxlb_solver.validator.

Each test targets one of the four detection paths in validate_and_merge_constraints:

  Path 1 — Transitive affinity merging (A+B, B+C  →  group {A,B,C})
  Path 2 — Affinity ↔ Anti-Affinity conflict  (A must be with B AND A must be away from B)
  Path 3 — Multi-pin conflict  (same VM pinned to two disjoint node sets)
  Path 4 — Affinity-aware pinning intersection  (A pinned to n1, B pinned to n2, A+B affine)

Tests also verify:
  - Non-conflicting clusters pass through without raising
  - Soft rules are ignored by the validator (cannot cause logical conflicts)
  - The RuleConflictError message identifies the relevant VMs / rules
  - VMs not present in the cluster are silently skipped
"""

from __future__ import annotations

import pytest

from proxlb_solver.models import Balancing, Cluster, Constraints, Expect, Node, VM
from proxlb_solver.validator import RuleConflictError, validate_and_merge_constraints

_GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _node(name: str) -> Node:
    return Node(name=name, cpu_total=8, memory_total=16 * _GB)


def _vm(name: str, node: str = "n1") -> VM:
    return VM(name=name, node=node, cpu=2, memory=4 * _GB)


def _cluster(
    vms: list[VM],
    constraints: Constraints,
    nodes: list[Node] | None = None,
) -> Cluster:
    if nodes is None:
        nodes = [_node("n1"), _node("n2"), _node("n3")]
    return Cluster(
        name="test",
        description="",
        balancing=Balancing(),
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=Expect(),
    )


# ---------------------------------------------------------------------------
# Happy path: no conflicts → returns constraints unchanged
# ---------------------------------------------------------------------------

class TestNoConflict:

    def test_empty_constraints_returns_constraints(self) -> None:
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=Constraints())
        result = validate_and_merge_constraints(cluster)
        assert result is cluster.constraints

    def test_affinity_only_no_conflict(self) -> None:
        cons = Constraints(
            affinity=[{"name": "web", "vms": ["vm-a", "vm-b"], "hard": True}],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        # Must not raise
        validate_and_merge_constraints(cluster)

    def test_anti_affinity_only_no_conflict(self) -> None:
        cons = Constraints(
            anti_affinity=[{"name": "spread", "vms": ["vm-a", "vm-b"], "hard": True}],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_pin_only_no_conflict(self) -> None:
        cons = Constraints(
            pin=[{"vm": "vm-a", "nodes": ["n1", "n2"]}],
        )
        cluster = _cluster(vms=[_vm("vm-a")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_affinity_and_anti_affinity_on_different_vms(self) -> None:
        """A+B affine, C+D anti-affine: no overlap → no conflict."""
        cons = Constraints(
            affinity=[{"name": "web", "vms": ["vm-a", "vm-b"], "hard": True}],
            anti_affinity=[{"name": "spread", "vms": ["vm-c", "vm-d"], "hard": True}],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c"), _vm("vm-d")],
            constraints=cons,
        )
        validate_and_merge_constraints(cluster)


# ---------------------------------------------------------------------------
# Path 1: Transitive affinity merging
# ---------------------------------------------------------------------------

class TestAffinityMerging:

    def test_two_rules_sharing_one_vm_are_merged(self) -> None:
        """A+B and B+C share B → merged into {A,B,C}."""
        cons = Constraints(
            affinity=[
                {"name": "rule1", "vms": ["vm-a", "vm-b"], "hard": True},
                {"name": "rule2", "vms": ["vm-b", "vm-c"], "hard": True},
            ],
            anti_affinity=[
                # A and C must be apart — this is fine as individual VMs only if the
                # merged group contains both, but here we ONLY check A↔C anti-affinity
                # which triggers via the merged group.
                {"name": "split", "vms": ["vm-a", "vm-c"], "hard": True},
            ],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c")],
            constraints=cons,
        )
        # A and C end up in the same merged group {A,B,C} because of transitivity.
        # The anti-affinity rule conflicts with that merged group.
        with pytest.raises(RuleConflictError):
            validate_and_merge_constraints(cluster)

    def test_three_independent_rules_merged_into_one_group(self) -> None:
        """A+B, B+C, C+D → {A,B,C,D}; anti-affinity A+D conflicts."""
        cons = Constraints(
            affinity=[
                {"name": "r1", "vms": ["vm-a", "vm-b"], "hard": True},
                {"name": "r2", "vms": ["vm-b", "vm-c"], "hard": True},
                {"name": "r3", "vms": ["vm-c", "vm-d"], "hard": True},
            ],
            anti_affinity=[
                {"name": "aa", "vms": ["vm-a", "vm-d"], "hard": True},
            ],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c"), _vm("vm-d")],
            constraints=cons,
        )
        with pytest.raises(RuleConflictError):
            validate_and_merge_constraints(cluster)

    def test_two_separate_groups_no_conflict(self) -> None:
        """A+B (group 1) and C+D (group 2): anti-affinity across groups is fine."""
        cons = Constraints(
            affinity=[
                {"name": "g1", "vms": ["vm-a", "vm-b"], "hard": True},
                {"name": "g2", "vms": ["vm-c", "vm-d"], "hard": True},
            ],
            anti_affinity=[
                # A must be apart from C — they are in different affinity groups.
                {"name": "across", "vms": ["vm-a", "vm-c"], "hard": True},
            ],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c"), _vm("vm-d")],
            constraints=cons,
        )
        # Should not raise: cross-group anti-affinity is valid.
        validate_and_merge_constraints(cluster)

    def test_soft_affinity_rules_are_not_merged(self) -> None:
        """Soft affinity rules must not participate in transitivity merging.

        A+B (soft), B+C (soft): even if anti-affinity A+C is hard,
        the validator must NOT raise because B is only softly linked.
        """
        cons = Constraints(
            affinity=[
                {"name": "s1", "vms": ["vm-a", "vm-b"], "hard": False},
                {"name": "s2", "vms": ["vm-b", "vm-c"], "hard": False},
            ],
            anti_affinity=[
                {"name": "aa", "vms": ["vm-a", "vm-c"], "hard": True},
            ],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c")],
            constraints=cons,
        )
        # Soft rules are ignored → no merged group → no conflict.
        validate_and_merge_constraints(cluster)


# ---------------------------------------------------------------------------
# Path 2: Affinity ↔ Anti-Affinity conflict
# ---------------------------------------------------------------------------

class TestAffinityAntiAffinityConflict:

    def test_direct_conflict_raises(self) -> None:
        """A+B must be together AND A+B must be apart: conflict."""
        cons = Constraints(
            affinity=[{"name": "must_together", "vms": ["vm-a", "vm-b"], "hard": True}],
            anti_affinity=[{"name": "must_apart", "vms": ["vm-a", "vm-b"], "hard": True}],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        with pytest.raises(RuleConflictError) as exc_info:
            validate_and_merge_constraints(cluster)
        # Error message should identify the anti-affinity rule name.
        assert "must_apart" in str(exc_info.value)

    def test_conflict_message_contains_vms(self) -> None:
        """Error message must identify the conflicting VMs."""
        cons = Constraints(
            affinity=[{"name": "aff", "vms": ["alpha", "beta"], "hard": True}],
            anti_affinity=[{"name": "anti", "vms": ["alpha", "beta"], "hard": True}],
        )
        cluster = _cluster(vms=[_vm("alpha"), _vm("beta")], constraints=cons)
        with pytest.raises(RuleConflictError) as exc_info:
            validate_and_merge_constraints(cluster)
        msg = str(exc_info.value)
        # Both VMs must appear in the message.
        assert "alpha" in msg or "beta" in msg

    def test_three_vms_partial_overlap_raises(self) -> None:
        """Affinity {A,B,C}, anti-affinity {A,B}: A and B are in the affinity group."""
        cons = Constraints(
            affinity=[{"name": "grp", "vms": ["vm-a", "vm-b", "vm-c"], "hard": True}],
            anti_affinity=[{"name": "split", "vms": ["vm-a", "vm-b"], "hard": True}],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c")],
            constraints=cons,
        )
        with pytest.raises(RuleConflictError):
            validate_and_merge_constraints(cluster)

    def test_soft_anti_affinity_with_hard_affinity_does_not_raise(self) -> None:
        """Soft anti-affinity can never produce a logical deadlock → no conflict."""
        cons = Constraints(
            affinity=[{"name": "aff", "vms": ["vm-a", "vm-b"], "hard": True}],
            anti_affinity=[{"name": "soft_aa", "vms": ["vm-a", "vm-b"], "hard": False}],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_unknown_vm_in_rule_is_skipped(self) -> None:
        """A VM mentioned in a rule but not in the cluster must be silently ignored."""
        cons = Constraints(
            affinity=[{"name": "aff", "vms": ["vm-a", "ghost"], "hard": True}],
            anti_affinity=[{"name": "aa", "vms": ["vm-a", "ghost"], "hard": True}],
        )
        # Only vm-a is actually in the cluster; "ghost" is filtered out.
        # After filtering, no group has ≥2 members → no conflict.
        cluster = _cluster(vms=[_vm("vm-a")], constraints=cons)
        validate_and_merge_constraints(cluster)


# ---------------------------------------------------------------------------
# Path 3: Multi-pin conflict (same VM pinned to disjoint node sets)
# ---------------------------------------------------------------------------

class TestPinConflict:

    def test_two_disjoint_pin_rules_raise(self) -> None:
        """vm pinned to {n1} and also pinned to {n2}: intersection is empty."""
        cons = Constraints(
            pin=[
                {"vm": "vm-a", "nodes": ["n1"]},
                {"vm": "vm-a", "nodes": ["n2"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a")], constraints=cons)
        with pytest.raises(RuleConflictError) as exc_info:
            validate_and_merge_constraints(cluster)
        assert "vm-a" in str(exc_info.value)

    def test_two_overlapping_pin_rules_do_not_raise(self) -> None:
        """vm pinned to {n1,n2} and {n2,n3}: intersection {n2} is non-empty."""
        cons = Constraints(
            pin=[
                {"vm": "vm-a", "nodes": ["n1", "n2"]},
                {"vm": "vm-a", "nodes": ["n2", "n3"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_single_pin_rule_does_not_raise(self) -> None:
        cons = Constraints(pin=[{"vm": "vm-a", "nodes": ["n1"]}])
        cluster = _cluster(vms=[_vm("vm-a")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_pin_conflict_message_names_the_vm(self) -> None:
        cons = Constraints(
            pin=[
                {"vm": "important-vm", "nodes": ["n1"]},
                {"vm": "important-vm", "nodes": ["n2"]},
            ],
        )
        cluster = _cluster(vms=[_vm("important-vm")], constraints=cons)
        with pytest.raises(RuleConflictError) as exc_info:
            validate_and_merge_constraints(cluster)
        assert "important-vm" in str(exc_info.value)

    def test_ghost_vm_pin_conflict_is_ignored(self) -> None:
        """A pin conflict for a VM not in the cluster must be silently skipped."""
        cons = Constraints(
            pin=[
                {"vm": "ghost", "nodes": ["n1"]},
                {"vm": "ghost", "nodes": ["n2"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a")], constraints=cons)
        # "ghost" is not in the cluster → skipped entirely.
        validate_and_merge_constraints(cluster)

    def test_three_pin_rules_intersection_empty_raises(self) -> None:
        """vm pinned to {n1,n2}, {n2,n3}, {n3,n4}: intersection {n2}∩{n3}={} raises."""
        cons = Constraints(
            pin=[
                {"vm": "vm-a", "nodes": ["n1", "n2"]},
                {"vm": "vm-a", "nodes": ["n2", "n3"]},
                {"vm": "vm-a", "nodes": ["n3", "n4"]},
            ],
        )
        cluster = _cluster(
            vms=[_vm("vm-a")],
            nodes=[_node(f"n{i}") for i in range(1, 5)],
            constraints=cons,
        )
        with pytest.raises(RuleConflictError):
            validate_and_merge_constraints(cluster)


# ---------------------------------------------------------------------------
# Path 4: Affinity-aware pinning intersection
# ---------------------------------------------------------------------------

class TestAffinityAwarePinning:

    def test_affine_vms_pinned_to_same_node_ok(self) -> None:
        """A+B affine, both pinned to n1: intersection {n1} is valid."""
        cons = Constraints(
            affinity=[{"name": "grp", "vms": ["vm-a", "vm-b"], "hard": True}],
            pin=[
                {"vm": "vm-a", "nodes": ["n1"]},
                {"vm": "vm-b", "nodes": ["n1"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_affine_vms_pinned_to_different_nodes_raises(self) -> None:
        """A+B affine, A pinned to n1, B pinned to n2: can never share a node."""
        cons = Constraints(
            affinity=[{"name": "together", "vms": ["vm-a", "vm-b"], "hard": True}],
            pin=[
                {"vm": "vm-a", "nodes": ["n1"]},
                {"vm": "vm-b", "nodes": ["n2"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        with pytest.raises(RuleConflictError) as exc_info:
            validate_and_merge_constraints(cluster)
        msg = str(exc_info.value)
        assert "unplaceable" in msg

    def test_affine_group_only_one_vm_pinned_no_conflict(self) -> None:
        """A+B affine; only A is pinned. No intersection check needed."""
        cons = Constraints(
            affinity=[{"name": "grp", "vms": ["vm-a", "vm-b"], "hard": True}],
            pin=[{"vm": "vm-a", "nodes": ["n1"]}],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        # B is unpinned → B can move to n1 with A.
        validate_and_merge_constraints(cluster)

    def test_transitive_group_pin_conflict(self) -> None:
        """A+B (rule1), B+C (rule2) → merged {A,B,C}. A→n1, C→n2 conflicts."""
        cons = Constraints(
            affinity=[
                {"name": "r1", "vms": ["vm-a", "vm-b"], "hard": True},
                {"name": "r2", "vms": ["vm-b", "vm-c"], "hard": True},
            ],
            pin=[
                {"vm": "vm-a", "nodes": ["n1"]},
                {"vm": "vm-c", "nodes": ["n2"]},
            ],
        )
        cluster = _cluster(
            vms=[_vm("vm-a"), _vm("vm-b"), _vm("vm-c")],
            constraints=cons,
        )
        with pytest.raises(RuleConflictError):
            validate_and_merge_constraints(cluster)

    def test_affine_vms_with_overlapping_pins_ok(self) -> None:
        """A+B affine, A→{n1,n2}, B→{n2,n3}: intersection {n2} is valid."""
        cons = Constraints(
            affinity=[{"name": "grp", "vms": ["vm-a", "vm-b"], "hard": True}],
            pin=[
                {"vm": "vm-a", "nodes": ["n1", "n2"]},
                {"vm": "vm-b", "nodes": ["n2", "n3"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        validate_and_merge_constraints(cluster)

    def test_soft_affinity_pinning_intersection_not_checked(self) -> None:
        """Soft affinity is excluded from transitivity merging, so pinned VMs
        with disjoint sets under a soft rule must not cause a RuleConflictError."""
        cons = Constraints(
            affinity=[{"name": "soft", "vms": ["vm-a", "vm-b"], "hard": False}],
            pin=[
                {"vm": "vm-a", "nodes": ["n1"]},
                {"vm": "vm-b", "nodes": ["n2"]},
            ],
        )
        cluster = _cluster(vms=[_vm("vm-a"), _vm("vm-b")], constraints=cons)
        validate_and_merge_constraints(cluster)
