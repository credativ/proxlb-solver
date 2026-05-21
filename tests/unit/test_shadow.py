"""Unit tests for shadow mode integration."""

import copy
import json
import logging
import os
import sys
import types
import pytest

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from unittest.mock import patch as mock_patch

from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData

from proxlb_solver.models import Migration, MigrationPlan, MigrationStep, Solution, SolverStats

from ..utils import MINIMAL_DATA, create_guest, create_node, create_node_metric

_GB = 1024 ** 3


def _minimal_proxlb_data() -> ProxLbData:
    """Build a feasible two-node, two-VM ProxLbData fixture."""
    proxlb_data = MINIMAL_DATA.model_copy(deep=True)
    proxlb_data.meta.balancing.method = Config.Balancing.Resource.Memory
    proxlb_data.meta.balancing.balanciness = 3

    proxlb_data.nodes = {
        # memory_used + memory_free = raw hardware total (16 GB).
        # memory_total is the pre-reduced value ProxLB stores after applying
        # node_resource_reserve (here: 16 GB - 4 GB default = 12 GB).
        "node1": create_node(
            name="node1",
            cpu=create_node_metric(total=8),
            disk=create_node_metric(free=100 * _GB),
            memory=create_node_metric(
                total=12 * _GB,
                used=2 * _GB,
                free=14 * _GB,
            ),
        ),
        "node2": create_node(
            name="node2",
            cpu=create_node_metric(total=8),
            disk=create_node_metric(free=100 * _GB),
            memory=create_node_metric(
                total=12 * _GB,  # already reservation-reduced
                used=3 * _GB,    # raw, from PVE API
                free=13 * _GB,   # raw, from PVE API  →  total = 16 GB
            ),
        ),
    }

    vm100 = create_guest("vm-100", node_current="node1", node_target="node1")
    vm100.cpu.total = 2
    vm100.cpu.used = 0.1
    vm100.memory.total = 2 * _GB

    vm101 = create_guest("vm-101", node_current="node2", node_target="node2")
    vm101.cpu.total = 2
    vm101.cpu.used = 0.2
    vm101.memory.total = 4 * _GB

    proxlb_data.guests = {"vm-100": vm100, "vm-101": vm101}
    return proxlb_data


def _infeasible_proxlb_data() -> ProxLbData:
    """Single-node cluster with two anti-affinity VMs — solver cannot satisfy."""
    proxlb_data = MINIMAL_DATA.model_copy(deep=True)
    proxlb_data.meta.balancing.method = Config.Balancing.Resource.Memory
    proxlb_data.meta.balancing.balanciness = 3

    proxlb_data.nodes = {
        "node1": create_node(
            name="node1",
            cpu=create_node_metric(total=8),
            disk=create_node_metric(free=100 * _GB),
            memory=create_node_metric(
                total=16 * _GB,
                used=2 * _GB,
                free=14 * _GB,
            ),
        ),
    }

    anti_aff = ProxLbData.HaRule(
        rule="anti-aff-rule-1",
        type=Config.AffinityType.NegativeAffinity,
        nodes=[],
        members=[],
    )

    def _aa_vm(name: str) -> ProxLbData.Guest:
        g = create_guest(name, node_current="node1", node_target="node1")
        g.cpu.total = 2
        g.cpu.used = 0.1
        g.memory.total = 2 * _GB
        g.ha_rules = [anti_aff]
        return g

    proxlb_data.guests = {
        "vm-aa1": _aa_vm("vm-aa1"),
        "vm-aa2": _aa_vm("vm-aa2"),
    }
    return proxlb_data


