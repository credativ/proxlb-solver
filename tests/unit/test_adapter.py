"""Unit tests for the ProxLB → Solver adapter, focusing on reservation handling."""

import pytest

_GB = 1024 ** 3

# Base node entry that mimics what ProxLB stores in proxlb_data["nodes"].
# memory_used + memory_free = 16 GB (raw hardware total).
# memory_total = 12 GB (= 16 GB - 4 GB default reservation, pre-reduced by ProxLB).
_NODE = {
    "cpu_total": 8,
    "memory_total": 12 * _GB,   # pre-reduced
    "memory_used":   2 * _GB,   # raw
    "memory_free":  14 * _GB,   # raw  →  total = 16 GB
    "disk_free": 100 * _GB,
    "maintenance": False,
}

_PROXLB_DATA_WITH_RESERVE = {
    "meta": {
        "cluster_name": "test",
        "balancing": {
            "method": "memory",
            "balanciness": 3,
            "cpu_overcommit": 2.0,
            "node_resource_reserve": {
                "defaults": {"memory": 4},       # 4 GB default
                "node2":    {"memory": 6},        # 6 GB node-specific override
            },
        },
    },
    "nodes": {
        "node1": dict(_NODE),
        "node2": dict(_NODE),
    },
    "guests": {},
    "pools": {},
    "ha_rules": {},
    "groups": {},
}


def test_raw_memory_total_used():
    """Adapter must use memory_used + memory_free as the raw hardware total."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    assert node1.memory_total == 16 * _GB, (
        f"Expected raw 16 GB, got {node1.memory_total / _GB:.1f} GB"
    )


def test_default_reservation_applied():
    """Default node_resource_reserve is applied as memory_reserve (in bytes)."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    assert node1.memory_reserve == 4 * _GB, (
        f"Expected 4 GB reserve, got {node1.memory_reserve / _GB:.1f} GB"
    )


def test_node_specific_reservation_overrides_default():
    """Per-node reservation overrides the default for that node."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=True)
    node2 = next(n for n in cluster.nodes if n.name == "node2")

    assert node2.memory_reserve == 6 * _GB, (
        f"Expected 6 GB reserve for node2, got {node2.memory_reserve / _GB:.1f} GB"
    )


def test_use_reservations_false_sets_reserve_to_zero():
    """use_reservations=False must leave memory_reserve at 0."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=False)
    for node in cluster.nodes:
        assert node.memory_reserve == 0, (
            f"Node {node.name}: expected reserve=0, got {node.memory_reserve}"
        )


def test_effective_capacity_equals_raw_minus_reserve():
    """The solver constraint uses memory_total - memory_reserve; verify that value."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    effective = node1.memory_total - node1.memory_reserve
    assert effective == 12 * _GB, (
        f"Expected effective capacity 12 GB (= 16 - 4), got {effective / _GB:.1f} GB"
    )


def test_fallback_to_memory_total_when_raw_fields_absent():
    """Without memory_used/memory_free, adapter falls back to memory_total with reserve=0."""
    from proxlb_solver.adapter import from_proxlb_data

    data = {
        "meta": {"cluster_name": "test", "balancing": {
            "node_resource_reserve": {"defaults": {"memory": 4}},
        }},
        "nodes": {"node1": {"cpu_total": 4, "memory_total": 12 * _GB, "disk_free": 0, "maintenance": False}},
        "guests": {},
    }
    cluster = from_proxlb_data(data, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    # Falls back to memory_total; reserve cannot be separated, stays 0.
    assert node1.memory_total == 12 * _GB
    assert node1.memory_reserve == 0


def test_reservation_clamped_to_raw_memory():
    """A reservation larger than the node's RAM is clamped, not applied blindly."""
    from proxlb_solver.adapter import from_proxlb_data

    data = {
        "meta": {"cluster_name": "test", "balancing": {
            "node_resource_reserve": {"defaults": {"memory": 100}},  # 100 GB > 16 GB node
        }},
        "nodes": {"node1": {"cpu_total": 4,
                             "memory_total": 12 * _GB,
                             "memory_used":   2 * _GB,
                             "memory_free":  14 * _GB,
                             "disk_free": 0, "maintenance": False}},
        "guests": {},
    }
    cluster = from_proxlb_data(data, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    assert node1.memory_reserve <= node1.memory_total, (
        "Reserve must never exceed memory_total"
    )


# ---------------------------------------------------------------------------
# pin_vms parameter tests
# ---------------------------------------------------------------------------

_DATA_WITH_GUEST = {
    "meta": {"cluster_name": "test", "balancing": {}},
    "nodes": {
        "node1": {
            "cpu_total": 4,
            "memory_total": 8 * _GB,
            "memory_used": 1 * _GB,
            "memory_free": 7 * _GB,
            "disk_free": 50 * _GB,
            "maintenance": False,
        }
    },
    "guests": {
        "vm-pinned": {
            "node_current": "node1",
            "node_target": None,
            "cpu_total": 2,
            "memory_total": 2 * _GB,
            "cpu_used": 0.1,
            "type": "vm",
            "priority": 2,
            "ha_rules": [],
            "tags": [],
            "pools": [],
        }
    },
}


def test_pin_vms_parameter_pins_to_current_node():
    """When pin_vms contains a VM name a pin rule to its current node is added."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_DATA_WITH_GUEST, pin_vms={"vm-pinned"})

    pin = next((r for r in cluster.constraints.pin if r["vm"] == "vm-pinned"), None)
    assert pin is not None, "Expected a pin rule for vm-pinned"
    assert "node1" in pin["nodes"], "Pin rule must include the current node"
    assert any(
        o.get("origin") == "solver" and o.get("source") == "migration_failed"
        for o in pin.get("origins", [])
    ), "Pin rule origins must record migration_failed source"


def test_pin_vms_none_adds_no_extra_pins():
    """pin_vms=None must not add any extra pin rules."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_DATA_WITH_GUEST, pin_vms=None)
    assert not any(r["vm"] == "vm-pinned" for r in cluster.constraints.pin)


def test_pin_vms_empty_set_adds_no_extra_pins():
    """pin_vms=set() must not add any extra pin rules."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_DATA_WITH_GUEST, pin_vms=set())
    assert not any(r["vm"] == "vm-pinned" for r in cluster.constraints.pin)
