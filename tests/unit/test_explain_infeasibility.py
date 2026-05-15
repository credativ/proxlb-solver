"""Tests for solver.explain_infeasibility.

The function builds a feasibility-only CP-SAT model with assumption literals
attached to each blameable rule, then returns a minimal subset of those
rules that suffices to prove infeasibility.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from proxlb_solver.loader import load_scenario
from proxlb_solver.solver import explain_infeasibility, solve


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _types(blame: list[dict[str, Any]]) -> set[str]:
    return {b["type"] for b in blame}


def test_anti_affinity_conflict_blames_rule() -> None:
    """3 mutually anti-affine VMs on 2 nodes → the rule is the blame."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "hard_anti_affinity_conflict.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    assert blame, "expected non-empty blame"
    assert "anti_affinity" in _types(blame)
    aa = next(b for b in blame if b["type"] == "anti_affinity")
    assert aa["name"] == "Hard-AA"
    assert set(aa["vms"]) == {"vm-1", "vm-2", "vm-3"}


def test_pin_to_maintenance_blames_pin_and_maintenance() -> None:
    """VM pinned to a node in maintenance → both rules in the blame."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "pin_to_maintenance.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    types = _types(blame)
    assert "maintenance" in types
    assert "pin" in types


def test_affinity_pinning_conflict_blames_pins_and_affinity() -> None:
    """Two VMs share a hard affinity but are pinned to different nodes."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "hard_affinity_pinning_conflict.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    types = _types(blame)
    assert "affinity" in types
    # Both pin rules are independently needed to prove the affinity unsat.
    pin_vms = {b["vm"] for b in blame if b["type"] == "pin"}
    assert "vm-1" in pin_vms and "vm-2" in pin_vms


def test_feasible_cluster_returns_empty_blame() -> None:
    """A cluster that the solver finds feasible must produce no blame."""
    cluster = load_scenario(_REPO_ROOT / "scenarios" / "basic"
                            / "simple_rebalance.yaml")
    assert explain_infeasibility(cluster, time_limit_s=5) == []


def test_empty_cluster_returns_empty_blame() -> None:
    """Degenerate input (no nodes/VMs) returns empty blame, not a crash."""
    from proxlb_solver.models import Cluster, Balancing, Constraints, Expect
    empty = Cluster(name="empty", description="",
                    balancing=Balancing(), nodes=[], vms=[],
                    constraints=Constraints(), expect=Expect())
    assert explain_infeasibility(empty) == []


# ---------------------------------------------------------------------------
# Shadow-side integration: blame must appear in the infeasible JSONL event.
# ---------------------------------------------------------------------------

def test_shadow_infeasible_event_includes_blame(tmp_path: Path) -> None:
    """The infeasible JSONL event must include a non-empty 'blame' list."""
    from proxlb.utils.config_parser import Config
    from proxlb_solver.shadow import run_shadow

    from .test_shadow import _infeasible_proxlb_data

    # Two anti-affine VMs on a single-node cluster — only blame candidate is
    # the anti-affinity rule.
    run_shadow(_infeasible_proxlb_data(), Config.Solver(log_dir=str(tmp_path)))

    files = [f for f in tmp_path.iterdir() if f.suffix == ".jsonl"]
    assert len(files) == 1
    events = [json.loads(line) for line in files[0].read_text().splitlines() if line]

    inf = next(e for e in events if e["event"] == "infeasible")
    assert "blame" in inf, "infeasible event must carry a 'blame' field"
    assert inf["blame"], "blame must be non-empty for a structurally infeasible cluster"
    assert any(b["type"] == "anti_affinity" for b in inf["blame"])


# ---------------------------------------------------------------------------
# Minimality, and agreement with solve() on edge cases.
# ---------------------------------------------------------------------------

_GiB = 1024 ** 3


def _cluster(nodes: list[Any], vms: list[Any], **constraints: Any) -> Any:
    from proxlb_solver.models import Balancing, Cluster, Constraints, Expect
    return Cluster(name="t", description="", balancing=Balancing(),
                   nodes=nodes, vms=vms,
                   constraints=Constraints(**constraints), expect=Expect())


def test_blame_is_minimal_when_cpsat_returns_every_assumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CP-SAT only promises a sufficient core; the deletion pass must shrink it.

    Force the first solve to report every assumption literal as the core and
    check that only the anti-affinity rule survives.
    """
    from ortools.sat.python import cp_model

    real = cp_model.CpSolver.sufficient_assumptions_for_infeasibility
    calls = {"n": 0}

    def everything_first(self: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return list(range(100_000))
        return real(self)

    monkeypatch.setattr(cp_model.CpSolver,
                        "sufficient_assumptions_for_infeasibility", everything_first)

    cluster = load_scenario(_REPO_ROOT / "scenarios" / "infeasible"
                            / "hard_anti_affinity_conflict.yaml")
    blame = explain_infeasibility(cluster, time_limit_s=5)
    assert calls["n"] > 1, "deletion pass did not run"
    assert _types(blame) == {"anti_affinity"}


def test_reserve_above_total_leaves_node_unusable_in_both_models() -> None:
    """Memory reserve > total: the node takes no VMs, and solve() agrees."""
    from proxlb_solver.models import Node, VM
    cluster = _cluster(
        nodes=[Node(name="n1", cpu_total=4, memory_total=8 * _GiB, memory_reserve=16 * _GiB),
               Node(name="n2", cpu_total=4, memory_total=8 * _GiB)],
        vms=[VM(name="vm", node="n1", cpu=1, memory=2 * _GiB)],
    )
    sol = solve(cluster, time_limit_s=5)
    assert sol.feasible
    assert sol.placements == {"vm": "n2"}
    assert explain_infeasibility(cluster, time_limit_s=5) == []


def test_ignored_vm_on_missing_node_is_infeasible_and_blamed() -> None:
    """An ignored VM whose node is gone cannot stay put."""
    from proxlb_solver.models import Node, VM
    cluster = _cluster(
        nodes=[Node(name="n1", cpu_total=4, memory_total=8 * _GiB)],
        vms=[VM(name="vm", node="gone", cpu=1, memory=1 * _GiB)],
        ignore=["vm"],
    )
    assert not solve(cluster, time_limit_s=5).feasible
    blame = explain_infeasibility(cluster, time_limit_s=5)
    assert blame == [{"type": "ignore", "vm": "vm", "node": "gone"}]

