"""Pytest configuration — collect YAML scenarios as test parameters."""

from __future__ import annotations

from pathlib import Path

import pytest

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"


def collect_scenarios() -> list[Path]:
    """Find all .yaml scenario files."""
    return sorted(SCENARIOS_DIR.rglob("*.yaml"))


def scenario_id(path: Path) -> str:
    """Generate a readable test ID from scenario path."""
    return str(path.relative_to(SCENARIOS_DIR)).replace("/", "::")


ALL_SCENARIOS = collect_scenarios()


@pytest.fixture(params=ALL_SCENARIOS, ids=[scenario_id(p) for p in ALL_SCENARIOS])
def scenario_path(request) -> Path:
    return request.param
