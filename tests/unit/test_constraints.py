"""
Tests for constraint extraction (affinity, anti-affinity, pins, ignore)
and their JSONL logging in shadow mode.

Covers:
- Origin tracking for all constraint types
- Explicit HA rule type check (no silent else-fallthrough)
- Pin origins from tag / pool / HA rule
- Constraint events appear in JSONL before solver_run
"""

import json
import os
import pytest

from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData

from typing import Any

from ..utils import MINIMAL_DATA, create_guest, create_node, create_node_metric

_GB = 1024 ** 3

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _base_data() -> ProxLbData:
    """Minimal proxlb_data with two nodes and two VMs."""

    node1 = create_node(
        name="node1",
        cpu=create_node_metric(total=8),
        disk=create_node_metric(free=100*_GB),
        memory=create_node_metric(
            free=14*_GB,
            total=16*_GB,
            used=2*_GB,
        ),
    )
    node2 = node1.model_copy(
        update={"name": "node2"},
        deep=True,
    )

    proxlb_data = MINIMAL_DATA.model_copy()

    proxlb_data.nodes = {
        "node1": node1,
        "node2": node2,
    }

    proxlb_data.guests = {
        "vm-100": create_guest("vm-100", node_current="node1", node_target="node1"),
        "vm-101": create_guest("vm-101", node_current="node2", node_target="node2"),
    }

    return proxlb_data


def _read_jsonl(path: str) -> list[Any]:
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _run_file(log_dir: str) -> str:
    files = [f for f in os.listdir(log_dir) if f.endswith(".jsonl")]
    assert len(files) == 1
    return os.path.join(log_dir, files[0])


# ---------------------------------------------------------------------------
# Adapter: affinity / anti-affinity origin tracking
# ---------------------------------------------------------------------------

