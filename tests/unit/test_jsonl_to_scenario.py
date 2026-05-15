"""Round-trip tests for scripts/jsonl_to_scenario.py.

Loads a scenario, asks the shadow writers to emit the cluster_state and
constraint events that would be logged at runtime, then converts that
JSONL back into a YAML scenario and reloads it. The resulting Cluster
must match the original in every field the scenario format can
represent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from proxlb_solver.loader import load_scenario
from proxlb_solver.shadow import _write_cluster_state, _write_constraints
from proxlb_solver.models import Cluster

from ..utils import MINIMAL_DATA


# Load the converter as a module — it lives under scripts/, not in a package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT    = _REPO_ROOT / "scripts" / "jsonl_to_scenario.py"
_spec = importlib.util.spec_from_file_location("jsonl_to_scenario", _SCRIPT)
j2s   = importlib.util.module_from_spec(_spec)        # type: ignore[arg-type]
_spec.loader.exec_module(j2s)                          # type: ignore[union-attr]


# Scenarios chosen to exercise different parts of the schema:
#   simple_rebalance      — bare minimum: 2 nodes, 2 VMs, no constraints
#   pin_to_node           — adds a pin constraint
#   hard_anti_affinity_conflict — adds an anti-affinity constraint
_ROUND_TRIP_SCENARIOS = [
    "basic/simple_rebalance.yaml",
    "constraints/pin_to_node.yaml",
    "infeasible/hard_anti_affinity_conflict.yaml",
]


def _emit_jsonl(cluster: Cluster, path: Path) -> None:
    """Write the events the round-trip needs: cluster_state + constraints."""
    with open(path, "w") as f:
        # _write_cluster_state needs proxlb_data only for the runtime
        # ``memory_used`` field. Empty data is fine for round-trip tests.
        _write_cluster_state(cluster, MINIMAL_DATA, f)
        _write_constraints(cluster, f)


def _round_trip(scenario_rel: str, tmp_path: Path) -> tuple[Cluster, Cluster]:
    orig = load_scenario(_REPO_ROOT / "scenarios" / scenario_rel)

    jsonl_path = tmp_path / "snapshot.jsonl"
    _emit_jsonl(orig, jsonl_path)

    out_path = tmp_path / "reconstructed.yaml"
    j2s.main([str(jsonl_path), "-o", str(out_path)])

    new = load_scenario(out_path)
    return orig, new


@pytest.mark.parametrize("scenario_rel", _ROUND_TRIP_SCENARIOS)
def test_round_trip_nodes_match(scenario_rel: str, tmp_path: Path) -> None:
    orig, new = _round_trip(scenario_rel, tmp_path)

    orig_nodes = {n.name: n for n in orig.nodes}
    new_nodes  = {n.name: n for n in new.nodes}
    assert orig_nodes.keys() == new_nodes.keys()

    for name, on in orig_nodes.items():
        nn = new_nodes[name]
        assert nn.cpu_total       == on.cpu_total
        assert nn.memory_total    == on.memory_total
        assert nn.memory_reserve  == on.memory_reserve
        assert nn.cpu_reserve     == on.cpu_reserve
        assert dict(nn.storage_free)    == dict(on.storage_free)
        assert dict(nn.storage_reserve) == dict(on.storage_reserve)
        assert nn.cpu_pressure    == on.cpu_pressure
        assert nn.memory_pressure == on.memory_pressure
        assert nn.io_pressure     == on.io_pressure
        assert nn.maintenance     == on.maintenance


@pytest.mark.parametrize("scenario_rel", _ROUND_TRIP_SCENARIOS)
def test_round_trip_vms_match(scenario_rel: str, tmp_path: Path) -> None:
    orig, new = _round_trip(scenario_rel, tmp_path)

    orig_vms = {v.name: v for v in orig.vms}
    new_vms  = {v.name: v for v in new.vms}
    assert orig_vms.keys() == new_vms.keys()

    for name, ov in orig_vms.items():
        nv = new_vms[name]
        assert nv.node            == ov.node
        assert nv.cpu             == ov.cpu
        assert nv.memory          == ov.memory
        assert nv.cpu_usage       == ov.cpu_usage
        assert nv.cpu_pressure    == ov.cpu_pressure
        assert nv.memory_pressure == ov.memory_pressure
        assert nv.io_pressure     == ov.io_pressure
        assert dict(nv.disks)     == dict(ov.disks)
        assert nv.priority        == ov.priority
        assert nv.vm_type         == ov.vm_type


@pytest.mark.parametrize("scenario_rel", _ROUND_TRIP_SCENARIOS)
def test_round_trip_balancing_match(scenario_rel: str, tmp_path: Path) -> None:
    orig, new = _round_trip(scenario_rel, tmp_path)

    # Compare the full Balancing config — all weights, thresholds, modes.
    assert orig.balancing.model_dump() == new.balancing.model_dump()


@pytest.mark.parametrize("scenario_rel", _ROUND_TRIP_SCENARIOS)
def test_round_trip_constraints_match(scenario_rel: str, tmp_path: Path) -> None:
    orig, new = _round_trip(scenario_rel, tmp_path)

    def _norm(rules: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        # Sort by name (and origin where relevant) so order doesn't matter.
        return sorted(
            (r.get("name"), r.get("origin"), tuple(sorted(r.get("vms", []))), r.get("hard", True))
            for r in rules
        )

    assert _norm(orig.constraints.affinity)      == _norm(new.constraints.affinity)
    assert _norm(orig.constraints.anti_affinity) == _norm(new.constraints.anti_affinity)

    def _norm_pin(rules: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return sorted((r["vm"], tuple(sorted(r["nodes"]))) for r in rules)

    assert _norm_pin(orig.constraints.pin) == _norm_pin(new.constraints.pin)
    assert sorted(orig.constraints.ignore) == sorted(new.constraints.ignore)


def test_missing_cluster_state_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(SystemExit):
        j2s.jsonl_to_scenario(empty)


def test_event_schema_self_contained(tmp_path: Path) -> None:
    """A real cluster_state event should contain everything needed for round-trip."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "basic" / "simple_rebalance.yaml")
    jsonl_path = tmp_path / "snapshot.jsonl"
    _emit_jsonl(cluster, jsonl_path)

    with open(jsonl_path) as f:
        events = [json.loads(line) for line in f if line.strip()]

    cs = next(e for e in events if e["event"] == "cluster_state")
    assert "balancing" in cs, "balancing block must be present for full reconstruction"
    assert "nodes" in cs and "guests" in cs

    sample_node = next(iter(cs["nodes"].values()))
    for field in ("cpu_total", "memory_total", "memory_reserve", "cpu_reserve",
                  "storage_free", "storage_reserve",
                  "cpu_pressure", "memory_pressure", "io_pressure", "maintenance"):
        assert field in sample_node, f"node entry missing {field!r}"

    sample_guest = next(iter(cs["guests"].values()))
    for field in ("node", "cpu", "memory", "cpu_usage", "disks",
                  "cpu_pressure", "memory_pressure", "io_pressure",
                  "priority", "vm_type"):
        assert field in sample_guest, f"guest entry missing {field!r}"


