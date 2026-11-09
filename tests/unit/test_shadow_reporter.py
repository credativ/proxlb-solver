"""Tests for the shadow-mode HTML reporter."""

import json
import re
from pathlib import Path
from typing import Any

from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData

import pytest


# ---------------------------------------------------------------------------
# JSONL fixture helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    with open(path, "w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _ts() -> str:
    return "2026-02-26T14:30:00.000000+00:00"


def _optimal_run(migrations: int = 2, gap: float = 0.045) -> list[dict[str, Any]]:
    """A complete, successful run with constraints, plan steps and comparison."""
    return [
        {"event": "constraint", "ts": _ts(), "type": "affinity",
         "name": "plb_affinity_web", "origin": "plb", "vms": ["vm-1", "vm-2"], "hard": True},
        {"event": "constraint", "ts": _ts(), "type": "anti_affinity",
         "name": "ha-rule-db", "origin": "pve", "vms": ["vm-3", "vm-4"], "hard": True},
        {"event": "constraint", "ts": _ts(), "type": "pin",
         "vm": "vm-5", "nodes": ["node1"],
         "origins": [{"origin": "tag", "source": "plb_pin_node1"}]},
        {"event": "constraint", "ts": _ts(), "type": "ignore", "vm": "vm-99"},
        {"event": "solver_run", "ts": _ts(), "status": "OPTIMAL",
         "migrations": migrations, "gap": gap, "wall_time_ms": 380.0, "feasible": True},
        {"event": "plan_step", "ts": _ts(), "step": 1,
         "vm": "vm-1", "source": "node1", "target": "node2", "parallel": False},
        {"event": "plan_step", "ts": _ts(), "step": 2,
         "vm": "vm-3", "source": "node2", "target": "node3", "parallel": True},
        {"event": "plan_step", "ts": _ts(), "step": 2,
         "vm": "vm-4", "source": "node3", "target": "node1", "parallel": True},
        {"event": "compare", "ts": _ts(), "vm": "vm-1",
         "result": "agree", "target": "node2"},
        {"event": "compare", "ts": _ts(), "vm": "vm-3",
         "result": "differ", "solver_target": "node3", "proxlb_target": "node1"},
        {"event": "compare", "ts": _ts(), "vm": "vm-6",
         "result": "solver_only", "solver_target": "node2"},
        {"event": "compare", "ts": _ts(), "vm": "vm-7",
         "result": "proxlb_only", "proxlb_target": "node3"},
    ]


def _infeasible_run() -> list[dict[str, Any]]:
    return [
        {"event": "solver_run", "ts": _ts(), "status": "INFEASIBLE",
         "migrations": 0, "gap": 0.0, "wall_time_ms": 30000.0, "feasible": False},
        {"event": "infeasible", "ts": _ts(), "blocking_vms": ["vm-42"]},
    ]


def _error_run() -> list[dict[str, str]]:
    return [
        {"event": "error", "ts": _ts(),
         "message": "Something went wrong", "traceback": "Traceback (most recent call last):\n  ..."},
    ]


# ---------------------------------------------------------------------------
# generate_report API tests
# ---------------------------------------------------------------------------

