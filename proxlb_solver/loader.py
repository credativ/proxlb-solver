"""YAML scenario loader."""

from __future__ import annotations

from pathlib import Path
from collections import defaultdict

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
        mode=balancing_data.get("mode", "used"),
        balanciness=balanciness,
        cpu_overcommit=balancing_data.get("cpu_overcommit", 2.0),
        memory_threshold=balancing_data.get("memory_threshold"),
        cpu_threshold=balancing_data.get("cpu_threshold"),
        disk_threshold=balancing_data.get("disk_threshold"),
        w_balance=balancing_data.get("w_balance"),
        w_stickiness=balancing_data.get("w_stickiness"),
        w_cpu_usage=balancing_data.get("w_cpu_usage", 1),
        w_cpu_psi=balancing_data.get("w_cpu_psi", 1),
        w_mem_usage=balancing_data.get("w_mem_usage", 1),
        w_mem_psi=balancing_data.get("w_mem_psi", 1),
        w_io_usage=balancing_data.get("w_io_usage", 1),
        w_io_psi=balancing_data.get("w_io_psi", 1),
        w_global_mem=balancing_data.get("w_global_mem", 10),
        w_global_cpu=balancing_data.get("w_global_cpu", 10),
        w_global_io=balancing_data.get("w_global_io", 1),
        max_parallel_migrations=balancing_data.get("max_parallel_migrations"),
        max_node_inflow=balancing_data.get("max_node_inflow", 1),
    )

    nodes = []
    for name, nd in data.get("nodes", {}).items():
        storage_free = {
            sname: _gb_to_bytes(sval)
            for sname, sval in nd.get("storage_free", {}).items()
        }

        reserve_data = nd.get("reserve", {})
        storage_reserve = {
            sname: _gb_to_bytes(sval)
            for sname, sval in reserve_data.get("storage_gb", {}).items()
        }

        nodes.append(Node(
            name=name,
            cpu_total=nd["cpu_total"],
            memory_total=_gb_to_bytes(nd["memory_total_gb"]),
            storage_free=storage_free,
            cpu_reserve=reserve_data.get("cpu", 0),
            memory_reserve=_gb_to_bytes(reserve_data.get("memory_gb", 0)),
            storage_reserve=storage_reserve,
            cpu_pressure=nd.get("cpu_pressure", 0.0),
            memory_pressure=nd.get("memory_pressure", 0.0),
            io_pressure=nd.get("io_pressure", 0.0),
            maintenance=nd.get("maintenance", False),
        ))

    vms = []
    tag_affinity = defaultdict(list)
    tag_anti_affinity = defaultdict(list)
    tag_pin = []

    for name, vd in data.get("vms", {}).items():
        disks = {
            sname: _gb_to_bytes(sval)
            for sname, sval in vd.get("disks", {}).items()
        }
        vms.append(VM(
            name=name,
            node=vd["node"],
            cpu=vd["cpu"],
            memory=_gb_to_bytes(vd["memory_gb"]),
            cpu_usage=vd.get("cpu_usage", float(vd["cpu"])),
            cpu_pressure=vd.get("cpu_pressure", 0.0),
            memory_pressure=vd.get("memory_pressure", 0.0),
            io_pressure=vd.get("io_pressure", 0.0),
            disks=disks,
            priority=vd.get("priority", 2),
            vm_type=vd.get("type", "vm"),
        ))

        # Parse tags for implicit constraints
        for tag in vd.get("tags", []):
            if tag.startswith("plb_affinity_"):
                tag_affinity[tag].append(name)
            elif tag.startswith("plb_anti_affinity_"):
                tag_anti_affinity[tag].append(name)
            elif tag.startswith("plb_pin_"):
                target_node = tag[len("plb_pin_"):]
                tag_pin.append({"vm": name, "nodes": [target_node], "origins": [{"origin": "tag", "source": tag}]})

    cd = data.get("constraints", {})
    ignore_raw = cd.get("ignore", [])
    ignore_list = []
    for entry in ignore_raw:
        if isinstance(entry, dict):
            ignore_list.append(entry["vm"])
        else:
            ignore_list.append(entry)

    affinity = [{**r, "hard": r.get("hard", True), "origin": r.get("origin", "plb")} for r in cd.get("affinity", [])]
    for tag, t_vms in tag_affinity.items():
        if len(t_vms) > 1:
            affinity.append({"name": tag, "vms": t_vms, "hard": True, "origin": "plb"})

    anti_affinity = [{**r, "hard": r.get("hard", True), "origin": r.get("origin", "plb")} for r in cd.get("anti_affinity", [])]
    for tag, t_vms in tag_anti_affinity.items():
        if len(t_vms) > 1:
            anti_affinity.append({"name": tag, "vms": t_vms, "hard": True, "origin": "plb"})

    pin = cd.get("pin", []) + tag_pin

    constraints = Constraints(
        affinity=affinity,
        anti_affinity=anti_affinity,
        pin=pin,
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
        path_feasible=ed.get("path_feasible"),
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