_MINIMAL_PROXLB_DATA: ProxLbData = _minimal_proxlb_data()
_INFEASIBLE_PROXLB_DATA: ProxLbData = _infeasible_proxlb_data()


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _run_file(log_dir: str) -> str:
    """Return the single JSONL file created in log_dir."""
    files = [f for f in os.listdir(log_dir) if f.endswith(".jsonl")]
    assert len(files) == 1, f"Expected 1 JSONL run file, found: {files}"
    return os.path.join(log_dir, files[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_shadow_creates_jsonl_with_solver_run_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """run_shadow must create a JSONL file containing a 'solver_run' event."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = Config.Solver(enable=True, log_dir=str(tmp_path))

    with caplog.at_level(logging.INFO, logger="ProxLB"):
        run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    run_file = _run_file(str(tmp_path))
    events = _read_jsonl(run_file)

    event_types = {e["event"] for e in events}
    assert "solver_run" in event_types

    solver_run = next(e for e in events if e["event"] == "solver_run")
    assert "status" in solver_run
    assert "migrations" in solver_run
    assert "gap" in solver_run
    assert "wall_time_ms" in solver_run
    assert "feasible" in solver_run
    assert "ts" in solver_run


def test_run_shadow_emits_single_summary_to_main_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The main ProxLB log must receive exactly one [solver] summary line."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = Config.Solver(enable=True, log_dir=str(tmp_path))

    with caplog.at_level(logging.INFO, logger="ProxLB"):
        run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    solver_lines = [r.message for r in caplog.records if r.message.startswith("[solver] run=")]
    assert len(solver_lines) == 1
    assert "status=" in solver_lines[0]
    assert "migrations=" in solver_lines[0]


def test_run_shadow_balanciness_and_method_override(tmp_path: Path) -> None:
    """Passing balanciness and method overrides must not raise."""
    from proxlb_solver.shadow import run_shadow

    data = _MINIMAL_PROXLB_DATA.model_copy(deep=True)
    data.meta.balancing.balanciness = 5
    data.meta.balancing.method = Config.Balancing.Resource.Cpu
    run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

    events = _read_jsonl(_run_file(str(tmp_path)))
    assert any(e["event"] == "solver_run" for e in events)


def test_run_shadow_timeout_seconds(tmp_path: Path) -> None:
    """timeout_seconds config option must be accepted without error."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = Config.Solver(log_dir=str(tmp_path), timeout_seconds=5)
    run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    events = _read_jsonl(_run_file(str(tmp_path)))
    assert any(e["event"] == "solver_run" for e in events)


def test_run_shadow_compare_events_with_proxlb_migration(tmp_path: Path) -> None:
    """When ProxLB has a planned migration, compare events must appear in the JSONL."""
    from proxlb_solver.shadow import run_shadow

    data = _MINIMAL_PROXLB_DATA.model_copy(deep=True)
    data.guests["vm-100"].node_target = "node2"  # ProxLB wants to move vm-100 to node2

    run_shadow(data, Config.Solver(log_dir=str(tmp_path)))

    events = _read_jsonl(_run_file(str(tmp_path)))
    compare_events = [e for e in events if e["event"] == "compare"]

    # There should be at least one compare event for vm-100
    assert any(e["vm"] == "vm-100" for e in compare_events), \
        f"Expected compare event for vm-100, got: {compare_events}"

    # Every compare event must have a valid result type
    valid_results = {"agree", "differ", "solver_only", "proxlb_only"}
    for ev in compare_events:
        assert ev["result"] in valid_results, f"Unexpected result: {ev}"


def test_run_shadow_invalid_log_dir_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """If log_dir cannot be created, a warning is logged and no exception raised."""
    from proxlb_solver.shadow import run_shadow

    invalid_dir = "/proc/nonexistent_proxlb_solver_dir/deep/path"
    solver_cfg = Config.Solver(log_dir=invalid_dir)

    with caplog.at_level(logging.WARNING, logger="ProxLB"):
        run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    warning_lines = [r.message for r in caplog.records if "cannot create log_dir" in r.message]
    assert warning_lines


def test_run_shadow_jsonl_all_lines_valid_json(tmp_path: Path) -> None:
    """Every line in the JSONL run file must be valid JSON with a 'ts' field."""
    from proxlb_solver.shadow import run_shadow

    run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))

    run_file = _run_file(str(tmp_path))
    with open(run_file) as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)  # raises if invalid JSON
            assert "ts" in obj, f"Line {i} missing 'ts': {line}"
            assert "event" in obj, f"Line {i} missing 'event': {line}"


