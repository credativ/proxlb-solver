"""Unit tests for the ProxLB → Solver adapter, focusing on reservation handling."""

from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData

from ..utils import MINIMAL_DATA, create_guest, create_node, create_node_metric

_GB = 1024 ** 3
_Resource = Config.Balancing.Resource


def _base_data(node_resource_reserve: dict[str, dict[_Resource, int]] | None = None) -> ProxLbData:
    """Two identical nodes (16 GB raw, 12 GB pre-reduced), no guests."""
    data = MINIMAL_DATA.model_copy(deep=True)
    data.meta.balancing.method = _Resource.Memory
    data.meta.balancing.balanciness = 3
    if node_resource_reserve is not None:
        # Keys are Config.Balancing.Resource StrEnums; strings coerce automatically.
        data.meta.balancing.node_resource_reserve = node_resource_reserve

    def _node(name: str) -> ProxLbData.Node:
        return create_node(
            name=name,
            cpu=create_node_metric(total=8),
            disk=create_node_metric(free=100 * _GB),
            memory=create_node_metric(
                total=12 * _GB,  # pre-reduced
                used=2 * _GB,    # raw
                free=14 * _GB,   # raw  →  total = 16 GB
            ),
        )

    data.nodes = {"node1": _node("node1"), "node2": _node("node2")}
    return data


_PROXLB_DATA_WITH_RESERVE = _base_data(node_resource_reserve={
    "defaults": {_Resource.Memory: 4},  # 4 GB default
    "node2":    {_Resource.Memory: 6},  # 6 GB node-specific override
})


def test_raw_memory_total_used() -> None:
    """Adapter must use memory_used + memory_free as the raw hardware total."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    assert node1.memory_total == 16 * _GB, (
        f"Expected raw 16 GB, got {node1.memory_total / _GB:.1f} GB"
    )


def test_default_reservation_applied() -> None:
    """Default node_resource_reserve is applied as memory_reserve (in bytes)."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    assert node1.memory_reserve == 4 * _GB, (
        f"Expected 4 GB reserve, got {node1.memory_reserve / _GB:.1f} GB"
    )


def test_node_specific_reservation_overrides_default() -> None:
    """Per-node reservation overrides the default for that node."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=True)
    node2 = next(n for n in cluster.nodes if n.name == "node2")

    assert node2.memory_reserve == 6 * _GB, (
        f"Expected 6 GB reserve for node2, got {node2.memory_reserve / _GB:.1f} GB"
    )


def test_use_reservations_false_sets_reserve_to_zero() -> None:
    """use_reservations=False must leave memory_reserve at 0."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=False)
    for node in cluster.nodes:
        assert node.memory_reserve == 0, (
            f"Node {node.name}: expected reserve=0, got {node.memory_reserve}"
        )


def test_effective_capacity_equals_raw_minus_reserve() -> None:
    """The solver constraint uses memory_total - memory_reserve; verify that value."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_PROXLB_DATA_WITH_RESERVE, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    effective = node1.memory_total - node1.memory_reserve
    assert effective == 12 * _GB, (
        f"Expected effective capacity 12 GB (= 16 - 4), got {effective / _GB:.1f} GB"
    )


def test_fallback_to_memory_total_when_raw_fields_absent() -> None:
    """Without memory_used/memory_free, adapter falls back to memory_total with reserve=0."""
    from proxlb_solver.adapter import from_proxlb_data

    data = MINIMAL_DATA.model_copy(deep=True)
    data.meta.balancing.node_resource_reserve = {"defaults": {_Resource.Memory: 4}}
    data.nodes = {
        "node1": create_node(
            name="node1",
            cpu=create_node_metric(total=4),
            disk=create_node_metric(free=0),
            memory=create_node_metric(total=12 * _GB, used=0, free=0),
        ),
    }

    cluster = from_proxlb_data(data, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    # Falls back to memory_total; reserve cannot be separated, stays 0.
    assert node1.memory_total == 12 * _GB
    assert node1.memory_reserve == 0


def test_reservation_clamped_to_raw_memory() -> None:
    """A reservation larger than the node's RAM is clamped, not applied blindly."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data(node_resource_reserve={
        "defaults": {_Resource.Memory: 100},  # 100 GB > 16 GB node
    })
    cluster = from_proxlb_data(data, use_reservations=True)
    node1 = next(n for n in cluster.nodes if n.name == "node1")

    assert node1.memory_reserve <= node1.memory_total, (
        "Reserve must never exceed memory_total"
    )


