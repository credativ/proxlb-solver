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

_GB = 1024 ** 3

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _base_data(**guest_overrides):
    """Minimal proxlb_data with two nodes and two VMs."""
    guests = {
        "vm-100": {
            "node_current": "node1", "node_target": None,
            "cpu_total": 2, "memory_total": 2 * _GB, "cpu_used": 0.1,
            "type": "vm", "priority": 2,
            "ha_rules": [], "tags": [], "pools": [], "ignore": False,
            "node_relationships": [],
        },
        "vm-101": {
            "node_current": "node2", "node_target": None,
            "cpu_total": 2, "memory_total": 2 * _GB, "cpu_used": 0.1,
            "type": "vm", "priority": 2,
            "ha_rules": [], "tags": [], "pools": [], "ignore": False,
            "node_relationships": [],
        },
    }
    guests.update(guest_overrides)
    return {
        "meta": {
            "cluster_name": "test",
            "balancing": {"method": "memory", "balanciness": 3, "cpu_overcommit": 2.0},
        },
        "nodes": {
            "node1": {"cpu_total": 8, "memory_total": 16 * _GB,
                      "memory_used": 2 * _GB, "memory_free": 14 * _GB,
                      "disk_free": 100 * _GB, "maintenance": False},
            "node2": {"cpu_total": 8, "memory_total": 16 * _GB,
                      "memory_used": 2 * _GB, "memory_free": 14 * _GB,
                      "disk_free": 100 * _GB, "maintenance": False},
        },
        "guests": guests,
        "pools": {}, "ha_rules": {}, "groups": {},
    }