# ---------------------------------------------------------------------------
# Tuple return + active mode tests
# ---------------------------------------------------------------------------

def test_run_shadow_returns_tuple(tmp_path: Path) -> None:
    """run_shadow() must return a 2-tuple (run_file, plan)."""
    from proxlb_solver.shadow import run_shadow

    result = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 2, f"Expected 2-tuple, got length {len(result)}"


def test_run_shadow_returns_none_tuple_on_bad_dir(caplog: pytest.LogCaptureFixture) -> None:
    """run_shadow() must return (None, None) when log_dir cannot be created."""
    from proxlb_solver.shadow import run_shadow

    result = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir="/proc/nonexistent_proxlb/deep"))
    assert result == (None, None)


def test_run_shadow_plan_is_not_none_on_success(tmp_path: Path) -> None:
    """For a feasible cluster the second element of the tuple must be a plan."""
    from proxlb_solver.shadow import run_shadow

    run_file, plan = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
    assert run_file is not None
    assert isinstance(plan, MigrationPlan)


def test_run_shadow_plan_is_none_when_infeasible(tmp_path: Path) -> None:
    """For an infeasible cluster the plan element must be None."""
    from proxlb_solver.shadow import run_shadow

    _run_file, plan = run_shadow(_INFEASIBLE_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
    assert plan is None


def test_solver_run_event_has_mode_field(tmp_path: Path) -> None:
    """The solver_run JSONL event must include a 'mode' field."""
    from proxlb_solver.shadow import run_shadow

    run_file, _ = run_shadow(
        _MINIMAL_PROXLB_DATA,
        Config.Solver(log_dir=str(tmp_path), mode=Config.Solver.Mode.Active),
    )
    assert run_file
    events = _read_jsonl(run_file)
    sr = next(e for e in events if e["event"] == "solver_run")
    assert sr.get("mode") == "active"


def test_solver_run_event_mode_defaults_to_shadow(tmp_path: Path) -> None:
    """When 'mode' is absent from solver_cfg the field defaults to 'shadow'."""
    from proxlb_solver.shadow import run_shadow

    run_file, _ = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
    assert run_file
    events = _read_jsonl(run_file)
    sr = next(e for e in events if e["event"] == "solver_run")
    assert sr.get("mode") == "shadow"


# ---------------------------------------------------------------------------
# execute_solver_plan — feedback loop
# ---------------------------------------------------------------------------

def _make_mock_proxlb_modules(balancing_cls: Any = None) -> tuple[Any, dict[str, Any]]:
    """Return (mock_balancing_cls, sys_modules_patch_dict)."""
    if balancing_cls is None:
        balancing_cls = MagicMock()
    mock_mod = types.SimpleNamespace(Balancing=balancing_cls)
    patch_dict = {
        "proxlb":                  MagicMock(),
        "proxlb.models":           MagicMock(),
        "proxlb.models.balancing": mock_mod,
    }
    return balancing_cls, patch_dict


def _make_one_step_plan(vm: str = "vm-100", source: str = "node1", target: str = "node2") -> MigrationPlan:
    return MigrationPlan(
        steps=[MigrationStep(
            step=1,
            migrations=[Migration(vm=vm, source=source, target=target)],
            parallel=False,
        )],
        dependency_edges=[],
        temp_moves=[],
    )


def test_execute_solver_plan_single_step_success(tmp_path: Path) -> None:
    """Single step where API confirms success → Balancing called once, no retry."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node2"},
        {"name": "vm-101", "node": "node2"},
    ]

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=3, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        from proxlb_solver.shadow import execute_solver_plan
        execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    # Balancing.balance() called exactly once (for the step; no remainder that needs moving)
    assert mock_balancing.balance.call_count == 1
    # node_current updated
    assert data.guests["vm-100"].node_current == "node2"

    events = _read_jsonl(run_file)
    step_results = [e for e in events if e["event"] == "active_step_result"]
    assert len(step_results) == 1
    assert step_results[0]["success"] is True
    assert step_results[0]["vm"] == "vm-100"


def test_execute_solver_plan_failure_triggers_retry(tmp_path: Path) -> None:
    """Failed migration is detected and active_retry event is written."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    # vm-100 stays on node1 (migration failed)
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node1"},
        {"name": "vm-101", "node": "node2"},
    ]

    # Re-solve returns an empty plan (vm-100 pinned to node1, no moves needed)
    empty_plan = MigrationPlan(steps=[], dependency_edges=[], temp_moves=[])
    mock_solution = Solution(
        feasible=True,
        placements={"vm-100": "node1", "vm-101": "node2"},
        migrations=[],
        stats=SolverStats(status="OPTIMAL", objective=0, load_gap=0.0,
                          migration_count=0, wall_time_ms=1.0),
    )

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=1, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        with mock_patch("proxlb_solver.solver.solve", return_value=mock_solution):
            with mock_patch("proxlb_solver.planner.plan_migrations", return_value=empty_plan):
                from proxlb_solver.shadow import execute_solver_plan
                execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    events = _read_jsonl(run_file)
    step_results  = [e for e in events if e["event"] == "active_step_result"]
    retry_events  = [e for e in events if e["event"] == "active_step_retry"]

    assert any(e["vm"] == "vm-100" and e["success"] is False for e in step_results)
    assert len(retry_events) == 1
    assert "vm-100" in retry_events[0]["pinned_vms"]


