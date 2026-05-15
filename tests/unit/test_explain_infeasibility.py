"""Tests for solver.explain_infeasibility.

The function builds a feasibility-only CP-SAT model with assumption literals
attached to each blameable rule, then returns a minimal subset of those
rules that suffices to prove infeasibility.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proxlb_solver.loader import load_scenario
from proxlb_solver.solver import explain_infeasibility, solve


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _types(blame: list[dict]) -> set[str]:
    return {b["type"] for b in blame}


def test_anti_affinity_conflict_blames_rule(tmp_path):
    """3 mutually anti-affine VMs on 2 nodes → the rule is the blame."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "hard_anti_affinity_conflict.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    assert blame, "expected non-empty blame"
    assert "anti_affinity" in _types(blame)
    aa = next(b for b in blame if b["type"] == "anti_affinity")
    assert aa["name"] == "Hard-AA"
    assert set(aa["vms"]) == {"vm-1", "vm-2", "vm-3"}


def test_pin_to_maintenance_blames_pin_and_maintenance():
    """VM pinned to a node in maintenance → both rules in the blame."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "pin_to_maintenance.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    types = _types(blame)
    assert "maintenance" in types
    assert "pin" in types


def test_affinity_pinning_conflict_blames_pins_and_affinity():
    """Two VMs share a hard affinity but are pinned to different nodes."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "hard_affinity_pinning_conflict.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    types = _types(blame)
    assert "affinity" in types
    # Both pin rules are independently needed to prove the affinity unsat.
    pin_vms = {b["vm"] for b in blame if b["type"] == "pin"}
    assert "vm-1" in pin_vms and "vm-2" in pin_vms


def test_feasible_cluster_returns_empty_blame():
    """A cluster that the solver finds feasible must produce no blame."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "basic"
                            / "simple_rebalance.yaml")
    assert explain_infeasibility(cluster, time_limit_s=5) == []


def test_empty_cluster_returns_empty_blame():
    """Degenerate input (no nodes/VMs) returns empty blame, not a crash."""
    from proxlb_solver.models import Cluster, Balancing, Constraints, Expect
    empty = Cluster(name="empty", description="",
                    balancing=Balancing(), nodes=[], vms=[],
                    constraints=Constraints(), expect=Expect())
    assert explain_infeasibility(empty) == []


# ---------------------------------------------------------------------------
# Shadow-side integration: blame must appear in the infeasible JSONL event.
# ---------------------------------------------------------------------------

# Two anti-affine VMs on a single-node cluster — only blame candidate is the
# anti-affinity rule.
_INFEASIBLE_PROXLB_DATA = {
    "meta": {
        "cluster_name": "test-cluster",
        "balancing": {
            "method": "memory", "balanciness": 3, "cpu_overcommit": 2.0,
            "max_node_inflow": 1,
        },
    },
    "nodes": {
        "node1": {
            "cpu_total": 8,
            "memory_total": 16 * 1024**3,
            "memory_used": 2 * 1024**3,
            "memory_free": 14 * 1024**3,
            "disk_free": 100 * 1024**3,
            "maintenance": False,
        },
    },
    "guests": {
        "vm-aa1": {
            "node_current": "node1", "node_target": None,
            "cpu_total": 2, "memory_total": 2 * 1024**3, "cpu_used": 0.1,
            "type": "vm", "priority": 2,
            "ha_rules": [{"rule": "anti-aff-rule-1", "type": "anti-affinity"}],
            "tags": [], "pools": [],
        },
        "vm-aa2": {
            "node_current": "node1", "node_target": None,
            "cpu_total": 2, "memory_total": 2 * 1024**3, "cpu_used": 0.1,
            "type": "vm", "priority": 2,
            "ha_rules": [{"rule": "anti-aff-rule-1", "type": "anti-affinity"}],
            "tags": [], "pools": [],
        },
    },
    "pools": {}, "ha_rules": {}, "groups": {},
}


def test_shadow_infeasible_event_includes_blame(tmp_path):
    """The infeasible JSONL event must include a non-empty 'blame' list."""
    from proxlb_solver.shadow import run_shadow

    run_shadow(_INFEASIBLE_PROXLB_DATA, {"log_dir": str(tmp_path)})

    files = [f for f in tmp_path.iterdir() if f.suffix == ".jsonl"]
    assert len(files) == 1
    events = [json.loads(line) for line in files[0].read_text().splitlines() if line]

    inf = next(e for e in events if e["event"] == "infeasible")
    assert "blame" in inf, "infeasible event must carry a 'blame' field"
    assert inf["blame"], "blame must be non-empty for a structurally infeasible cluster"
    assert any(b["type"] == "anti_affinity" for b in inf["blame"])
