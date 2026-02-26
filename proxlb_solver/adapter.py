"""
Adapter to convert ProxLB internal data structures to Solver models.

This module provides the 'bridge' from the existing ProxLB code base
to the new CP-SAT solver. It translates the nested dictionary structure
(proxlb_data) into the strictly typed Cluster, Node, and VM objects.

Memory / reservation handling
------------------------------
ProxLB calls set_node_resource_reservation() and stores the *already-reduced*
value in proxlb_data["nodes"][name]["memory_total"].  The raw hardware total is
not stored explicitly, but it can be reconstructed as:

    raw_memory = memory_used + memory_free          (both come straight from the PVE API)

By default (use_reservations=True) this adapter reconstructs the raw total and
re-applies the reservation itself — storing it explicitly in Node.memory_reserve
so the solver's constraints are transparent.  With use_reservations=False the
reservation is set to zero, letting the solver use the full hardware capacity.

Fallback: if memory_used / memory_free are absent (e.g. synthetic test data),
memory_total is used as-is with memory_reserve=0.  This preserves backward
compatibility but means the pre-baked reservation cannot be separated out.
"""

from __future__ import annotations
from typing import Dict, Any
from collections import defaultdict
from .models import Cluster, Node, VM, Constraints, Balancing, Expect


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_memory_reserve_bytes(node_name: str, reserve_cfg: dict) -> int:
    """Return the configured memory reservation for *node_name* in bytes.

    Resolution order: node-specific → default → 0.
    Non-numeric values are treated as 0 (same as ProxLB's own logic).
    """
    gb = reserve_cfg.get(node_name, {}).get("memory") \
        or reserve_cfg.get("defaults", {}).get("memory", 0)
    gb = gb if isinstance(gb, (int, float)) else 0
    return int(gb * 1024 ** 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def from_proxlb_data(
    proxlb_data: Dict[str, Any],
    use_reservations: bool = True,
    pin_vms: set | None = None,
) -> Cluster:
    """Convert a proxlb_data dict into a Cluster ready for the solver.

    Args:
        proxlb_data:      The merged dict built by ProxLB's main loop.
        use_reservations: If True (default), node_resource_reserve entries
                          from the balancing config are applied as explicit
                          Node.memory_reserve values.  Set to False to let
                          the solver treat the full hardware RAM as available.
        pin_vms:          Optional set of VM names to force-pin to their
                          current node.  Used by the active-mode feedback loop
                          after a failed migration so that the VM is not
                          re-attempted in the next solve.
    """
    meta = proxlb_data.get("meta", {})
    balancing_cfg = meta.get("balancing", {})
    reserve_cfg = balancing_cfg.get("node_resource_reserve", {})

    # 1. Map Balancing Configuration
    balancing = Balancing(
        method=balancing_cfg.get("method", "memory"),
        balanciness=balancing_cfg.get("balanciness", 3),
        cpu_overcommit=balancing_cfg.get("cpu_overcommit", 2.0),
        max_node_inflow=balancing_cfg.get("max_node_inflow", 1),
        max_parallel_migrations=balancing_cfg.get("max_parallel_migrations")
    )

    # 2. Map Nodes
    nodes = []
    for name, nd in proxlb_data.get("nodes", {}).items():
        storage_free = {"local": nd.get("disk_free", 0)}

        # Reconstruct the raw hardware total from the PVE API fields that
        # ProxLB stores untouched.  memory_total is already reservation-reduced
        # and cannot be used here.
        mem_used = nd.get("memory_used", 0)
        mem_free = nd.get("memory_free", 0)
        raw_memory = mem_used + mem_free

        if raw_memory > 0:
            # We have the raw value — apply reservation explicitly.
            memory_reserve = (
                _get_memory_reserve_bytes(name, reserve_cfg)
                if use_reservations else 0
            )
            # Safety clamp: reservation must not exceed available hardware RAM.
            memory_reserve = min(memory_reserve, raw_memory)
        else:
            # Fallback for synthetic/test data that only provides memory_total.
            # memory_total is already reduced; we cannot separate the reserve.
            raw_memory = nd.get("memory_total", 0)
            memory_reserve = 0

        nodes.append(Node(
            name=name,
            cpu_total=nd.get("cpu_total", 0),
            memory_total=raw_memory,
            memory_reserve=memory_reserve,
            storage_free=storage_free,
            cpu_pressure=nd.get("cpu_pressure_some_percent", 0.0),
            memory_pressure=nd.get("memory_pressure_some_percent", 0.0),
            io_pressure=nd.get("disk_pressure_some_percent", 0.0),
            maintenance=nd.get("maintenance", False)
        ))

    # 3. Map Guests (VMs/Containers) and extract implicit constraints
    vms = []
    affinity_map: dict = defaultdict(list)
    anti_affinity_map: dict = defaultdict(list)
    pin_rules = []
    ignore_list = []

    for name, gd in proxlb_data.get("guests", {}).items():
        vms.append(VM(
            name=name,
            node=gd.get("node_current"),
            cpu=gd.get("cpu_total", 1),
            memory=gd.get("memory_total", 0),
            cpu_usage=gd.get("cpu_used", 0.0),
            cpu_pressure=gd.get("cpu_pressure_some_percent", 0.0),
            memory_pressure=gd.get("memory_pressure_some_percent", 0.0),
            io_pressure=gd.get("disk_pressure_some_percent", 0.0),
            disks={},  # Specific disk pools are skipped in simulation for now
            priority=gd.get("priority", 2),
            vm_type=gd.get("type", "vm")
        ))

        # PVE Native HA Rules — affinity and anti-affinity groups
        for rule in gd.get("ha_rules", []):
            rule_id = rule["rule"]
            rule_type = rule.get("type", "")
            if rule_type == "affinity":
                affinity_map[(rule_id, "pve")].append(name)
            elif rule_type == "anti-affinity":
                anti_affinity_map[(rule_id, "pve")].append(name)
            # Unknown types are ignored (not silently mis-classified)

        # ProxLB Tags — affinity / anti-affinity by tag prefix
        for tag in gd.get("tags", []):
            if tag.startswith("plb_affinity"):
                affinity_map[(tag, "plb")].append(name)
            elif tag.startswith("plb_anti_affinity"):
                anti_affinity_map[(tag, "plb")].append(name)

        # ProxLB Pools — affinity / anti-affinity by pool config
        for pool in gd.get("pools", []):
            pool_cfg = balancing_cfg.get("pools", {}).get(pool)
            if pool_cfg:
                if pool_cfg.get("type") == "affinity":
                    affinity_map[(pool, "plb")].append(name)
                elif pool_cfg.get("type") == "anti-affinity":
                    anti_affinity_map[(pool, "plb")].append(name)

        # Node pins — re-derived from raw sources to preserve origin metadata.
        # The validated node list comes from ProxLB's pre-computed
        # node_relationships (invalid/missing nodes already filtered).
        # We reconstruct which source contributed each pin for logging.
        if gd.get("node_relationships"):
            pin_origins: list[dict] = []

            for tag in gd.get("tags", []):
                if tag.startswith("plb_pin"):
                    pin_origins.append({"origin": "tag", "source": tag})

            for pool in gd.get("pools", []):
                pool_cfg = balancing_cfg.get("pools", {}).get(pool, {})
                if pool_cfg.get("pin"):
                    pin_origins.append({"origin": "pool", "source": pool})

            for rule in gd.get("ha_rules", []):
                if rule.get("type") == "affinity" and rule.get("nodes"):
                    pin_origins.append({"origin": "pve", "source": rule["rule"]})

            pin_rules.append({
                "vm": name,
                "nodes": gd["node_relationships"],  # validated by ProxLB
                "origins": pin_origins,
            })

        # Active-mode feedback: pin VMs whose migrations failed to their
        # current node so the re-solve cannot attempt to move them again.
        if pin_vms and name in pin_vms:
            pin_rules.append({
                "vm": name,
                "nodes": [gd.get("node_current", "")],
                "origins": [{"origin": "solver", "source": "migration_failed"}],
            })

        # Ignore flags
        if gd.get("ignore"):
            ignore_list.append(name)

    # 4. Build Constraints — only keep groups with more than 1 member
    constraints = Constraints(
        affinity=[
            {"name": k[0], "origin": k[1], "vms": v, "hard": True}
            for k, v in affinity_map.items() if len(v) > 1
        ],
        anti_affinity=[
            {"name": k[0], "origin": k[1], "vms": v, "hard": True}
            for k, v in anti_affinity_map.items() if len(v) > 1
        ],
        pin=pin_rules,
        ignore=ignore_list
    )

    return Cluster(
        name=meta.get("cluster_name", "Live Cluster"),
        description="Auto-generated from ProxLB live data",
        balancing=balancing,
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=Expect(feasible=True)
    )
