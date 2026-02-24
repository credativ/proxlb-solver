"""YAML scenario loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import Balancing, Cluster, Constraints, Expect, Node, VM

_GB = 1024 * 1024 * 1024
_MB = 1024 * 1024


def _gb_to_bytes(value: float) -> int:
    return int(value * _GB)


def load_scenario(path: str | Path) -> Cluster:
    """Load a YAML scenario file and return a Cluster."""
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)

    balancing_data = data.get("balancing", {})
    balanciness = balancing_data.get("balanciness", 3)
    if not (1 <= balanciness <= 5):
        raise ValueError(
            f"balanciness must be 1–5, got {balanciness}"
        )
    balancing = Balancing(
        method=balancing_data.get("method", "memory"),
        balanciness=balanciness,
        cpu_overcommit=balancing_data.get("cpu_overcommit", 2.0),
        w_balance=balancing_data.get("w_balance"),
        w_stickiness=balancing_data.get("w_stickiness"),
    )

    nodes = []
    for name, nd in data.get("nodes", {}).items():
        nodes.append(Node(
            name=name,
            cpu_total=nd["cpu_total"],
            memory_total=_gb_to_bytes(nd["memory_total_gb"]),
            maintenance=nd.get("maintenance", False),
        ))

    vms = []
    for name, vd in data.get("vms", {}).items():
        vms.append(VM(
            name=name,
            node=vd["node"],
            cpu=vd["cpu"],
            memory=_gb_to_bytes(vd["memory_gb"]),
            vm_type=vd.get("type", "vm"),
        ))

    cd = data.get("constraints", {})
    ignore_raw = cd.get("ignore", [])
    ignore_list = []
    for entry in ignore_raw:
        if isinstance(entry, dict):
            ignore_list.append(entry["vm"])
        else:
            ignore_list.append(entry)

    constraints = Constraints(
        affinity=cd.get("affinity", []),
        anti_affinity=cd.get("anti_affinity", []),
        pin=cd.get("pin", []),
        ignore=ignore_list,
    )

    ed = data.get("expect", {})
    placements_raw = ed.get("placements", {})
    placements = {k: str(v) for k, v in placements_raw.items()}

    expect = Expect(
        feasible=ed.get("feasible", True),
        constraints_satisfied=ed.get("constraints_satisfied", True),
        spread_improved=ed.get("spread_improved"),
        max_migrations=ed.get("max_migrations"),
        placements=placements,
        node_empty=ed.get("node_empty"),
    )

    evacuate_node = data.get("evacuate_node")

    # Validate references
    node_names = {n.name for n in nodes}
    for vm in vms:
        if vm.node not in node_names:
            raise ValueError(
                f"VM '{vm.name}' references unknown node '{vm.node}'"
            )
    if evacuate_node and evacuate_node not in node_names:
        raise ValueError(
            f"evacuate_node '{evacuate_node}' is not a known node"
        )

    return Cluster(
        name=data.get("name", path.stem),
        description=data.get("description", ""),
        balancing=balancing,
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=expect,
        evacuate_node=evacuate_node,
    )