def test_execute_solver_plan_respects_max_retries(tmp_path: Path) -> None:
    """Loop runs at most max_retries+1 times even if migration keeps failing."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    # Always reports failure
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node1"},
    ]

    mock_solution = Solution(
        feasible=True,
        placements={"vm-100": "node2"},
        migrations=[],
        stats=SolverStats(status="OPTIMAL", objective=0, load_gap=0.0,
                          migration_count=0, wall_time_ms=1.0),
    )
    empty_plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    max_retries = 2
    solver_cfg = Config.Solver(active_step_retries=max_retries, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        with mock_patch("proxlb_solver.solver.solve", return_value=mock_solution):
            with mock_patch("proxlb_solver.planner.plan_migrations", return_value=empty_plan):
                from proxlb_solver.shadow import execute_solver_plan
                execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    events = _read_jsonl(run_file)
    retry_events = [e for e in events if e["event"] == "active_step_retry"]
    # Retries 1..max_retries → max_retries retry events
    assert len(retry_events) == max_retries


def test_execute_solver_plan_fallback_on_infeasible_resolve(tmp_path: Path) -> None:
    """When re-solve is infeasible the loop exits and active_resolve is written."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node1"},  # migration failed
    ]

    infeasible_solution = Solution(
        feasible=False,
        placements={},
        migrations=[],
        stats=SolverStats(status="INFEASIBLE", objective=0, load_gap=0.0,
                          migration_count=0, wall_time_ms=100.0),
        blocking_vms=["vm-100"],
    )

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=1, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        with mock_patch("proxlb_solver.solver.solve", return_value=infeasible_solution):
            from proxlb_solver.shadow import execute_solver_plan
            execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    events = _read_jsonl(run_file)
    resolve_events = [e for e in events if e["event"] == "active_resolve"]
    assert len(resolve_events) == 1
    assert resolve_events[0]["feasible"] is False


