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
    """Bytes → GB, exact.

    Returns an int for whole GB. Otherwise the float is not rounded: dividing
    by 2**30 is exact in binary floating point, so the loader's
    ``int(value * 2**30)`` gets the original byte count back. Rounding would
    shift capacities and can flip a tight scenario between feasible and
    infeasible.
    """
    if not b:
        return 0
    if b % _GB == 0:
        return b // _GB
    return b / _GB


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


# Keys whose loader default is None: omitting a None value is lossless.
_OPTIONAL_KEYS = ("memory_threshold", "cpu_threshold", "disk_threshold",
                  "w_balance", "w_stickiness", "max_parallel_migrations")
# Keys written whenever the log has them. The loader's defaults for several
# of these differ from the Balancing model's (e.g. w_cpu_psi 1 vs 2), so
# leaving one out would change the objective of the reconstructed scenario.
_EXPLICIT_KEYS = ("w_cpu_usage", "w_cpu_psi", "w_mem_usage", "w_mem_psi",
                  "w_io_usage", "w_io_psi",
                  "w_global_mem", "w_global_cpu", "w_global_io",
                  "max_node_inflow")


def _build_balancing(bal: dict[str, Any]) -> dict[str, Any]:
    """Emit the balancing block so the loader rebuilds the logged values."""
    out: dict[str, Any] = {
        "method":         bal.get("method", "memory"),
        "mode":           bal.get("mode", "used"),
        "balanciness":    bal.get("balanciness", 3),
        "cpu_overcommit": bal.get("cpu_overcommit", 2.0),
    }
    for k in _OPTIONAL_KEYS:
        if bal.get(k) is not None:
            out[k] = bal[k]
    for k in _EXPLICIT_KEYS:
        if k in bal:
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
            ignore.append(ev["vm"])

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


def _add_missing_nodes(nodes: dict[str, Any], vms: dict[str, Any]) -> list[str]:
    """Add a placeholder for every node a VM sits on that the log lacks.

    The solver accepts a VM whose node is gone (it must migrate), but the
    loader rejects unknown node references. A node in maintenance with no
    capacity is equivalent for the solver: nothing can be placed there, so
    the VM must move, and maintenance nodes are left out of the load gap.
    """
    missing = sorted({v["node"] for v in vms.values() if v["node"] not in nodes})
    for name in missing:
        nodes[name] = {"cpu_total": 0, "memory_total_gb": 0, "maintenance": True}
    return missing


def jsonl_to_scenario(jsonl_path: Path) -> dict[str, Any]:
    cs, constraint_events = _load_events(jsonl_path)
    if cs is None:
        raise SystemExit(f"No cluster_state event found in {jsonl_path}")
    if cs.get("partial"):
        raise SystemExit(
            f"The cluster_state in {jsonl_path} is partial: building the "
            "cluster from ProxLB data failed in that run (see its 'error' "
            "event), so the solver input was never known.")

    # Fall back gracefully if the log predates the enriched schema.
    bal = cs.get("balancing") or {"method": cs.get("method", "memory")}

    nodes = _build_nodes(cs.get("nodes") or {})
    vms = _build_vms(cs.get("guests") or {})
    missing = _add_missing_nodes(nodes, vms)

    orig_desc = (cs.get("description") or "").strip()
    reconstr  = f"Reconstructed from {jsonl_path.name}."
    if missing:
        reconstr += (" Nodes not in the log were added as empty maintenance"
                     f" placeholders: {', '.join(missing)}.")
        print(f"warning: VMs reference nodes missing from the log; added "
              f"maintenance placeholders: {', '.join(missing)}", file=sys.stderr)
    description = f"{orig_desc}\n{reconstr}" if orig_desc else reconstr

    scenario: dict[str, Any] = {
        "name":        cs.get("name") or jsonl_path.stem,
        "description": description,
        "balancing":   _build_balancing(bal),
        "nodes":       nodes,
        "vms":         vms,
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