class TestAffinityOrigins:

    def test_tag_affinity_origin_is_plb(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_affinity_web"]
        data.guests["vm-101"].tags = ["plb_affinity_web"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.affinity
        assert any(r["name"] == "plb_affinity_web" and r["origin"] == "plb" for r in rules)

    def test_tag_anti_affinity_origin_is_plb(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_anti_affinity_db"]
        data.guests["vm-101"].tags = ["plb_anti_affinity_db"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.anti_affinity
        assert any(r["name"] == "plb_anti_affinity_db" and r["origin"] == "plb" for r in rules)

    def test_ha_rule_affinity_origin_is_pve(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = ProxLbData.HaRule(rule="ha-rule-1", type=Config.AffinityType.PositiveAffinity, nodes=[], members=[100, 101])
        data = _base_data()
        data.guests["vm-100"].ha_rules = [ha_rule]
        data.guests["vm-101"].ha_rules = [ha_rule]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.affinity
        assert any(r["name"] == "ha-rule-1" and r["origin"] == "pve" for r in rules)

    def test_ha_rule_anti_affinity_origin_is_pve(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = ProxLbData.HaRule(rule="ha-rule-2", type=Config.AffinityType.NegativeAffinity, nodes=[], members=[100, 101])
        data = _base_data()
        data.guests["vm-100"].ha_rules = [ha_rule]
        data.guests["vm-101"].ha_rules = [ha_rule]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.anti_affinity
        assert any(r["name"] == "ha-rule-2" and r["origin"] == "pve" for r in rules)

    def test_pool_affinity_origin_is_plb(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.meta.balancing.pools = {"dev": Config.Balancing.Pool(type=Config.AffinityType.PositiveAffinity)}
        data.guests["vm-100"].pools = ["dev"]
        data.guests["vm-101"].pools = ["dev"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.affinity
        assert any(r["name"] == "dev" and r["origin"] == "plb" for r in rules)

    def test_pool_anti_affinity_origin_is_plb(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.meta.balancing.pools = {"db": Config.Balancing.Pool(type=Config.AffinityType.NegativeAffinity)}
        data.guests["vm-100"].pools = ["db"]
        data.guests["vm-101"].pools = ["db"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.anti_affinity
        assert any(r["name"] == "db" and r["origin"] == "plb" for r in rules)

    def test_single_member_group_is_excluded(self) -> None:
        """Groups with only one member must not produce a constraint (nothing to enforce)."""
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_affinity_lonely"]
        # vm-101 has no tag → only 1 member

        cluster = from_proxlb_data(data)
        names = {r["name"] for r in cluster.constraints.affinity}
        assert "plb_affinity_lonely" not in names


# ---------------------------------------------------------------------------
# Adapter: pin origin tracking
# ---------------------------------------------------------------------------

class TestPinOrigins:

    def test_tag_pin_origin_recorded(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_pin_node1"]
        data.guests["vm-100"].node_relationships = ["node1"]

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert pins, "Expected a pin rule for vm-100"
        origins = pins[0]["origins"]
        assert any(o["origin"] == "tag" and "plb_pin_node1" in o["source"] for o in origins)

    def test_pool_pin_origin_recorded(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data.meta.balancing.pools = {
            "licensed": Config.Balancing.Pool(
                type=Config.AffinityType.PositiveAffinity,
                pin=["node1"],
            ),
        }
        data.guests["vm-100"].pools = ["licensed"]
        data.guests["vm-100"].node_relationships = ["node1"]

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert pins
        origins = pins[0]["origins"]
        assert any(o["origin"] == "pool" and o["source"] == "licensed" for o in origins)

    def test_ha_rule_pin_origin_recorded(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = ProxLbData.HaRule(
            rule="ha-pin-rule",
            type=Config.AffinityType.PositiveAffinity,
            nodes=["node1"],
            members=[100],
        )
        data = _base_data()
        data.guests["vm-100"].ha_rules = [ha_rule]
        data.guests["vm-100"].node_relationships = ["node1"]

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert pins
        origins = pins[0]["origins"]
        assert any(o["origin"] == "pve" and o["source"] == "ha-pin-rule" for o in origins)

    def test_pin_nodes_come_from_node_relationships(self) -> None:
        """The validated node list must come from node_relationships, not raw tags."""
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        # Tag points to nonexistent node — ProxLB would filter it out
        data.guests["vm-100"].tags = ["plb_pin_ghost-node"]
        # node_relationships already has ProxLB-validated result (empty here)
        data.guests["vm-100"].node_relationships = []  # filtered by ProxLB

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        # No pin rule because node_relationships is empty
        assert not pins, "Pin rule should not be created when node_relationships is empty"

    def test_no_pin_no_entry(self) -> None:
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert not pins

    def test_anti_affinity_ha_rule_does_not_produce_pin(self) -> None:
        """HA rules with type=anti-affinity must not create pin constraints."""
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = ProxLbData.HaRule(
            rule="aa-rule",
            type=Config.AffinityType.NegativeAffinity,
            nodes=["node1"],
            members=[100, 101],
        )
        data = _base_data()
        data.guests["vm-100"].ha_rules = [ha_rule]
        data.guests["vm-101"].ha_rules = [ha_rule]
        # ProxLB doesn't add pin for anti-affinity rules with nodes
        data.guests["vm-100"].node_relationships = []
        data.guests["vm-101"].node_relationships = []

        cluster = from_proxlb_data(data)
        pins = cluster.constraints.pin
        assert not pins


# ---------------------------------------------------------------------------
# Shadow JSONL: constraint events appear before solver_run
# ---------------------------------------------------------------------------

class TestConstraintLogging:

    def test_constraint_events_precede_solver_run(self, tmp_path: str) -> None:
        """All constraint events must be written before the solver_run event."""
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_affinity_web"]
        data.guests["vm-101"].tags = ["plb_affinity_web"]

        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

        events = _read_jsonl(_run_file(str(tmp_path)))
        types = [e["event"] for e in events]

        solver_idx = types.index("solver_run")
        constraint_indices = [i for i, t in enumerate(types) if t == "constraint"]

        assert constraint_indices, "Expected at least one constraint event"
        assert all(i < solver_idx for i in constraint_indices), (
            "All constraint events must appear before solver_run"
        )

    def test_affinity_constraint_event_fields(self, tmp_path: str) -> None:
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_affinity_web"]
        data.guests["vm-101"].tags = ["plb_affinity_web"]

        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

        events = _read_jsonl(_run_file(str(tmp_path)))
        affinity_events = [e for e in events
                           if e["event"] == "constraint" and e["type"] == "affinity"]

        assert affinity_events, "Expected affinity constraint event"
        ev = affinity_events[0]
        assert "name" in ev
        assert ev["origin"] == "plb"
        assert set(ev["vms"]) == {"vm-100", "vm-101"}
        assert "hard" in ev

    def test_pin_constraint_event_has_origins(self, tmp_path: str) -> None:
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data.guests["vm-100"].tags = ["plb_pin_node1"]
        data.guests["vm-100"].node_relationships = ["node1"]

        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

        events = _read_jsonl(_run_file(str(tmp_path)))
        pin_events = [e for e in events
                      if e["event"] == "constraint" and e["type"] == "pin"]

        assert pin_events, "Expected pin constraint event"
        ev = pin_events[0]
        assert ev["vm"] == "vm-100"
        assert "node1" in ev["nodes"]
        assert isinstance(ev["origins"], list)
        assert any(o["origin"] == "tag" for o in ev["origins"])

    def test_ignore_constraint_event(self, tmp_path: str) -> None:
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data.guests["vm-100"].ignore = True

        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

        events = _read_jsonl(_run_file(str(tmp_path)))
        ignore_events = [e for e in events
                         if e["event"] == "constraint" and e["type"] == "ignore"]

        assert any(e["vm"] == "vm-100" for e in ignore_events)

    def test_no_constraints_no_constraint_events(self, tmp_path: str) -> None:
        from proxlb_solver.shadow import run_shadow

        run_shadow(_base_data(), Config.Solver(log_dir=str(tmp_path)))

        events = _read_jsonl(_run_file(str(tmp_path)))
        constraint_events = [e for e in events if e["event"] == "constraint"]
        assert not constraint_events

    def test_pve_ha_rule_constraint_event(self, tmp_path: str) -> None:
        from proxlb_solver.shadow import run_shadow

        ha_rule = ProxLbData.HaRule(rule="ha-rule-1", type=Config.AffinityType.PositiveAffinity, nodes=[], members=[100, 101])
        data = _base_data()
        data.guests["vm-100"].ha_rules = [ha_rule]
        data.guests["vm-101"].ha_rules = [ha_rule]

        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

        events = _read_jsonl(_run_file(str(tmp_path)))
        ha_constraints = [
            e for e in events
            if e["event"] == "constraint"
            and e.get("origin") == "pve"
            and e.get("name") == "ha-rule-1"
        ]
        assert ha_constraints, "Expected pve-origin constraint event for HA rule"
