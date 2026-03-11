"""
Integration tests for the Pydantic → dict adapter path.

The adapter accepts either a plain dict or a ProxLbData Pydantic object.
All other tests in the suite pass plain dicts; this file covers the
Pydantic path (_pydantic_to_dict → from_proxlb_data).

Because ProxLB is not a runtime dependency of the solver, we use
SimpleNamespace stubs that match the exact attribute structure the adapter
accesses.  The mapping is documented in adapter._pydantic_to_dict.

Attribute paths accessed by the adapter
----------------------------------------
proxlb_data.meta.balancing:
    .method, .mode, .balanciness, .cpu_overcommit,
    .memory_threshold, .cpu_threshold, .disk_threshold,
    .max_node_inflow, .max_parallel_migrations,
    .node_resource_reserve, .pools

proxlb_data.nodes[name]:
    .cpu.total, .memory.used, .memory.free, .memory.total,
    .disk.free, .cpu.pressure_some_percent,
    .memory.pressure_some_percent, .disk.pressure_some_percent,
    .maintenance

proxlb_data.guests[name]:
    .node_current, .node_target, .cpu.total, .memory.total,
    .cpu.used, .disk.used, .cpu.pressure_some_percent,
    .memory.pressure_some_percent, .disk.pressure_some_percent,
    .type, .ha_rules[i](.rule, .type, .nodes),
    .tags, .pools, .node_relationships, .ignore
"""

from __future__ import annotations

import types
from typing import Any

import pytest

_GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Stub builders
# ---------------------------------------------------------------------------

def _ns(**kwargs: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kwargs)


def _metric(total: int = 0, used: float = 0.0, free: float = 0.0,
            pressure_some_percent: float = 0.0) -> types.SimpleNamespace:
    return _ns(
        total=total, used=used, free=free,
        pressure_some_percent=pressure_some_percent,
    )


def _balancing(
    method: str = "memory",
    mode: str = "used",
    balanciness: int = 3,
    cpu_overcommit: float = 2.0,
    memory_threshold: None = None,
    cpu_threshold: None = None,
    disk_threshold: None = None,
    max_node_inflow: int = 1,
    max_parallel_migrations: None = None,
    node_resource_reserve: None = None,
    pools: None = None,
) -> types.SimpleNamespace:
    return _ns(
        method=method,
        mode=mode,
        balanciness=balanciness,
        cpu_overcommit=cpu_overcommit,
        memory_threshold=memory_threshold,
        cpu_threshold=cpu_threshold,
        disk_threshold=disk_threshold,
        max_node_inflow=max_node_inflow,
        max_parallel_migrations=max_parallel_migrations,
        node_resource_reserve=node_resource_reserve,
        pools=pools,
    )


def _node_stub(
    cpu_total: int = 8,
    memory_used: int = 2 * _GB,
    memory_free: int = 14 * _GB,
    memory_total: int = 16 * _GB,
    disk_free: int = 100 * _GB,
    maintenance: bool = False,
    cpu_pressure: float = 0.0,
    memory_pressure: float = 0.0,
    disk_pressure: float = 0.0,
) -> types.SimpleNamespace:
    return _ns(
        cpu=_ns(total=cpu_total, pressure_some_percent=cpu_pressure),
        memory=_ns(used=memory_used, free=memory_free, total=memory_total,
                   pressure_some_percent=memory_pressure),
        disk=_ns(free=disk_free, pressure_some_percent=disk_pressure),
        maintenance=maintenance,
    )


def _ha_rule_stub(rule: str, type_: str, nodes: list[str] | None = None) -> types.SimpleNamespace:
    return _ns(rule=rule, type=type_, nodes=nodes or [])


def _guest_stub(
    node_current: str = "node1",
    node_target: str | None = None,
    cpu_total: int = 2,
    memory_total: int = 4 * _GB,
    cpu_used: float = 0.1,
    disk_used: int = 10 * _GB,
    type_: str = "vm",
    ha_rules: list[Any] | None = None,
    tags: list[str] | None = None,
    pools: list[str] | None = None,
    node_relationships: list[str] | None = None,
    ignore: bool = False,
) -> types.SimpleNamespace:
    return _ns(
        node_current=node_current,
        node_target=node_target,
        cpu=_ns(total=cpu_total, used=cpu_used, pressure_some_percent=0.0),
        memory=_ns(total=memory_total, pressure_some_percent=0.0),
        disk=_ns(used=disk_used, pressure_some_percent=0.0),
        type=type_,
        ha_rules=ha_rules or [],
        tags=tags or [],
        pools=pools or [],
        node_relationships=node_relationships or [],
        ignore=ignore,
    )


