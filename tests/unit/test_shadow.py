"""Unit tests for shadow mode integration."""

import json
import logging
import os
import pytest


_MINIMAL_PROXLB_DATA = {
    "meta": {
        "cluster_name": "test-cluster",
        "balancing": {
            "method": "memory",
            "balanciness": 3,
            "cpu_overcommit": 2.0,
            "max_node_inflow": 1,
        },
    },
    "nodes": {
        # memory_used + memory_free = raw hardware total (16 GB).
        # memory_total is the pre-reduced value ProxLB stores after applying
        # node_resource_reserve (here: 16 GB - 4 GB default = 12 GB).
        "node1": {
            "cpu_total": 8,
            "memory_total": 12 * 1024**3,   # already reservation-reduced
            "memory_used":   2 * 1024**3,   # raw, from PVE API
            "memory_free":  14 * 1024**3,   # raw, from PVE API  →  total = 16 GB
            "disk_free": 100 * 1024**3,
            "maintenance": False,
        },
        "node2": {
            "cpu_total": 8,
            "memory_total": 12 * 1024**3,
            "memory_used":   3 * 1024**3,
            "memory_free":  13 * 1024**3,
            "disk_free": 100 * 1024**3,
            "maintenance": False,
        },
    },
    "guests": {
        "vm-100": {
            "node_current": "node1",
            "node_target": None,
            "cpu_total": 2,
            "memory_total": 2 * 1024**3,
            "cpu_used": 0.1,
            "type": "vm",
            "priority": 2,
            "ha_rules": [],
            "tags": [],
            "pools": [],
        },
        "vm-101": {
            "node_current": "node2",
            "node_target": None,
            "cpu_total": 2,
            "memory_total": 4 * 1024**3,
            "cpu_used": 0.2,
            "type": "vm",
            "priority": 2,
            "ha_rules": [],
            "tags": [],
            "pools": [],
        },
    },
    "pools": {},
    "ha_rules": {},
    "groups": {},
}


def _read_jsonl(path: str) -> list[dict]:
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

def test_run_shadow_creates_jsonl_with_solver_run_event(tmp_path, caplog):
    """run_shadow must create a JSONL file containing a 'solver_run' event."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = {"enable": True, "log_dir": str(tmp_path)}

    with caplog.at_level(logging.INFO, logger="proxlb"):
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


def test_run_shadow_emits_single_summary_to_main_log(tmp_path, caplog):
    """The main ProxLB log must receive exactly one [solver] summary line."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = {"enable": True, "log_dir": str(tmp_path)}

    with caplog.at_level(logging.INFO, logger="proxlb"):
        run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    solver_lines = [r.message for r in caplog.records if r.message.startswith("[solver] run=")]
    assert len(solver_lines) == 1
    assert "status=" in solver_lines[0]
    assert "migrations=" in solver_lines[0]


def test_run_shadow_balanciness_and_method_override(tmp_path):
    """Passing balanciness and method overrides must not raise."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = {"log_dir": str(tmp_path), "balanciness": 5, "method": "cpu"}
    run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    events = _read_jsonl(_run_file(str(tmp_path)))
    assert any(e["event"] == "solver_run" for e in events)


def test_run_shadow_timeout_seconds(tmp_path):
    """timeout_seconds config option must be accepted without error."""
    from proxlb_solver.shadow import run_shadow

    solver_cfg = {"log_dir": str(tmp_path), "timeout_seconds": 5}
    run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    events = _read_jsonl(_run_file(str(tmp_path)))
    assert any(e["event"] == "solver_run" for e in events)


def test_run_shadow_compare_events_with_proxlb_migration(tmp_path):
    """When ProxLB has a planned migration, compare events must appear in the JSONL."""
    from proxlb_solver.shadow import run_shadow

    data = {
        **_MINIMAL_PROXLB_DATA,
        "guests": {
            **_MINIMAL_PROXLB_DATA["guests"],
            "vm-100": {
                **_MINIMAL_PROXLB_DATA["guests"]["vm-100"],
                "node_target": "node2",  # ProxLB wants to move vm-100 to node2
            },
        },
    }

    run_shadow(data, {"log_dir": str(tmp_path)})

    events = _read_jsonl(_run_file(str(tmp_path)))
    compare_events = [e for e in events if e["event"] == "compare"]

    # There should be at least one compare event for vm-100
    assert any(e["vm"] == "vm-100" for e in compare_events), \
        f"Expected compare event for vm-100, got: {compare_events}"

    # Every compare event must have a valid result type
    valid_results = {"agree", "differ", "solver_only", "proxlb_only"}
    for ev in compare_events:
        assert ev["result"] in valid_results, f"Unexpected result: {ev}"


def test_run_shadow_invalid_log_dir_logs_warning(caplog):
    """If log_dir cannot be created, a warning is logged and no exception raised."""
    from proxlb_solver.shadow import run_shadow

    invalid_dir = "/proc/nonexistent_proxlb_solver_dir/deep/path"
    solver_cfg = {"log_dir": invalid_dir}

    with caplog.at_level(logging.WARNING, logger="proxlb"):
        run_shadow(_MINIMAL_PROXLB_DATA, solver_cfg)

    warning_lines = [r.message for r in caplog.records if "cannot create log_dir" in r.message]
    assert warning_lines


def test_run_shadow_jsonl_all_lines_valid_json(tmp_path):
    """Every line in the JSONL run file must be valid JSON with a 'ts' field."""
    from proxlb_solver.shadow import run_shadow

    run_shadow(_MINIMAL_PROXLB_DATA, {"log_dir": str(tmp_path)})

    run_file = _run_file(str(tmp_path))
    with open(run_file) as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)  # raises if invalid JSON
            assert "ts" in obj, f"Line {i} missing 'ts': {line}"
            assert "event" in obj, f"Line {i} missing 'event': {line}"
