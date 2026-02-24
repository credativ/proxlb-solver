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
        w_cpu_usage=balancing_data.get("w_cpu_usage", 1),
        w_cpu_psi=balancing_data.get("w_cpu_psi", 1),
        w_mem_usage=balancing_data.get("w_mem_usage", 1),
        w_mem_psi=balancing_data.get("w_mem_psi", 1),
        w_io_usage=balancing_data.get("w_io_usage", 1),
        w_io_psi=balancing_data.get("w_io_psi", 1),
        w_global_mem=balancing_data.get("w_global_mem", 10),
        w_global_cpu=balancing_data.get("w_global_cpu", 10),
        w_global_io=balancing_data.get("w_global_io", 1),
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