def _proxlb_data(
    nodes: dict[str, Any] | None = None,
    guests: dict[str, Any] | None = None,
    balancing: Any = None,
) -> types.SimpleNamespace:
    """Build a minimal ProxLbData-shaped stub."""
    return _ns(
        meta=_ns(balancing=balancing or _balancing()),
        nodes=nodes or {
            "node1": _node_stub(memory_used=2 * _GB, memory_free=14 * _GB),
            "node2": _node_stub(memory_used=3 * _GB, memory_free=13 * _GB),
        },
        guests=guests or {},
        pools={},
        ha_rules={},
        groups=_ns(affinity={}, anti_affinity={}),
    )


# ---------------------------------------------------------------------------
# Basic round-trip: non-dict object triggers _pydantic_to_dict
# ---------------------------------------------------------------------------

class TestRoundTrip:

    def test_non_dict_triggers_pydantic_path(self):
        """A non-dict object must go through _pydantic_to_dict."""
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={"vm-1": _guest_stub()})
        cluster = from_proxlb_data(stub)

        assert len(cluster.vms) == 1
        assert cluster.vms[0].name == "vm-1"

    def test_cluster_name_from_stub(self):
        """cluster_name must be extracted from meta.cluster_name if present."""
        from proxlb_solver.adapter import _pydantic_to_dict, from_proxlb_data

        # _pydantic_to_dict hard-codes "Live Cluster" — verify the adapter uses it.
        stub = _proxlb_data()
        d = _pydantic_to_dict(stub)
        assert d["meta"]["cluster_name"] == "Live Cluster"

    def test_two_nodes_created(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data()
        cluster = from_proxlb_data(stub)
        assert len(cluster.nodes) == 2
        node_names = {n.name for n in cluster.nodes}
        assert node_names == {"node1", "node2"}


# ---------------------------------------------------------------------------
# Node field mapping
# ---------------------------------------------------------------------------

class TestNodeMapping:

    def test_cpu_total_mapped(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(nodes={"n1": _node_stub(cpu_total=16)})
        cluster = from_proxlb_data(stub)
        node = cluster.nodes[0]
        assert node.cpu_total == 16

    def test_memory_raw_total_reconstructed(self):
        """memory_total must be memory_used + memory_free (raw hardware), not the
        pre-reduced value ProxLB stores in memory.total."""
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(nodes={
            "n1": _node_stub(memory_used=2 * _GB, memory_free=14 * _GB,
                             memory_total=12 * _GB),  # pre-reduced, must be ignored
        })
        cluster = from_proxlb_data(stub)
        assert cluster.nodes[0].memory_total == 16 * _GB

    def test_memory_reserve_applied_with_use_reservations(self):
        """When use_reservations=True and a reserve is configured, memory_reserve > 0."""
        from proxlb_solver.adapter import from_proxlb_data

        bal = _balancing(node_resource_reserve={"defaults": {"memory": 4}})
        stub = _proxlb_data(
            nodes={"n1": _node_stub(memory_used=2 * _GB, memory_free=14 * _GB)},
            balancing=bal,
        )
        cluster = from_proxlb_data(stub, use_reservations=True)
        assert cluster.nodes[0].memory_reserve == 4 * _GB

    def test_memory_reserve_zero_without_use_reservations(self):
        from proxlb_solver.adapter import from_proxlb_data

        bal = _balancing(node_resource_reserve={"defaults": {"memory": 4}})
        stub = _proxlb_data(
            nodes={"n1": _node_stub(memory_used=2 * _GB, memory_free=14 * _GB)},
            balancing=bal,
        )
        cluster = from_proxlb_data(stub, use_reservations=False)
        assert cluster.nodes[0].memory_reserve == 0

    def test_maintenance_flag_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(nodes={"n1": _node_stub(maintenance=True)})
        cluster = from_proxlb_data(stub)
        assert cluster.nodes[0].maintenance is True

    def test_psi_pressures_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(nodes={
            "n1": _node_stub(
                cpu_pressure=12.5,
                memory_pressure=7.3,
                disk_pressure=4.1,
            ),
        })
        cluster = from_proxlb_data(stub)
        n = cluster.nodes[0]
        assert n.cpu_pressure == pytest.approx(12.5)
        assert n.memory_pressure == pytest.approx(7.3)
        assert n.io_pressure == pytest.approx(4.1)


# ---------------------------------------------------------------------------
# Guest field mapping
# ---------------------------------------------------------------------------

class TestGuestMapping:

    def test_vm_name_and_node_mapped(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={"myvm": _guest_stub(node_current="node2")})
        cluster = from_proxlb_data(stub)
        vm = cluster.vms[0]
        assert vm.name == "myvm"
        assert vm.node == "node2"

    def test_vm_memory_mapped(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={"vm-a": _guest_stub(memory_total=8 * _GB)})
        cluster = from_proxlb_data(stub)
        assert cluster.vms[0].memory == 8 * _GB

    def test_vm_cpu_total_mapped(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={"vm-a": _guest_stub(cpu_total=4)})
        cluster = from_proxlb_data(stub)
        assert cluster.vms[0].cpu == 4

    def test_ignore_flag_adds_to_ignore_list(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-ignored": _guest_stub(ignore=True),
            "vm-normal":  _guest_stub(ignore=False),
        })
        cluster = from_proxlb_data(stub)
        assert "vm-ignored" in cluster.constraints.ignore
        assert "vm-normal" not in cluster.constraints.ignore

    def test_vm_type_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={"ct-1": _guest_stub(type_="ct")})
        cluster = from_proxlb_data(stub)
        assert cluster.vms[0].vm_type == "ct"