def test_execute_single_step_updates_node_current(tmp_path: Path) -> None:
    """After a successful step node_current must be updated to the target."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    step = MigrationStep(
        step=1,
        migrations=[Migration(vm="vm-100", source="node1", target="node2")],
        parallel=False,
    )

    mock_api = MagicMock()
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node2"},  # success
    ]

    mock_balancing, patch_dict = _make_mock_proxlb_modules()

    with patch.dict(sys.modules, patch_dict):
        from proxlb_solver.shadow import _execute_single_step
        failed = _execute_single_step(
            mock_api, data, step, None, step_retry=0
        )

    assert not failed
    assert data.guests["vm-100"].node_current == "node2"


def test_execute_solver_plan_aborts_step_on_verify_failure(tmp_path: Path) -> None:
    """If step 1 fails verification, step 2 must not be executed in the same pass."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = MigrationPlan(
        steps=[
            MigrationStep(step=1, migrations=[Migration(vm="vm-100", source="node1", target="node2")], parallel=False),
            MigrationStep(step=2, migrations=[Migration(vm="vm-101", source="node2", target="node1")], parallel=False),
        ],
        dependency_edges=[],
        temp_moves=[],
    )

    mock_api = MagicMock()
    # vm-100 fails; vm-101 never queried in this pass
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node1"},
        {"name": "vm-101", "node": "node2"},
    ]

    # Re-solve: infeasible → loop exits immediately, no step 2 attempt
    infeasible_sol = Solution(
        feasible=False,
        placements={},
        migrations=[],
        stats=SolverStats(status="INFEASIBLE", objective=0, load_gap=0.0,
                          migration_count=0, wall_time_ms=1.0),
    )

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=1, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        with mock_patch("proxlb_solver.solver.solve", return_value=infeasible_sol):
            from proxlb_solver.shadow import execute_solver_plan
            execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    # Only step 1 (vm-100) should have produced a step_result event;
    # step 2 (vm-101) was skipped because step 1 failed.
    events = _read_jsonl(run_file)
    step_results = [e for e in events if e["event"] == "active_step_result"]
    assert not any(e["vm"] == "vm-101" for e in step_results), (
        "vm-101 (step 2) must not have an active_step_result — step 2 was skipped"
    )
    assert any(e["vm"] == "vm-100" for e in step_results)


def test_execute_single_step_balancing_exception_marks_all_vms_failed(tmp_path: Path) -> None:
    """If Balancing() raises, all VMs in that step are marked failed."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    step = MigrationStep(
        step=1,
        migrations=[
            Migration(vm="vm-100", source="node1", target="node2"),
            Migration(vm="vm-101", source="node2", target="node1"),
        ],
        parallel=True,
    )

    mock_api = MagicMock()
    mock_balancing = MagicMock()
    mock_balancing.balance.side_effect = RuntimeError("VM is locked")
    mock_mod = types.SimpleNamespace(Balancing=mock_balancing)
    patch_dict = {
        "proxlb": MagicMock(),
        "proxlb.models": MagicMock(),
        "proxlb.models.balancing": mock_mod,
    }

    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    with patch.dict(sys.modules, patch_dict):
        from proxlb_solver.shadow import _execute_single_step
        failed = _execute_single_step(mock_api, data, step, run_file, step_retry=0)

    assert "vm-100" in failed
    assert "vm-101" in failed
    # No verify call — exception aborted before that
    mock_api.cluster.resources.get.assert_not_called()

    events = _read_jsonl(run_file)
    step_results = [e for e in events if e["event"] == "active_step_result"]
    assert len(step_results) == 2
    assert all(e["success"] is False for e in step_results)
    assert all("error" in e for e in step_results)
    assert all("VM is locked" in e["error"] for e in step_results)


def test_execute_solver_plan_balancing_exception_skips_subsequent_steps(tmp_path: Path) -> None:
    """If Balancing() raises on step 1, step 2 must never run in the same pass."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = MigrationPlan(
        steps=[
            MigrationStep(step=1, migrations=[Migration(vm="vm-100", source="node1", target="node2")], parallel=False),
            MigrationStep(step=2, migrations=[Migration(vm="vm-101", source="node2", target="node1")], parallel=False),
        ],
        dependency_edges=[],
        temp_moves=[],
    )

    mock_api = MagicMock()
    mock_balancing = MagicMock()
    mock_balancing.balance.side_effect = RuntimeError("timeout")
    mock_mod = types.SimpleNamespace(Balancing=mock_balancing)
    patch_dict = {
        "proxlb": MagicMock(),
        "proxlb.models": MagicMock(),
        "proxlb.models.balancing": mock_mod,
    }

    # Re-solve infeasible → exits loop
    infeasible_sol = Solution(
        feasible=False,
        placements={},
        migrations=[],
        stats=SolverStats(status="INFEASIBLE", objective=0, load_gap=0.0,
                          migration_count=0, wall_time_ms=1.0),
    )

    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=1, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        with mock_patch("proxlb_solver.solver.solve", return_value=infeasible_sol):
            from proxlb_solver.shadow import execute_solver_plan
            execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    # Step 2 (vm-101) must never have produced an active_step_result event —
    # Balancing raised on step 1, so no migration was attempted for step 2.
    events = _read_jsonl(run_file)
    step_results = [e for e in events if e["event"] == "active_step_result"]
    assert not any(e["vm"] == "vm-101" for e in step_results), (
        "vm-101 (step 2) must not have an active_step_result"
    )
    # vm-100 had the Balancing exception → marked failed
    vm100_results = [e for e in step_results if e["vm"] == "vm-100"]
    assert vm100_results and vm100_results[0]["success"] is False