# ---------------------------------------------------------------------------
# pin_vms parameter tests
# ---------------------------------------------------------------------------

def _data_with_guest() -> ProxLbData:
    data = MINIMAL_DATA.model_copy(deep=True)
    data.nodes = {
        "node1": create_node(
            name="node1",
            cpu=create_node_metric(total=4),
            disk=create_node_metric(free=50 * _GB),
            memory=create_node_metric(total=8 * _GB, used=1 * _GB, free=7 * _GB),
        ),
    }
    vm = create_guest("vm-pinned", node_current="node1", node_target="node1")
    vm.cpu.total = 2
    vm.cpu.used = 0.1
    vm.memory.total = 2 * _GB
    data.guests = {"vm-pinned": vm}
    return data


_DATA_WITH_GUEST = _data_with_guest()


def test_pin_vms_parameter_pins_to_current_node() -> None:
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


def test_pin_vms_none_adds_no_extra_pins() -> None:
    """pin_vms=None must not add any extra pin rules."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_DATA_WITH_GUEST, pin_vms=None)
    assert not any(r["vm"] == "vm-pinned" for r in cluster.constraints.pin)


def test_pin_vms_empty_set_adds_no_extra_pins() -> None:
    """pin_vms=set() must not add any extra pin rules."""
    from proxlb_solver.adapter import from_proxlb_data

    cluster = from_proxlb_data(_DATA_WITH_GUEST, pin_vms=set())
    assert not any(r["vm"] == "vm-pinned" for r in cluster.constraints.pin)


# ---------------------------------------------------------------------------
# LXC Container support tests
# ---------------------------------------------------------------------------

def test_adapter_handles_lxc_type() -> None:
    """Adapter must correctly map ProxLB guest type 'ct' to VM.vm_type."""
    from proxlb_solver.adapter import from_proxlb_data

    data = MINIMAL_DATA.model_copy(deep=True)
    data.nodes = {
        "node1": create_node(
            name="node1",
            cpu=create_node_metric(total=4),
            disk=create_node_metric(free=0),
            memory=create_node_metric(total=8 * _GB, used=0, free=0),
        ),
    }
    ct = create_guest("container-1", node_current="node1", node_target="node1")
    ct.cpu.total = 1
    ct.memory.total = 1 * _GB
    ct.type = Config.GuestType.Ct
    data.guests = {"container-1": ct}

    cluster = from_proxlb_data(data)
    out = next(v for v in cluster.vms if v.name == "container-1")

    assert out.vm_type == "ct", f"Expected vm_type='ct', got {out.vm_type!r}"


# ---------------------------------------------------------------------------
# Field propagation: node attributes, VM attributes, balancing config
# ---------------------------------------------------------------------------

def test_node_maintenance_flag_propagated() -> None:
    """Node.maintenance must round-trip from ProxLbData into the solver model."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.nodes["node1"].maintenance = True
    cluster = from_proxlb_data(data)
    by_name = {n.name: n for n in cluster.nodes}
    assert by_name["node1"].maintenance is True
    assert by_name["node2"].maintenance is False