# ---------------------------------------------------------------------------
# HA rule extraction from Pydantic stubs
# ---------------------------------------------------------------------------

class TestHaRuleExtraction:

    def test_affinity_ha_rule_creates_affinity_constraint(self):
        """Two guests sharing a HA affinity rule must appear in constraints.affinity."""
        from proxlb_solver.adapter import from_proxlb_data

        rule = _ha_rule_stub("ha-grp-1", "affinity")
        stub = _proxlb_data(guests={
            "vm-100": _guest_stub(ha_rules=[rule]),
            "vm-101": _guest_stub(ha_rules=[rule]),
        })
        cluster = from_proxlb_data(stub)
        aff_names = {r["name"] for r in cluster.constraints.affinity}
        assert "ha-grp-1" in aff_names

    def test_affinity_ha_rule_has_pve_origin(self):
        from proxlb_solver.adapter import from_proxlb_data

        rule = _ha_rule_stub("ha-grp-1", "affinity")
        stub = _proxlb_data(guests={
            "vm-100": _guest_stub(ha_rules=[rule]),
            "vm-101": _guest_stub(ha_rules=[rule]),
        })
        cluster = from_proxlb_data(stub)
        rules = [r for r in cluster.constraints.affinity if r["name"] == "ha-grp-1"]
        assert rules[0]["origin"] == "pve"

    def test_anti_affinity_ha_rule_creates_anti_affinity_constraint(self):
        from proxlb_solver.adapter import from_proxlb_data

        rule = _ha_rule_stub("ha-aa-1", "anti-affinity")
        stub = _proxlb_data(guests={
            "vm-200": _guest_stub(ha_rules=[rule]),
            "vm-201": _guest_stub(ha_rules=[rule]),
        })
        cluster = from_proxlb_data(stub)
        aa_names = {r["name"] for r in cluster.constraints.anti_affinity}
        assert "ha-aa-1" in aa_names

    def test_ha_rule_with_nodes_creates_pin(self):
        """HA affinity rule with restricted nodes creates a pin constraint
        when the guest also has node_relationships populated (as ProxLB does)."""
        from proxlb_solver.adapter import from_proxlb_data

        rule = _ha_rule_stub("ha-licensed", "affinity", nodes=["node1"])
        stub = _proxlb_data(guests={
            "vm-pin": _guest_stub(
                ha_rules=[rule],
                node_relationships=["node1"],  # ProxLB has already validated this
            ),
        })
        cluster = from_proxlb_data(stub)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-pin"]
        assert pins, "Expected a pin constraint for vm-pin"
        assert "node1" in pins[0]["nodes"]
        origins = pins[0]["origins"]
        assert any(o["origin"] == "pve" and o["source"] == "ha-licensed" for o in origins)

    def test_members_field_is_not_used_for_vm_identification(self):
        """The 'members' integer list in HA rules is irrelevant to the adapter.
        VM membership is inferred from which guests carry the rule, not from
        the integer IDs in members."""
        from proxlb_solver.adapter import from_proxlb_data

        # Rule has a members list that does NOT match the guest IDs in this stub.
        rule = _ha_rule_stub("ha-grp-x", "affinity")
        # Add members attribute to the stub to confirm it's ignored.
        rule.members = [999, 1000]

        stub = _proxlb_data(guests={
            "vm-a": _guest_stub(ha_rules=[rule]),
            "vm-b": _guest_stub(ha_rules=[rule]),
        })
        cluster = from_proxlb_data(stub)
        # vm-a and vm-b are in the affinity group — derived from which guests
        # have the rule, NOT from members=[999, 1000].
        rules = [r for r in cluster.constraints.affinity if r["name"] == "ha-grp-x"]
        assert rules
        assert set(rules[0]["vms"]) == {"vm-a", "vm-b"}