class TestGenerateReport:

    def test_returns_count_of_processed_files(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        out_dir = tmp_path / "report"

        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())
        _write_jsonl(log_dir / "solver_run_20260226_023000.jsonl", _infeasible_run())

        from proxlb_solver.shadow_reporter import generate_report
        n = generate_report(log_dir, out_dir)
        assert n == 2

    def test_creates_index_html(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())

        from proxlb_solver.shadow_reporter import generate_report
        generate_report(log_dir, tmp_path / "out")

        assert (tmp_path / "out" / "index.html").exists()

    def test_creates_detail_page_per_run(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())
        _write_jsonl(log_dir / "solver_run_20260226_023000.jsonl", _infeasible_run())

        from proxlb_solver.shadow_reporter import generate_report
        generate_report(log_dir, tmp_path / "out")

        assert (tmp_path / "out" / "solver_run_20260226_143000.html").exists()
        assert (tmp_path / "out" / "solver_run_20260226_023000.html").exists()

    def test_empty_log_dir_produces_empty_index(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        out_dir = tmp_path / "out"

        from proxlb_solver.shadow_reporter import generate_report
        n = generate_report(log_dir, out_dir)
        assert n == 0
        assert (out_dir / "index.html").exists()

    def test_creates_output_dir_if_absent(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        out_dir = tmp_path / "deep" / "nested" / "out"

        from proxlb_solver.shadow_reporter import generate_report
        generate_report(log_dir, out_dir)
        assert out_dir.is_dir()


# ---------------------------------------------------------------------------
# Index page content
# ---------------------------------------------------------------------------

class TestIndexPage:

    @pytest.fixture
    def index_html(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())
        _write_jsonl(log_dir / "solver_run_20260226_023000.jsonl", _infeasible_run())
        _write_jsonl(log_dir / "solver_run_20260225_120000.jsonl", _error_run())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "index.html").read_text(encoding="utf-8")

    def test_is_valid_html(self, index_html: str) -> None:
        assert "<!DOCTYPE html>" in index_html
        assert "<html" in index_html
        assert "</html>" in index_html

    def test_has_title(self, index_html: str) -> None:
        assert "ProxLB Solver" in index_html

    def test_links_to_detail_pages(self, index_html: str) -> None:
        assert "solver_run_20260226_143000.html" in index_html
        assert "solver_run_20260226_023000.html" in index_html

    def test_shows_optimal_status(self, index_html: str) -> None:
        assert "OPTIMAL" in index_html

    def test_shows_infeasible_status(self, index_html: str) -> None:
        assert "INFEASIBLE" in index_html

    def test_no_unescaped_script_injection(self, index_html: str) -> None:
        # Any user data that might contain < > & should be escaped
        assert "<script>" not in index_html.lower().replace("</script>", "")


# ---------------------------------------------------------------------------
# Run detail page content
# ---------------------------------------------------------------------------

class TestRunDetailPage:

    @pytest.fixture
    def detail_html(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260226_143000.html").read_text(encoding="utf-8")

    def test_breadcrumb_links_to_index(self, detail_html: str) -> None:
        assert 'href="index.html"' in detail_html

    def test_shows_status(self, detail_html: str) -> None:
        assert "OPTIMAL" in detail_html

    def test_shows_constraint_section(self, detail_html: str) -> None:
        assert "Constraints" in detail_html
        assert "plb_affinity_web" in detail_html
        assert "ha-rule-db" in detail_html

    def test_shows_origin_chips(self, detail_html: str) -> None:
        assert "pve" in detail_html.lower()  # PVE HA chip
        assert "plb" in detail_html.lower()  # plb chip

    def test_shows_pin_with_source(self, detail_html: str) -> None:
        assert "plb_pin_node1" in detail_html

    def test_shows_ignored_vm(self, detail_html: str) -> None:
        assert "vm-99" in detail_html

    def test_shows_migration_plan(self, detail_html: str) -> None:
        assert "migration plan" in detail_html.lower()
        assert "node1" in detail_html
        assert "node2" in detail_html

    def test_shows_parallel_badge(self, detail_html: str) -> None:
        assert "parallel" in detail_html

    def test_shows_comparison_section(self, detail_html: str) -> None:
        assert "comparison" in detail_html.lower()
        assert "agree" in detail_html
        assert "differ" in detail_html
        assert "solver_only" in detail_html or "Solver only" in detail_html
        assert "proxlb_only" in detail_html or "ProxLB only" in detail_html

    def test_no_raw_jsonl_artifact(self, detail_html: str) -> None:
        # Should not contain raw JSON event keys leaking into output
        assert '"event":' not in detail_html


class TestInfeasibleDetailPage:

    @pytest.fixture
    def detail_html(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_023000.jsonl", _infeasible_run())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260226_023000.html").read_text(encoding="utf-8")

    def test_shows_infeasible(self, detail_html: str) -> None:
        assert "Infeasible" in detail_html

    def test_shows_blocking_vm(self, detail_html: str) -> None:
        assert "vm-42" in detail_html


class TestErrorDetailPage:

    @pytest.fixture
    def detail_html(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260225_120000.jsonl", _error_run())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260225_120000.html").read_text(encoding="utf-8")

    def test_shows_error_section(self, detail_html: str) -> None:
        assert "Error" in detail_html
        assert "Something went wrong" in detail_html

    def test_shows_traceback(self, detail_html: str) -> None:
        assert "Traceback" in detail_html


# ---------------------------------------------------------------------------
# HTML injection / escaping safety
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ProxLB actions in JSONL and in the HTML report
# ---------------------------------------------------------------------------

def _optimal_run_with_proxlb_actions(migrations: int = 2) -> list[dict[str, Any]]:
    """Run with proxlb_action events (what ProxLB planned) plus solver events."""
    base = _optimal_run(migrations=migrations)
    # Inject proxlb_action events at the front (as shadow.py emits them)
    proxlb_events = [
        {"event": "proxlb_action", "ts": _ts(),
         "vm": "vm-1", "source": "node1", "target": "node2", "type": "vm"},
        {"event": "proxlb_action", "ts": _ts(),
         "vm": "vm-3", "source": "node2", "target": "node1", "type": "ct"},
    ]
    return proxlb_events + base


def _run_with_finalize(dry_run: bool = False) -> list[dict[str, str]]:
    """Run with proxlb_action events and a proxlb_executed event at the end."""
    events = _optimal_run_with_proxlb_actions()
    events.append({"event": "proxlb_executed", "ts": _ts(), "dry_run": dry_run})
    return events


class TestProxLBActionsInJSONL:
    """Reporter correctly parses proxlb_action and proxlb_executed events."""

    @pytest.fixture
    def detail_html_with_actions(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl",
                     _run_with_finalize(dry_run=False))

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260226_143000.html").read_text(encoding="utf-8")

    @pytest.fixture
    def detail_html_dry_run(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl",
                     _run_with_finalize(dry_run=True))

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260226_143000.html").read_text(encoding="utf-8")

    def test_proxlb_plan_section_present(self, detail_html_with_actions: str) -> None:
        assert "ProxLB migration plan" in detail_html_with_actions

    def test_proxlb_plan_shows_vm_names(self, detail_html_with_actions: str) -> None:
        assert "vm-1" in detail_html_with_actions
        assert "vm-3" in detail_html_with_actions

    def test_proxlb_plan_shows_move_arrows(self, detail_html_with_actions: str) -> None:
        # The move is rendered as "source → target"
        assert "node1" in detail_html_with_actions
        assert "node2" in detail_html_with_actions

    def test_proxlb_plan_shows_type_badge(self, detail_html_with_actions: str) -> None:
        assert "ct" in detail_html_with_actions  # CT type badge

    def test_executed_badge_when_real_run(self, detail_html_with_actions: str) -> None:
        assert "executed" in detail_html_with_actions.lower()

    def test_dry_run_badge_when_dry_run(self, detail_html_dry_run: str) -> None:
        assert "dry run" in detail_html_dry_run.lower()

    def test_solver_plan_section_still_present(self, detail_html_with_actions: str) -> None:
        assert "Solver migration plan" in detail_html_with_actions

    def test_proxlb_mig_count_in_header_cards(self, detail_html_with_actions: str) -> None:
        assert "ProxLB mig." in detail_html_with_actions

    def test_index_shows_proxlb_mig_count(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl",
                     _run_with_finalize(dry_run=False))

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        index_html = (out / "index.html").read_text(encoding="utf-8")

        # Index has a "ProxLB" column header and "ProxLB migrations" summary card
        assert "ProxLB" in index_html

    def test_no_proxlb_plan_section_without_actions(self, tmp_path: Path) -> None:
        """If no proxlb_action events, no ProxLB plan section is rendered."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        html = (out / "solver_run_20260226_143000.html").read_text(encoding="utf-8")
        assert "ProxLB migration plan" not in html


class TestProxLBActionsInShadow:
    """shadow.run_shadow() emits proxlb_action events and returns run file path."""

    def _write_proxlb_data_with_migration(self, tmp_path: Path) -> ProxLbData:
        """Minimal proxlb_data where ProxLB has planned one migration."""
        import copy
        from tests.unit.test_shadow import _MINIMAL_PROXLB_DATA
        data = copy.deepcopy(_MINIMAL_PROXLB_DATA)
        data.guests["vm-100"].node_target = "node2"
        return data

    def test_run_shadow_returns_file_path(self, tmp_path: Path) -> None:
        from proxlb_solver.shadow import run_shadow
        from tests.unit.test_shadow import _MINIMAL_PROXLB_DATA
        run_file, _plan = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
        assert run_file is not None
        assert run_file.endswith(".jsonl")

    def test_run_shadow_returns_none_on_bad_dir(self) -> None:
        from proxlb_solver.shadow import run_shadow
        from tests.unit.test_shadow import _MINIMAL_PROXLB_DATA
        run_file, plan = run_shadow(_MINIMAL_PROXLB_DATA,
                                    Config.Solver(log_dir="/proc/nonexistent_proxlb/deep"))
        assert run_file is None
        assert plan is None

    def test_proxlb_action_events_emitted(self, tmp_path: Path) -> None:
        import json, os
        from proxlb_solver.shadow import run_shadow
        data = self._write_proxlb_data_with_migration(tmp_path)
        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))
        files = [f for f in os.listdir(tmp_path) if f.endswith(".jsonl")]
        events = [json.loads(l) for l in open(os.path.join(tmp_path, files[0])) if l.strip()]
        action_events = [e for e in events if e["event"] == "proxlb_action"]
        assert len(action_events) == 1
        assert action_events[0]["vm"] == "vm-100"
        assert action_events[0]["source"] == "node1"
        assert action_events[0]["target"] == "node2"

    def test_proxlb_actions_precede_constraints_and_solver(self, tmp_path: Path) -> None:
        """proxlb_action events must come before constraint and solver_run events."""
        import json, os
        from proxlb_solver.shadow import run_shadow
        data = self._write_proxlb_data_with_migration(tmp_path)
        run_shadow(data, Config.Solver(log_dir=str(tmp_path)))
        files = [f for f in os.listdir(tmp_path) if f.endswith(".jsonl")]
        events = [json.loads(l) for l in open(os.path.join(tmp_path, files[0])) if l.strip()]
        types = [e["event"] for e in events]
        last_action = max((i for i, t in enumerate(types) if t == "proxlb_action"), default=-1)
        first_solver = next((i for i, t in enumerate(types) if t == "solver_run"), len(types))
        assert last_action < first_solver

    def test_finalize_run_appends_proxlb_executed(self, tmp_path: Path) -> None:
        import json, os
        from proxlb_solver.shadow import run_shadow, finalize_run
        from tests.unit.test_shadow import _MINIMAL_PROXLB_DATA
        run_file, _plan = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
        assert run_file
        finalize_run(run_file, dry_run=False)
        events = [json.loads(l) for l in open(run_file) if l.strip()]
        executed = [e for e in events if e["event"] == "proxlb_executed"]
        assert len(executed) == 1
        assert executed[0]["dry_run"] is False

    def test_finalize_run_dry_run_flag(self, tmp_path: Path) -> None:
        import json, os
        from proxlb_solver.shadow import run_shadow, finalize_run
        from tests.unit.test_shadow import _MINIMAL_PROXLB_DATA
        run_file, _plan = run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
        assert run_file
        finalize_run(run_file, dry_run=True)
        events = [json.loads(l) for l in open(run_file) if l.strip()]
        executed = [e for e in events if e["event"] == "proxlb_executed"]
        assert executed[0]["dry_run"] is True

    def test_no_proxlb_actions_when_no_migrations_planned(self, tmp_path: Path) -> None:
        """Guests with node_target == node_current must not produce proxlb_action events."""
        import json, os
        from proxlb_solver.shadow import run_shadow
        from tests.unit.test_shadow import _MINIMAL_PROXLB_DATA
        # Both guests have node_target=None → no ProxLB migrations
        run_shadow(_MINIMAL_PROXLB_DATA, Config.Solver(log_dir=str(tmp_path)))
        files = [f for f in os.listdir(tmp_path) if f.endswith(".jsonl")]
        events = [json.loads(l) for l in open(os.path.join(tmp_path, files[0])) if l.strip()]
        assert not any(e["event"] == "proxlb_action" for e in events)


# ---------------------------------------------------------------------------
# Active mode — Mode badge + Active Execution section
# ---------------------------------------------------------------------------

def _active_run_events() -> list[dict[str, Any]]:
    """JSONL events for an active-mode run with one failed step and one retry."""
    base = _optimal_run(migrations=2)
    # Inject mode=active into the solver_run event
    base = [
        {**e, "mode": "active"} if e.get("event") == "solver_run" else e
        for e in base
    ]
    base.extend([
        # step_retry=0: initial solve pass
        {"event": "active_step_result", "ts": _ts(), "step_retry": 0,
         "step": 1, "vm": "vm-1", "expected": "node2", "actual": "node2", "success": True},
        {"event": "active_step_result", "ts": _ts(), "step_retry": 0,
         "step": 2, "vm": "vm-3", "expected": "node3", "actual": "node1", "success": False},
        # step 2 failed → re-solve triggered
        {"event": "active_step_retry", "ts": _ts(), "step": 2, "step_retry": 1,
         "pinned_vms": ["vm-3"]},
        {"event": "active_resolve",    "ts": _ts(), "step_retry": 1,
         "status": "OPTIMAL", "migrations": 1, "feasible": True},
        # step_retry=1: re-solve pass (vm-3 pinned, only vm-1-like moves remain)
        {"event": "active_step_result", "ts": _ts(), "step_retry": 1,
         "step": 1, "vm": "vm-3", "expected": "node3", "actual": "node3", "success": True},
        # summary
        {"event": "active_complete", "ts": _ts(),
         "step_retries": 1, "pinned_vms": ["vm-3"]},
    ])
    return base


class TestActiveMode:

    @pytest.fixture
    def active_detail_html(self, tmp_path: Path) -> str:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _active_run_events())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260226_143000.html").read_text(encoding="utf-8")

    @pytest.fixture
    def shadow_detail_html(self, tmp_path: Path) -> str:
        """Detail page for a shadow-mode run (no 'mode' field in solver_run)."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "solver_run_20260226_143000.jsonl", _optimal_run())

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)
        return (out / "solver_run_20260226_143000.html").read_text(encoding="utf-8")

    def test_detail_shows_active_mode_badge(self, active_detail_html: str) -> None:
        assert "ACTIVE" in active_detail_html

    def test_detail_shows_shadow_mode_badge_by_default(self, shadow_detail_html: str) -> None:
        assert "SHADOW" in shadow_detail_html

    def test_detail_shows_active_execution_section(self, active_detail_html: str) -> None:
        assert "Active Execution" in active_detail_html

    def test_detail_shows_step_results(self, active_detail_html: str) -> None:
        # vm-1 success and vm-3 failure are both rendered
        assert "vm-1" in active_detail_html
        assert "vm-3" in active_detail_html

    def test_detail_shows_retry_section(self, active_detail_html: str) -> None:
        assert "Re-solve" in active_detail_html
        # Pinned VM list
        assert "vm-3" in active_detail_html

    def test_detail_no_active_section_for_shadow_run(self, shadow_detail_html: str) -> None:
        assert "Active Execution" not in shadow_detail_html

    def test_detail_retry_shows_resolve_status_badge(self, active_detail_html: str) -> None:
        # The OPTIMAL re-solve badge appears in the retry sub-heading
        assert "OPTIMAL" in active_detail_html


class TestHtmlEscaping:

    def test_vm_names_with_special_chars_are_escaped(self, tmp_path: Path) -> None:
        """VM names containing < > & must not produce broken HTML."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        events = [
            {"event": "solver_run", "ts": _ts(), "status": "OPTIMAL",
             "migrations": 1, "gap": 0.01, "wall_time_ms": 100.0, "feasible": True},
            {"event": "plan_step", "ts": _ts(), "step": 1,
             "vm": "<script>alert(1)</script>", "source": "node1", "target": "node2",
             "parallel": False},
        ]
        _write_jsonl(log_dir / "solver_run_20260226_143001.jsonl", events)

        from proxlb_solver.shadow_reporter import generate_report
        out = tmp_path / "out"
        generate_report(log_dir, out)

        html = (out / "solver_run_20260226_143001.html").read_text()
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