# ---------------------------------------------------------------------------
# Logs produced at runtime (adapter-built clusters), not via load_scenario.
# ---------------------------------------------------------------------------

_GiB = 1024 ** 3
_MiB = 1024 ** 2


def _runtime_cluster(**overrides: Any) -> Cluster:
    """A cluster built like the adapter builds it: model-default Balancing."""
    from proxlb_solver.models import Balancing, Constraints, Expect, Method, Node, VM
    fields: dict[str, Any] = dict(
        name="rt", description="",
        balancing=Balancing(method=Method.MemorySmart),
        nodes=[Node(name="n1", cpu_total=8, memory_total=16 * _GiB),
               Node(name="n2", cpu_total=8, memory_total=16 * _GiB)],
        vms=[VM(name="vm", node="n1", cpu=2, memory=2 * _GiB)],
        constraints=Constraints(), expect=Expect(),
    )
    fields.update(overrides)
    return Cluster(**fields)


def _convert(cluster: Cluster, tmp_path: Path) -> Cluster:
    jsonl_path = tmp_path / "snapshot.jsonl"
    _emit_jsonl(cluster, jsonl_path)
    out_path = tmp_path / "reconstructed.yaml"
    j2s.main([str(jsonl_path), "-o", str(out_path)])
    return load_scenario(out_path)


def test_model_default_weights_survive(tmp_path: Path) -> None:
    """The loader's weight defaults differ from the model's; none may be lost."""
    orig = _runtime_cluster()
    new = _convert(orig, tmp_path)
    assert new.balancing.model_dump() == orig.balancing.model_dump()


def test_byte_values_are_exact(tmp_path: Path) -> None:
    """Capacities that aren't whole GiB must come back to the byte."""
    from proxlb_solver.models import Node, VM
    odd = 15 * _GiB + 123_456_789
    orig = _runtime_cluster(
        nodes=[Node(name="n1", cpu_total=8, memory_total=odd,
                    memory_reserve=_GiB + 7 * _MiB + 3,
                    storage_free={"local": 512 * _GiB + 1})],
        vms=[VM(name="vm", node="n1", cpu=2, memory=2 * _GiB + 1,
                disks={"local": 10 * _GiB + 4095})],
    )
    new = _convert(orig, tmp_path)
    assert new.nodes[0].memory_total == odd
    assert new.nodes[0].memory_reserve == orig.nodes[0].memory_reserve
    assert dict(new.nodes[0].storage_free) == dict(orig.nodes[0].storage_free)
    assert new.vms[0].memory == orig.vms[0].memory
    assert dict(new.vms[0].disks) == dict(orig.vms[0].disks)


def test_vm_on_missing_node_gets_maintenance_placeholder(tmp_path: Path) -> None:
    """The solver accepts a VM on a gone node; the scenario must still load."""
    from proxlb_solver.models import VM
    orig = _runtime_cluster(vms=[VM(name="vm", node="gone", cpu=2, memory=2 * _GiB)])
    new = _convert(orig, tmp_path)
    placeholder = next(n for n in new.nodes if n.name == "gone")
    assert placeholder.maintenance
    assert new.vms[0].node == "gone"
    assert "gone" in new.description


def test_partial_cluster_state_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "partial.jsonl"
    path.write_text(json.dumps({"event": "cluster_state", "partial": True,
                                "method": "memory", "nodes": {}, "guests": {}}) + "\n")
    with pytest.raises(SystemExit, match="partial"):
        j2s.jsonl_to_scenario(path)