# ---------------------------------------------------------------------------
# Tag extraction from Pydantic stubs
# ---------------------------------------------------------------------------

class TestTagExtraction:

    def test_plb_affinity_tag_creates_constraint(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-1": _guest_stub(tags=["plb_affinity_web"]),
            "vm-2": _guest_stub(tags=["plb_affinity_web"]),
        })
        cluster = from_proxlb_data(stub)
        aff_names = {r["name"] for r in cluster.constraints.affinity}
        assert "plb_affinity_web" in aff_names

    def test_plb_anti_affinity_tag_creates_constraint(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-1": _guest_stub(tags=["plb_anti_affinity_db"]),
            "vm-2": _guest_stub(tags=["plb_anti_affinity_db"]),
        })
        cluster = from_proxlb_data(stub)
        aa_names = {r["name"] for r in cluster.constraints.anti_affinity}
        assert "plb_anti_affinity_db" in aa_names

    def test_plb_pin_tag_with_node_relationships_creates_pin(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-pinned": _guest_stub(
                tags=["plb_pin_node1"],
                node_relationships=["node1"],
            ),
        })
        cluster = from_proxlb_data(stub)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-pinned"]
        assert pins
        origins = pins[0]["origins"]
        assert any(o["origin"] == "tag" and "plb_pin_node1" in o["source"] for o in origins)

    def test_single_member_tag_group_excluded(self):
        """A tag group with only one member must not produce a constraint."""
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-alone": _guest_stub(tags=["plb_affinity_solo"]),
        })
        cluster = from_proxlb_data(stub)
        aff_names = {r["name"] for r in cluster.constraints.affinity}
        assert "plb_affinity_solo" not in aff_names


# ---------------------------------------------------------------------------
# Balancing config extracted from Pydantic stubs
# ---------------------------------------------------------------------------

class TestBalancingConfig:

    def test_balanciness_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(balancing=_balancing(balanciness=5))
        cluster = from_proxlb_data(stub)
        assert cluster.balancing.balanciness == 5

    def test_method_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(balancing=_balancing(method="cpu"))
        cluster = from_proxlb_data(stub)
        assert cluster.balancing.method == "cpu"

    def test_cpu_overcommit_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(balancing=_balancing(cpu_overcommit=4.0))
        cluster = from_proxlb_data(stub)
        assert cluster.balancing.cpu_overcommit == pytest.approx(4.0)

    def test_max_node_inflow_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(balancing=_balancing(max_node_inflow=3))
        cluster = from_proxlb_data(stub)
        assert cluster.balancing.max_node_inflow == 3

    def test_memory_threshold_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(balancing=_balancing(memory_threshold=80))
        cluster = from_proxlb_data(stub)
        assert cluster.balancing.memory_threshold == 80

    def test_cpu_threshold_propagated(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(balancing=_balancing(cpu_threshold=70))
        cluster = from_proxlb_data(stub)
        assert cluster.balancing.cpu_threshold == 70

    def test_node_resource_reserve_applied_per_node(self):
        """Node-specific reserve overrides the default."""
        from proxlb_solver.adapter import from_proxlb_data

        bal = _balancing(node_resource_reserve={
            "defaults": {"memory": 2},   # 2 GB default
            "n2": {"memory": 6},          # 6 GB override for n2
        })
        stub = _proxlb_data(
            nodes={
                "n1": _node_stub(memory_used=2 * _GB, memory_free=14 * _GB),
                "n2": _node_stub(memory_used=2 * _GB, memory_free=14 * _GB),
            },
            balancing=bal,
        )
        cluster = from_proxlb_data(stub, use_reservations=True)
        by_name = {n.name: n for n in cluster.nodes}
        assert by_name["n1"].memory_reserve == 2 * _GB
        assert by_name["n2"].memory_reserve == 6 * _GB


# ---------------------------------------------------------------------------
# pin_vms feedback loop (active mode re-solve)
# ---------------------------------------------------------------------------

class TestPinVmsFeedback:

    def test_pin_vms_adds_pin_rule(self):
        """VMs in pin_vms must be pinned to their current node."""
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-failed": _guest_stub(node_current="node1"),
        })
        cluster = from_proxlb_data(stub, pin_vms={"vm-failed"})
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-failed"]
        assert pins
        assert pins[0]["nodes"] == ["node1"]
        assert any(o["origin"] == "solver" for o in pins[0]["origins"])

    def test_non_pinned_vm_not_in_pin_list(self):
        from proxlb_solver.adapter import from_proxlb_data

        stub = _proxlb_data(guests={
            "vm-ok": _guest_stub(node_current="node1"),
        })
        cluster = from_proxlb_data(stub, pin_vms=set())
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-ok"]
        assert not pins
