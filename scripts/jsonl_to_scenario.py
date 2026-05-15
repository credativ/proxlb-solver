#!/usr/bin/env python3
"""Reconstruct a scenario YAML from a solver_run JSONL log.

The JSONL ``cluster_state`` event written by shadow.py captures the solver's
full input. This script reads that event (plus ``constraint`` events) and
emits a YAML scenario compatible with ``proxlb_solver.loader.load_scenario``.

Usage:
    python scripts/jsonl_to_scenario.py path/to/solver_run.jsonl
    python scripts/jsonl_to_scenario.py path/to/solver_run.jsonl -o scenarios/out.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

_GB = 1024 ** 3


def _bytes_to_gb(b: int) -> float | int:
    """Bytes → GB. Returns int if exact, else float rounded to 3 decimals."""
    if not b:
        return 0
    gb = b / _GB
    rounded = round(gb)
    if abs(gb - rounded) < 1e-9:
        return int(rounded)
    return round(gb, 3)


def _storage_dict_to_gb(d: dict[str, int] | None) -> dict[str, Any]:
    return {k: _bytes_to_gb(int(v)) for k, v in (d or {}).items()}


def _load_events(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    cluster_state: dict[str, Any] | None = None
    constraints: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("event")
            if t == "cluster_state":
                cluster_state = ev
            elif t == "constraint":
                constraints.append(ev)
    return cluster_state, constraints


def _build_balancing(bal: dict[str, Any]) -> dict[str, Any]:
    """Emit balancing block; omit fields equal to the model defaults."""
    out: dict[str, Any] = {
        "method":         bal.get("method", "memory"),
        "mode":           bal.get("mode", "used"),
        "balanciness":    bal.get("balanciness", 3),
        "cpu_overcommit": bal.get("cpu_overcommit", 2.0),
    }
    for k in ("memory_threshold", "cpu_threshold", "disk_threshold",
              "w_balance", "w_stickiness", "max_parallel_migrations"):
        if bal.get(k) is not None:
            out[k] = bal[k]
    # Model defaults for the keys below; only emit when overridden in the log.
    defaults = {
        "w_cpu_usage": 1, "w_cpu_psi": 2,
        "w_mem_usage": 1, "w_mem_psi": 2,
        "w_io_usage":  1, "w_io_psi":  2,
        "w_global_mem": 10, "w_global_cpu": 5, "w_global_io": 1,
        "max_node_inflow": 1,
    }
    for k, default in defaults.items():
        if k in bal and bal[k] != default:
            out[k] = bal[k]
    return out


def _build_nodes(nodes_data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, n in nodes_data.items():
        entry: dict[str, Any] = {
            "cpu_total":       int(n.get("cpu_total", 0)),
            "memory_total_gb": _bytes_to_gb(int(n.get("memory_total", 0))),
        }
        for k in ("cpu_pressure", "memory_pressure", "io_pressure"):
            v = n.get(k)
            if v:
                entry[k] = v
        if n.get("maintenance"):
            entry["maintenance"] = True

        sf = _storage_dict_to_gb(n.get("storage_free"))
        if any(v for v in sf.values()):
            entry["storage_free"] = sf

        reserve: dict[str, Any] = {}
        if n.get("cpu_reserve"):
            reserve["cpu"] = n["cpu_reserve"]
        if n.get("memory_reserve"):
            reserve["memory_gb"] = _bytes_to_gb(int(n["memory_reserve"]))
        sr = _storage_dict_to_gb(n.get("storage_reserve"))
        if any(v for v in sr.values()):
            reserve["storage_gb"] = sr
        if reserve:
            entry["reserve"] = reserve

        out[name] = entry
    return out


def _build_vms(guests_data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, g in guests_data.items():
        cpu = int(g.get("cpu", 1))
        entry: dict[str, Any] = {
            "node":      g.get("node"),
            "cpu":       cpu,
            "memory_gb": _bytes_to_gb(int(g.get("memory", 0))),
        }
        cu = g.get("cpu_usage")
        # Loader defaults cpu_usage to float(cpu); skip when value matches.
        if cu is not None and float(cu) != float(cpu):
            entry["cpu_usage"] = cu
        for k in ("cpu_pressure", "memory_pressure", "io_pressure"):
            v = g.get(k)
            if v:
                entry[k] = v
        disks = _storage_dict_to_gb(g.get("disks"))
        if any(v for v in disks.values()):
            entry["disks"] = disks
        prio = g.get("priority", 2)
        if prio != 2:
            entry["priority"] = prio
        vm_type = g.get("vm_type", "vm")
        if vm_type != "vm":
            entry["type"] = vm_type
        out[name] = entry
    return out


def _build_constraints(events: list[dict[str, Any]]) -> dict[str, Any]:
    affinity, anti_affinity, pin = [], [], []
    ignore: list[str] = []
    for ev in events:
        ct = ev.get("type")
        if ct == "affinity":
            affinity.append({
                "name":   ev.get("name"),
                "vms":    ev.get("vms", []),
                "hard":   ev.get("hard", True),
                "origin": ev.get("origin", "plb"),
            })
        elif ct == "anti_affinity":
            anti_affinity.append({
                "name":   ev.get("name"),
                "vms":    ev.get("vms", []),
                "hard":   ev.get("hard", True),
                "origin": ev.get("origin", "plb"),
            })
        elif ct == "pin":
            pin.append({"vm": ev.get("vm"), "nodes": ev.get("nodes", [])})
        elif ct == "ignore":
            ignore.append(ev.get("vm"))

    out: dict[str, Any] = {}
    if affinity:
        out["affinity"] = affinity
    if anti_affinity:
        out["anti_affinity"] = anti_affinity
    if pin:
        out["pin"] = pin
    if ignore:
        out["ignore"] = ignore
    return out


def jsonl_to_scenario(jsonl_path: Path) -> dict[str, Any]:
    cs, constraint_events = _load_events(jsonl_path)
    if cs is None:
        raise SystemExit(f"No cluster_state event found in {jsonl_path}")

    # Fall back gracefully if the log predates the enriched schema.
    bal = cs.get("balancing") or {"method": cs.get("method", "memory")}

    orig_desc = (cs.get("description") or "").strip()
    reconstr  = f"Reconstructed from {jsonl_path.name}."
    description = f"{orig_desc}\n{reconstr}" if orig_desc else reconstr

    scenario: dict[str, Any] = {
        "name":        cs.get("name") or jsonl_path.stem,
        "description": description,
        "balancing":   _build_balancing(bal),
        "nodes":       _build_nodes(cs.get("nodes") or {}),
        "vms":         _build_vms(cs.get("guests") or {}),
    }
    cs_constraints = _build_constraints(constraint_events)
    if cs_constraints:
        scenario["constraints"] = cs_constraints
    scenario["expect"] = {"feasible": True}
    return scenario


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("jsonl", type=Path, help="Path to a solver_run_*.jsonl file")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Write YAML to this path (default: stdout).")
    args = p.parse_args(argv)

    scenario = jsonl_to_scenario(args.jsonl)
    text = yaml.safe_dump(scenario, sort_keys=False, default_flow_style=False)
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