def _read_jsonl(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _run_file(log_dir):
    files = [f for f in os.listdir(log_dir) if f.endswith(".jsonl")]
    assert len(files) == 1
    return os.path.join(log_dir, files[0])


# ---------------------------------------------------------------------------
# Adapter: affinity / anti-affinity origin tracking
# ---------------------------------------------------------------------------

class TestAffinityOrigins:

    def test_tag_affinity_origin_is_plb(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_affinity_web"]
        data["guests"]["vm-101"]["tags"] = ["plb_affinity_web"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.affinity
        assert any(r["name"] == "plb_affinity_web" and r["origin"] == "plb" for r in rules)

    def test_tag_anti_affinity_origin_is_plb(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_anti_affinity_db"]
        data["guests"]["vm-101"]["tags"] = ["plb_anti_affinity_db"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.anti_affinity
        assert any(r["name"] == "plb_anti_affinity_db" and r["origin"] == "plb" for r in rules)

    def test_ha_rule_affinity_origin_is_pve(self):
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = {"rule": "ha-rule-1", "type": "affinity", "nodes": [], "members": [100, 101]}
        data = _base_data()
        data["guests"]["vm-100"]["ha_rules"] = [ha_rule]
        data["guests"]["vm-101"]["ha_rules"] = [ha_rule]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.affinity
        assert any(r["name"] == "ha-rule-1" and r["origin"] == "pve" for r in rules)

    def test_ha_rule_anti_affinity_origin_is_pve(self):
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = {"rule": "ha-rule-2", "type": "anti-affinity", "nodes": [], "members": [100, 101]}
        data = _base_data()
        data["guests"]["vm-100"]["ha_rules"] = [ha_rule]
        data["guests"]["vm-101"]["ha_rules"] = [ha_rule]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.anti_affinity
        assert any(r["name"] == "ha-rule-2" and r["origin"] == "pve" for r in rules)

    def test_unknown_ha_rule_type_is_not_classified(self):
        """A HA rule with an unknown type must not silently land in anti-affinity."""
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = {"rule": "ha-rule-weird", "type": "unknown_future_type",
                   "nodes": [], "members": [100, 101]}
        data = _base_data()
        data["guests"]["vm-100"]["ha_rules"] = [ha_rule]
        data["guests"]["vm-101"]["ha_rules"] = [ha_rule]

        cluster = from_proxlb_data(data)
        names_aff = {r["name"] for r in cluster.constraints.affinity}
        names_aa = {r["name"] for r in cluster.constraints.anti_affinity}
        assert "ha-rule-weird" not in names_aff
        assert "ha-rule-weird" not in names_aa

    def test_pool_affinity_origin_is_plb(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["meta"]["balancing"]["pools"] = {"dev": {"type": "affinity"}}
        data["guests"]["vm-100"]["pools"] = ["dev"]
        data["guests"]["vm-101"]["pools"] = ["dev"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.affinity
        assert any(r["name"] == "dev" and r["origin"] == "plb" for r in rules)

    def test_pool_anti_affinity_origin_is_plb(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["meta"]["balancing"]["pools"] = {"db": {"type": "anti-affinity"}}
        data["guests"]["vm-100"]["pools"] = ["db"]
        data["guests"]["vm-101"]["pools"] = ["db"]

        cluster = from_proxlb_data(data)
        rules = cluster.constraints.anti_affinity
        assert any(r["name"] == "db" and r["origin"] == "plb" for r in rules)

    def test_single_member_group_is_excluded(self):
        """Groups with only one member must not produce a constraint (nothing to enforce)."""
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_affinity_lonely"]
        # vm-101 has no tag → only 1 member

        cluster = from_proxlb_data(data)
        names = {r["name"] for r in cluster.constraints.affinity}
        assert "plb_affinity_lonely" not in names


# ---------------------------------------------------------------------------
# Adapter: pin origin tracking
# ---------------------------------------------------------------------------

class TestPinOrigins:

    def test_tag_pin_origin_recorded(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_pin_node1"]
        data["guests"]["vm-100"]["node_relationships"] = ["node1"]

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert pins, "Expected a pin rule for vm-100"
        origins = pins[0]["origins"]
        assert any(o["origin"] == "tag" and "plb_pin_node1" in o["source"] for o in origins)

    def test_pool_pin_origin_recorded(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        data["meta"]["balancing"]["pools"] = {
            "licensed": {"type": "affinity", "pin": ["node1"]}
        }
        data["guests"]["vm-100"]["pools"] = ["licensed"]
        data["guests"]["vm-100"]["node_relationships"] = ["node1"]

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert pins
        origins = pins[0]["origins"]
        assert any(o["origin"] == "pool" and o["source"] == "licensed" for o in origins)

    def test_ha_rule_pin_origin_recorded(self):
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = {"rule": "ha-pin-rule", "type": "affinity",
                   "nodes": ["node1"], "members": [100]}
        data = _base_data()
        data["guests"]["vm-100"]["ha_rules"] = [ha_rule]
        data["guests"]["vm-100"]["node_relationships"] = ["node1"]

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert pins
        origins = pins[0]["origins"]
        assert any(o["origin"] == "pve" and o["source"] == "ha-pin-rule" for o in origins)

    def test_pin_nodes_come_from_node_relationships(self):
        """The validated node list must come from node_relationships, not raw tags."""
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        # Tag points to nonexistent node — ProxLB would filter it out
        data["guests"]["vm-100"]["tags"] = ["plb_pin_ghost-node"]
        # node_relationships already has ProxLB-validated result (empty here)
        data["guests"]["vm-100"]["node_relationships"] = []  # filtered by ProxLB

        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        # No pin rule because node_relationships is empty
        assert not pins, "Pin rule should not be created when node_relationships is empty"

    def test_no_pin_no_entry(self):
        from proxlb_solver.adapter import from_proxlb_data

        data = _base_data()
        cluster = from_proxlb_data(data)
        pins = [p for p in cluster.constraints.pin if p["vm"] == "vm-100"]
        assert not pins

    def test_anti_affinity_ha_rule_does_not_produce_pin(self):
        """HA rules with type=anti-affinity must not create pin constraints."""
        from proxlb_solver.adapter import from_proxlb_data

        ha_rule = {"rule": "aa-rule", "type": "anti-affinity",
                   "nodes": ["node1"], "members": [100, 101]}
        data = _base_data()
        data["guests"]["vm-100"]["ha_rules"] = [ha_rule]
        data["guests"]["vm-101"]["ha_rules"] = [ha_rule]
        # ProxLB doesn't add pin for anti-affinity rules with nodes
        data["guests"]["vm-100"]["node_relationships"] = []
        data["guests"]["vm-101"]["node_relationships"] = []

        cluster = from_proxlb_data(data)
        pins = cluster.constraints.pin
        assert not pins


# ---------------------------------------------------------------------------
# Shadow JSONL: constraint events appear before solver_run
# ---------------------------------------------------------------------------

class TestConstraintLogging:

    def test_constraint_events_precede_solver_run(self, tmp_path):
        """All constraint events must be written before the solver_run event."""
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_affinity_web"]
        data["guests"]["vm-101"]["tags"] = ["plb_affinity_web"]

        run_shadow(data, {"log_dir": str(tmp_path)})

        events = _read_jsonl(_run_file(str(tmp_path)))
        types = [e["event"] for e in events]

        solver_idx = types.index("solver_run")
        constraint_indices = [i for i, t in enumerate(types) if t == "constraint"]

        assert constraint_indices, "Expected at least one constraint event"
        assert all(i < solver_idx for i in constraint_indices), (
            "All constraint events must appear before solver_run"
        )

    def test_affinity_constraint_event_fields(self, tmp_path):
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_affinity_web"]
        data["guests"]["vm-101"]["tags"] = ["plb_affinity_web"]

        run_shadow(data, {"log_dir": str(tmp_path)})

        events = _read_jsonl(_run_file(str(tmp_path)))
        affinity_events = [e for e in events
                           if e["event"] == "constraint" and e["type"] == "affinity"]

        assert affinity_events, "Expected affinity constraint event"
        ev = affinity_events[0]
        assert "name" in ev
        assert ev["origin"] == "plb"
        assert set(ev["vms"]) == {"vm-100", "vm-101"}
        assert "hard" in ev

    def test_pin_constraint_event_has_origins(self, tmp_path):
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data["guests"]["vm-100"]["tags"] = ["plb_pin_node1"]
        data["guests"]["vm-100"]["node_relationships"] = ["node1"]

        run_shadow(data, {"log_dir": str(tmp_path)})

        events = _read_jsonl(_run_file(str(tmp_path)))
        pin_events = [e for e in events
                      if e["event"] == "constraint" and e["type"] == "pin"]

        assert pin_events, "Expected pin constraint event"
        ev = pin_events[0]
        assert ev["vm"] == "vm-100"
        assert "node1" in ev["nodes"]
        assert isinstance(ev["origins"], list)
        assert any(o["origin"] == "tag" for o in ev["origins"])

    def test_ignore_constraint_event(self, tmp_path):
        from proxlb_solver.shadow import run_shadow

        data = _base_data()
        data["guests"]["vm-100"]["ignore"] = True

        run_shadow(data, {"log_dir": str(tmp_path)})

        events = _read_jsonl(_run_file(str(tmp_path)))
        ignore_events = [e for e in events
                         if e["event"] == "constraint" and e["type"] == "ignore"]

        assert any(e["vm"] == "vm-100" for e in ignore_events)

    def test_no_constraints_no_constraint_events(self, tmp_path):
        from proxlb_solver.shadow import run_shadow

        run_shadow(_base_data(), {"log_dir": str(tmp_path)})

        events = _read_jsonl(_run_file(str(tmp_path)))
        constraint_events = [e for e in events if e["event"] == "constraint"]
        assert not constraint_events

    def test_pve_ha_rule_constraint_event(self, tmp_path):
        from proxlb_solver.shadow import run_shadow

        ha_rule = {"rule": "ha-rule-1", "type": "affinity", "nodes": [], "members": [100, 101]}
        data = _base_data()
        data["guests"]["vm-100"]["ha_rules"] = [ha_rule]
        data["guests"]["vm-101"]["ha_rules"] = [ha_rule]

        run_shadow(data, {"log_dir": str(tmp_path)})

        events = _read_jsonl(_run_file(str(tmp_path)))
        ha_constraints = [
            e for e in events
            if e["event"] == "constraint"
            and e.get("origin") == "pve"
            and e.get("name") == "ha-rule-1"
        ]
        assert ha_constraints, "Expected pve-origin constraint event for HA rule"
