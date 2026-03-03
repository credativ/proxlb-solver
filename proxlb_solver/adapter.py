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
    meta = proxlb_data.get("meta", {})
    
    balancing_cfg = meta.get("balancing", {})
    
    if isinstance(balancing_cfg, dict):
        reserve_cfg = balancing_cfg.get("node_resource_reserve") or {}
    else:
        # Pydantic model access
        reserve_cfg = getattr(balancing_cfg, "node_resource_reserve", None) or {}

    # 1. Map Balancing Configuration
    if isinstance(balancing_cfg, dict):
        balancing = Balancing(
            method=balancing_cfg.get("method", "memory"),
            mode=balancing_cfg.get("mode", "used"),
            balanciness=balancing_cfg.get("balanciness", 3),
            cpu_overcommit=balancing_cfg.get("cpu_overcommit", 2.0),
            max_node_inflow=balancing_cfg.get("max_node_inflow", 1),
            max_parallel_migrations=balancing_cfg.get("max_parallel_migrations")
        )
    else:
        # Pydantic model access
        balancing = Balancing(
            method=getattr(balancing_cfg, "method", "memory"),
            mode=getattr(balancing_cfg, "mode", "used"),
            balanciness=getattr(balancing_cfg, "balanciness", 3),
            cpu_overcommit=getattr(balancing_cfg, "cpu_overcommit", 2.0),
            max_node_inflow=getattr(balancing_cfg, "max_node_inflow", 1),
            max_parallel_migrations=getattr(balancing_cfg, "max_parallel_migrations", None)
        )

    # 2. Map Nodes
    nodes = []
    for name, nd in proxlb_data.get("nodes", {}).items():
        # Unified access for dict or model-dumped dict
        def _nd_val(key, subkey=None, default=0):
            val = nd.get(key)
            if subkey and isinstance(val, dict):
                return val.get(subkey, default)
            return val if val is not None else default

        storage_free = {"local": _nd_val("disk_free", default=_nd_val("disk", "free"))}

        # Reconstruct the raw hardware total from the PVE API fields that
        # ProxLB stores untouched.  memory_total is already reservation-reduced
        # and cannot be used here.
        mem_used = int(_nd_val("memory_used", default=_nd_val("memory", "used")))
        mem_free = int(_nd_val("memory_free", default=_nd_val("memory", "free")))
        raw_memory = mem_used + mem_free

        if raw_memory > 0:
            # We have the raw value — apply reservation explicitly.
            memory_reserve = int(
                _get_memory_reserve_bytes(name, reserve_cfg)
                if use_reservations else 0
            )
            # Safety clamp: reservation must not exceed available hardware RAM.
            memory_reserve = min(memory_reserve, raw_memory)
        else:
            # Fallback for synthetic/test data that only provides memory_total.
            # memory_total is already reduced; we cannot separate the reserve.
            raw_memory = int(_nd_val("memory_total", default=_nd_val("memory", "total")))
            memory_reserve = 0

        nodes.append(Node(
            name=name,
            cpu_total=int(_nd_val("cpu_total", default=_nd_val("cpu", "total"))),
            memory_total=raw_memory,
            memory_reserve=memory_reserve,
            storage_free=storage_free,
            cpu_pressure=_nd_val("cpu_pressure_some_percent", default=_nd_val("cpu", "pressure_some_percent")),
            memory_pressure=_nd_val("memory_pressure_some_percent", default=_nd_val("memory", "pressure_some_percent")),
            io_pressure=_nd_val("disk_pressure_some_percent", default=_nd_val("disk", "pressure_some_percent")),
            maintenance=_nd_val("maintenance", default=False)
        ))

    # 3. Map Guests (VMs/Containers) and extract implicit constraints
    vms = []
    affinity_map: dict = defaultdict(list)
    anti_affinity_map: dict = defaultdict(list)
    pin_rules = []
    ignore_list = []

    for name, gd in proxlb_data.get("guests", {}).items():
        def _gd_val(key, subkey=None, default=0):
            val = gd.get(key)
            if subkey and isinstance(val, dict):
                return val.get(subkey, default)
            return val if val is not None else default

        vms.append(VM(
            name=name,
            node=_gd_val("node_current", default=gd.get("node_current")),
            cpu=int(_gd_val("cpu_total", default=_gd_val("cpu", "total", default=1))),
            memory=int(_gd_val("memory_total", default=_gd_val("memory", "total"))),
            cpu_usage=_gd_val("cpu_used", default=_gd_val("cpu", "used")),
            cpu_pressure=_gd_val("cpu_pressure_some_percent", default=_gd_val("cpu", "pressure_some_percent")),
            memory_pressure=_gd_val("memory_pressure_some_percent", default=_gd_val("memory", "pressure_some_percent")),
            io_pressure=_gd_val("disk_pressure_some_percent", default=_gd_val("disk", "pressure_some_percent")),
            disks={},  # Specific disk pools are skipped in simulation for now
            priority=int(_gd_val("priority", default=2)),
            vm_type=_gd_val("type", default="vm")
        ))

        # PVE Native HA Rules — affinity and anti-affinity groups
        for rule in gd.get("ha_rules", []):
            # Support both dict and Pydantic model (via dict access if dumped)
            if isinstance(rule, dict):
                rule_id = rule["rule"]
                rule_type = rule.get("type", "")
            else:
                rule_id = getattr(rule, "rule")
                rule_type = getattr(rule, "type", "")

            if rule_type == "affinity":
                affinity_map[(rule_id, "pve")].append(name)
            elif rule_type == "anti-affinity":
                anti_affinity_map[(rule_id, "pve")].append(name)

        # ProxLB Tags — affinity / anti-affinity by tag prefix
        for tag in gd.get("tags", []):
            if tag.startswith("plb_affinity"):
                affinity_map[(tag, "plb")].append(name)
            elif tag.startswith("plb_anti_affinity"):
                anti_affinity_map[(tag, "plb")].append(name)

        # ProxLB Pools — affinity / anti-affinity by pool config
        for pool in gd.get("pools", []):
            if isinstance(reserve_cfg, dict):
                pool_cfg = balancing_cfg.get("pools", {}).get(pool)
            else:
                pool_cfg = getattr(balancing_cfg, "pools", {}).get(pool)

            if pool_cfg:
                # Support both dict and Pydantic model
                if isinstance(pool_cfg, dict):
                    p_type = pool_cfg.get("type")
                else:
                    p_type = getattr(pool_cfg, "type", None)

                if p_type == "affinity":
                    affinity_map[(pool, "plb")].append(name)
                elif p_type == "anti-affinity":
                    anti_affinity_map[(pool, "plb")].append(name)

        # Node pins — re-derived from raw sources to preserve origin metadata.
        if gd.get("node_relationships"):
            pin_origins: list[dict] = []

            for tag in gd.get("tags", []):
                if tag.startswith("plb_pin"):
                    pin_origins.append({"origin": "tag", "source": tag})

            for pool in gd.get("pools", []):
                if isinstance(reserve_cfg, dict):
                    pool_cfg = balancing_cfg.get("pools", {}).get(pool, {})
                else:
                    pool_cfg = getattr(balancing_cfg, "pools", {}).get(pool, {})

                if isinstance(pool_cfg, dict):
                    has_pin = pool_cfg.get("pin")
                else:
                    has_pin = getattr(pool_cfg, "pin", None)

                if has_pin:
                    pin_origins.append({"origin": "pool", "source": pool})

            for rule in gd.get("ha_rules", []):
                if isinstance(rule, dict):
                    r_type = rule.get("type")
                    r_nodes = rule.get("nodes")
                    r_rule = rule["rule"]
                else:
                    r_type = getattr(rule, "type")
                    r_nodes = getattr(rule, "nodes")
                    r_rule = getattr(rule, "rule")

                if r_type == "affinity" and r_nodes:
                    pin_origins.append({"origin": "pve", "source": r_rule})

            pin_rules.append({
                "vm": name,
                "nodes": gd["node_relationships"],  # validated by ProxLB
                "origins": pin_origins,
            })

        # Active-mode feedback: pin VMs whose migrations failed
        if pin_vms and name in pin_vms:
            pin_rules.append({
                "vm": name,
                "nodes": [_gd_val("node_current", default=gd.get("node_current"))],
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