def test_execute_solver_plan_emits_active_complete(tmp_path: Path) -> None:
    """execute_solver_plan must append an active_complete summary event."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node2"},  # success
    ]

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=3, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        from proxlb_solver.shadow import execute_solver_plan
        execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    events = _read_jsonl(run_file)
    complete_events = [e for e in events if e["event"] == "active_complete"]
    assert len(complete_events) == 1
    ev = complete_events[0]
    assert "step_retries" in ev
    assert "pinned_vms" in ev
    assert ev["step_retries"] == 0      # no retries needed
    assert ev["pinned_vms"] == []       # nothing was pinned


def test_execute_solver_plan_active_complete_records_pinned_vms(tmp_path: Path) -> None:
    """active_complete must list VMs that were permanently pinned after failures."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    # vm-100 always stays on node1 (persistent failure)
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node1"},
    ]

    infeasible_sol = Solution(
        feasible=False,
        placements={},
        migrations=[],
        stats=SolverStats(status="INFEASIBLE", objective=0, load_gap=0.0,
                          migration_count=0, wall_time_ms=1.0),
    )

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=1, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        with mock_patch("proxlb_solver.solver.solve", return_value=infeasible_sol):
            from proxlb_solver.shadow import execute_solver_plan
            execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    events = _read_jsonl(run_file)
    complete = next(e for e in events if e["event"] == "active_complete")
    assert "vm-100" in complete["pinned_vms"]
    assert complete["step_retries"] >= 1


def test_active_step_result_has_step_retry_field(tmp_path: Path) -> None:
    """active_step_result events must carry a 'step_retry' field."""
    data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
    plan = _make_one_step_plan("vm-100", "node1", "node2")

    mock_api = MagicMock()
    mock_api.cluster.resources.get.return_value = [
        {"name": "vm-100", "node": "node2"},
    ]

    mock_balancing, patch_dict = _make_mock_proxlb_modules()
    run_file = str(tmp_path / "run.jsonl")
    open(run_file, "w").close()

    solver_cfg = Config.Solver(active_step_retries=3, use_reservations=True, timeout_seconds=5)

    with patch.dict(sys.modules, patch_dict):
        from proxlb_solver.shadow import execute_solver_plan
        execute_solver_plan(mock_api, data, plan, solver_cfg, run_file)

    events = _read_jsonl(run_file)
    step_results = [e for e in events if e["event"] == "active_step_result"]
    assert len(step_results) == 1
    assert "step_retry" in step_results[0]
    assert step_results[0]["step_retry"] == 0
