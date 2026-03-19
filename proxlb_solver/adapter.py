"""
Adapter to convert ProxLbData (Pydantic) into Solver models.

Memory / reservation handling
------------------------------
ProxLB stores the *already-reduced* value in node memory_total after applying
reservations.  The raw hardware total is reconstructed as:

    raw_memory = memory_used + memory_free   (both come straight from the PVE API)

By default (use_reservations=True) this adapter reconstructs the raw total and
re-applies the reservation explicitly in Node.memory_reserve so the solver's
constraints are transparent.  With use_reservations=False the reservation is
zero, letting the solver use the full hardware capacity.

Fallback: if memory_used + memory_free == 0 (e.g. synthetic test data),
memory_total is used as-is with memory_reserve=0.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .models import Balancing, Cluster, Constraints, Expect, Node, VM

if TYPE_CHECKING:
    from proxlb.utils.config_parser import Config
    from proxlb.utils.proxlb_data import ProxLbData


def from_proxlb_data(
    proxlb_data: ProxLbData,
    use_reservations: bool = True,
    pin_vms: set[str] | None = None,
) -> Cluster:
    """
    Convert ProxLbData into a Cluster ready for the solver.

    Args:
        proxlb_data:      ProxLbData from the ProxLB main loop.
        use_reservations: If True, node memory reservations are applied explicitly.
        pin_vms:          VM names to force-pin to their current node (active-mode
                          retry: VMs that failed to migrate previously).
    """
    # Lazy: only needed when from_proxlb_data is actually called.
    from proxlb.utils.config_parser import Config

    bc = proxlb_data.meta.balancing
    reserve_cfg = bc.node_resource_reserve or {}
    memory = Config.Balancing.Resource.Memory

    # Physical nodes ----------------------------------------------------------
    nodes: list[Node] = []
    for name, nd in proxlb_data.nodes.items():
        mem_used = int(nd.memory.used)
        mem_free = int(nd.memory.free)
        raw_memory = mem_used + mem_free
        reserve = 0

        if raw_memory > 0:
            if use_reservations:
                # Node-specific override wins over the 'defaults' entry.
                gb = (reserve_cfg.get(name, {}).get(memory)
                      or reserve_cfg.get("defaults", {}).get(memory, 0))
                reserve = min(int(gb * 1024 ** 3), raw_memory)
        else:
            raw_memory = int(nd.memory.total)

        nodes.append(Node(
            name=name,
            cpu_total=int(nd.cpu.total),
            memory_total=raw_memory,
            memory_reserve=reserve,
            storage_free={"local": int(nd.disk.free)},
            cpu_pressure=nd.cpu.pressure_some_percent,
            memory_pressure=nd.memory.pressure_some_percent,
            io_pressure=nd.disk.pressure_some_percent,
            maintenance=bool(nd.maintenance),
        ))

    # Guests + constraint extraction ------------------------------------------
    vms: list[VM] = []
    affinity_groups:      dict[tuple[str, str], list[str]] = defaultdict(list)
    anti_affinity_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    pin_rules:   list[dict[str, Any]] = []
    ignore_list: list[str] = []

    pools_config = bc.pools or {}

    for name, gd in proxlb_data.guests.items():
        vms.append(VM(
            name=name,
            node=gd.node_current,
            cpu=int(gd.cpu.total),
            memory=int(gd.memory.total),
            cpu_usage=float(gd.cpu.used),
            cpu_pressure=gd.cpu.pressure_some_percent,
            memory_pressure=gd.memory.pressure_some_percent,
            io_pressure=gd.disk.pressure_some_percent,
            disks={"local": int(gd.disk.used)},
            vm_type=str(gd.type),
        ))

        # A. Native Proxmox HA rules
        for rule in gd.ha_rules:
            if rule.type == "affinity":
                affinity_groups[(rule.rule, "pve")].append(name)
            elif rule.type == "anti-affinity":
                anti_affinity_groups[(rule.rule, "pve")].append(name)

        # B. ProxLB tags  (plb_affinity_*, plb_anti_affinity_*)
        for tag in gd.tags:
            if tag.startswith("plb_affinity"):
                affinity_groups[(tag, "plb")].append(name)
            elif tag.startswith("plb_anti_affinity"):
                anti_affinity_groups[(tag, "plb")].append(name)

        # C. Resource pool rules
        for pool_name in gd.pools:
            pool_cfg = pools_config.get(pool_name)
            if pool_cfg and pool_cfg.type is not None:
                if str(pool_cfg.type) == "affinity":
                    affinity_groups[(pool_name, "plb")].append(name)
                elif str(pool_cfg.type) == "anti-affinity":
                    anti_affinity_groups[(pool_name, "plb")].append(name)

        # D. Node pinning  (tags, pools, HA restricted nodes)
        if gd.node_relationships:
            origins: list[dict[str, Any]] = []
            for tag in gd.tags:
                if tag.startswith("plb_pin"):
                    origins.append({"origin": "tag", "source": tag})
            for pool in gd.pools:
                pool_cfg = pools_config.get(pool)
                if pool_cfg and pool_cfg.pin:
                    origins.append({"origin": "pool", "source": pool})
            for rule in gd.ha_rules:
                if rule.type == "affinity" and rule.nodes:
                    origins.append({"origin": "pve", "source": rule.rule})
            pin_rules.append({
                "vm":      name,
                "nodes":   gd.node_relationships,
                "origins": origins,
            })

        # E. Active-mode feedback: pin VMs that failed to migrate previously
        if pin_vms and name in pin_vms:
            pin_rules.append({
                "vm":      name,
                "nodes":   [gd.node_current],
                "origins": [{"origin": "solver", "source": "migration_failed"}],
            })

        # F. Ignore flag
        if gd.ignore:
            ignore_list.append(name)

    # Constraints + Cluster ---------------------------------------------------
    constraints = Constraints(
        affinity=[
            {"name": k[0], "origin": k[1], "vms": v, "hard": True}
            for k, v in affinity_groups.items() if len(v) > 1
        ],
        anti_affinity=[
            {"name": k[0], "origin": k[1], "vms": v, "hard": True}
            for k, v in anti_affinity_groups.items() if len(v) > 1
        ],
        pin=pin_rules,
        ignore=ignore_list,
    )

    return Cluster(
        name="Live Cluster",
        description="Auto-generated from ProxLB live data",
        balancing=Balancing(
            **bc.model_dump(include={
                "method", "mode", "balanciness",
                "memory_threshold", "cpu_threshold", "disk_threshold",
                "cpu_overcommit", "max_node_inflow",
            }),
            max_parallel_migrations=bc.parallel_jobs if bc.parallel else 1,
        ),
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=Expect(feasible=True),
    )