def test_node_psi_pressures_propagated() -> None:
    """Per-resource PSI pressure values must propagate to Node.{cpu,memory,io}_pressure."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.nodes["node1"].cpu.pressure_some_percent = 12.5
    data.nodes["node1"].memory.pressure_some_percent = 7.3
    data.nodes["node1"].disk.pressure_some_percent = 4.1
    cluster = from_proxlb_data(data)
    n = next(n for n in cluster.nodes if n.name == "node1")

    assert n.cpu_pressure == 12.5
    assert n.memory_pressure == 7.3
    assert n.io_pressure == 4.1


def test_vm_name_node_cpu_memory_propagated() -> None:
    """VM identity and capacity fields must propagate from the guest entry."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _data_with_guest()
    data.guests["vm-pinned"].cpu.total = 4
    data.guests["vm-pinned"].memory.total = 8 * _GB
    cluster = from_proxlb_data(data)
    vm = next(v for v in cluster.vms if v.name == "vm-pinned")

    assert vm.name == "vm-pinned"
    assert vm.node == "node1"
    assert vm.cpu == 4
    assert vm.memory == 8 * _GB


def test_ignore_flag_adds_to_ignore_list() -> None:
    """Guests with .ignore=True land in constraints.ignore; others do not."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _data_with_guest()
    data.guests["vm-pinned"].ignore = True
    other = create_guest("vm-normal", node_current="node1", node_target="node1")
    other.memory.total = 1 * _GB
    data.guests["vm-normal"] = other

    cluster = from_proxlb_data(data)
    assert "vm-pinned" in cluster.constraints.ignore
    assert "vm-normal" not in cluster.constraints.ignore


def test_single_member_tag_group_not_a_constraint() -> None:
    """A tag-affinity group with only one guest must not produce a constraint."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _data_with_guest()
    data.guests["vm-pinned"].tags = ["plb_affinity_solo"]
    cluster = from_proxlb_data(data)
    names = {r["name"] for r in cluster.constraints.affinity}
    assert "plb_affinity_solo" not in names


def test_balancing_method_balanciness_and_thresholds_propagated() -> None:
    """method, balanciness, and the *_threshold fields propagate into Balancing."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.meta.balancing.method = _Resource.Cpu
    data.meta.balancing.balanciness = 5
    data.meta.balancing.memory_threshold = 80
    data.meta.balancing.cpu_threshold = 70
    data.meta.balancing.disk_threshold = 60

    cluster = from_proxlb_data(data)
    assert cluster.balancing.method == "cpu"
    assert cluster.balancing.balanciness == 5
    assert cluster.balancing.memory_threshold == 80
    assert cluster.balancing.cpu_threshold == 70
    assert cluster.balancing.disk_threshold == 60


def test_cpu_overcommit_propagated() -> None:
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.meta.balancing.cpu_overcommit = 4.0
    cluster = from_proxlb_data(data)
    assert cluster.balancing.cpu_overcommit == 4.0


def test_max_node_inflow_propagated_when_set() -> None:
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.meta.balancing.max_node_inflow = 3
    cluster = from_proxlb_data(data)
    assert cluster.balancing.max_node_inflow == 3


def test_max_node_inflow_none_flows_through_as_unbounded() -> None:
    """ProxLB users opt into unbounded inflow by setting `max_node_inflow: ~`
    in YAML; the value must reach the solver as None so the planner's
    `if max_inflow and ...` guard short-circuits and skips the cap."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.meta.balancing.max_node_inflow = None
    cluster = from_proxlb_data(data)
    assert cluster.balancing.max_node_inflow is None


def test_parallel_true_uses_parallel_jobs_as_max_parallel_migrations() -> None:
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.meta.balancing.parallel = True
    data.meta.balancing.parallel_jobs = 7
    cluster = from_proxlb_data(data)
    assert cluster.balancing.max_parallel_migrations == 7


def test_parallel_false_caps_max_parallel_migrations_at_one() -> None:
    """parallel=False means sequential — even if parallel_jobs is non-default."""
    from proxlb_solver.adapter import from_proxlb_data

    data = _base_data()
    data.meta.balancing.parallel = False
    data.meta.balancing.parallel_jobs = 7
    cluster = from_proxlb_data(data)
    assert cluster.balancing.max_parallel_migrations == 1
