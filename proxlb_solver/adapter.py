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
from typing import Dict, Any, Optional
from collections import defaultdict
from .models import Cluster, Node, VM, Constraints, Balancing, Expect


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_memory_reserve_bytes(node_name: str, reserve_cfg: dict) -> int:
    """
    Returns the configured memory reservation for a specific node in bytes.

    Resolution order:
    1. Node-specific override (e.g. reserve_cfg['pve-01']['memory'])
    2. Global default (reserve_cfg['defaults']['memory'])
    3. Zero (if nothing is configured)
    """
    node_config = reserve_cfg.get(node_name, {})
    default_config = reserve_cfg.get("defaults", {})

    # Try node-specific first, then default
    gb = node_config.get("memory") or default_config.get("memory", 0)

    # Ensure we have a numeric value
    if not isinstance(gb, (int, float)):
        gb = 0

    return int(gb * 1024 ** 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def from_proxlb_data(
    proxlb_data: Dict[str, Any],
    use_reservations: bool = True,
    pin_vms: set | None = None,
) -> Cluster:
    """
    Converts a proxlb_data dict (from ProxLB main loop) into a Cluster
    model ready for the solver.

    Args:
        proxlb_data:      The merged dict built by ProxLB.
        use_reservations: If True, node reservations are applied explicitly.
        pin_vms:          Optional set of VM names to force-pin to their current node
                          (used for retrying after migration failures).
    """
    meta = proxlb_data.get("meta", {})
    balancing_cfg = meta.get("balancing", {})

    # 1. Handle Balancing Configuration
    # ProxLB might provide this as a raw dict or a Pydantic model (depending on branch)
    if isinstance(balancing_cfg, dict):
        # Legacy Dict Access
        balancing = Balancing(
            method=balancing_cfg.get("method", "memory"),
            mode=balancing_cfg.get("mode", "used"),
            balanciness=balancing_cfg.get("balanciness", 3),
            cpu_overcommit=balancing_cfg.get("cpu_overcommit", 2.0),
            memory_threshold=balancing_cfg.get("memory_threshold"),
            cpu_threshold=balancing_cfg.get("cpu_threshold"),
            disk_threshold=balancing_cfg.get("disk_threshold"),
            max_node_inflow=balancing_cfg.get("max_node_inflow", 1),
            max_parallel_migrations=balancing_cfg.get("max_parallel_migrations")
        )
        reserve_cfg = balancing_cfg.get("node_resource_reserve") or {}
    else:
        # Pydantic Model Access
        balancing = Balancing(
            method=getattr(balancing_cfg, "method", "memory"),
            mode=getattr(balancing_cfg, "mode", "used"),
            balanciness=getattr(balancing_cfg, "balanciness", 3),
            cpu_overcommit=getattr(balancing_cfg, "cpu_overcommit", 2.0),
            memory_threshold=getattr(balancing_cfg, "memory_threshold", None),
            cpu_threshold=getattr(balancing_cfg, "cpu_threshold", None),
            disk_threshold=getattr(balancing_cfg, "disk_threshold", None),
            max_node_inflow=getattr(balancing_cfg, "max_node_inflow", 1),
            max_parallel_migrations=getattr(balancing_cfg, "max_parallel_migrations", None)
        )
        reserve_cfg = getattr(balancing_cfg, "node_resource_reserve", None) or {}

    # 2. Map Physical Nodes
    nodes = []
    for name, nd in proxlb_data.get("nodes", {}).items():

        # Helper to extract values from nested dicts (handle different ProxLB versions)
        def _get_val(key, subkey=None, default=0):
            val = nd.get(key)
            if subkey and isinstance(val, dict):
                return val.get(subkey, default)
            return val if val is not None else default

        # Reconstruct raw hardware memory (before reservations)
        mem_used = int(_get_val("memory_used", default=_get_val("memory", "used")))
        mem_free = int(_get_val("memory_free", default=_get_val("memory", "free")))
        raw_memory = mem_used + mem_free

        if raw_memory > 0:
            # Apply reservation explicitly so the solver can "see" it
            memory_reserve = int(_get_memory_reserve_bytes(name, reserve_cfg)) if use_reservations else 0
            # Safety clamp: reservation cannot exceed hardware RAM
            memory_reserve = min(memory_reserve, raw_memory)
        else:
            # Fallback for data that only provides the pre-reduced total
            raw_memory = int(_get_val("memory_total", default=_get_val("memory", "total")))
            memory_reserve = 0

        nodes.append(Node(
            name=name,
            cpu_total=int(_get_val("cpu_total", default=_get_val("cpu", "total"))),
            memory_total=raw_memory,
            memory_reserve=memory_reserve,
            storage_free={"local": _get_val("disk_free", default=_get_val("disk", "free"))},
            cpu_pressure=_get_val("cpu_pressure_some_percent", default=_get_val("cpu", "pressure_some_percent")),
            memory_pressure=_get_val("memory_pressure_some_percent", default=_get_val("memory", "pressure_some_percent")),
            io_pressure=_get_val("disk_pressure_some_percent", default=_get_val("disk", "pressure_some_percent")),
            maintenance=_get_val("maintenance", default=False)
        ))

    # 3. Map Guests (VMs/Containers) and extract implicit constraints (Tags, Pools, HA)
    vms = []
    affinity_groups: dict = defaultdict(list)
    anti_affinity_groups: dict = defaultdict(list)
    pin_rules = []
    ignore_list = []

    for name, guest_data in proxlb_data.get("guests", {}).items():

        # Unified value extractor for guest metrics
        def _get_guest_val(key, subkey=None, default=0):
            val = guest_data.get(key)
            if subkey and isinstance(val, dict):
                return val.get(subkey, default)
            return val if val is not None else default

        vms.append(VM(
            name=name,
            node=_get_guest_val("node_current", default=guest_data.get("node_current")),
            cpu=int(_get_guest_val("cpu_total", default=_get_guest_val("cpu", "total", default=1))),
            memory=int(_get_guest_val("memory_total", default=_get_guest_val("memory", "total"))),
            cpu_usage=_get_guest_val("cpu_used", default=_get_guest_val("cpu", "used")),
            cpu_pressure=_get_guest_val("cpu_pressure_some_percent", default=_get_guest_val("cpu", "pressure_some_percent")),
            memory_pressure=_get_guest_val("memory_pressure_some_percent", default=_get_guest_val("memory", "pressure_some_percent")),
            io_pressure=_get_guest_val("disk_pressure_some_percent", default=_get_guest_val("disk", "pressure_some_percent")),
            priority=int(_get_guest_val("priority", default=2)),
            vm_type=_get_guest_val("type", default="vm")
        ))

        # ── Parse Placement Rules ───────────────────────────────────────────

        # A. Native Proxmox HA Rules
        for rule in guest_data.get("ha_rules", []):
            # Extract rule details (supporting both Dict and Pydantic)
            if isinstance(rule, dict):
                r_id, r_type = rule["rule"], rule.get("type", "")
            else:
                r_id, r_type = getattr(rule, "rule"), getattr(rule, "type", "")

            if r_type == "affinity":
                affinity_groups[(r_id, "pve")].append(name)
            elif r_type == "anti-affinity":
                anti_affinity_groups[(r_id, "pve")].append(name)

        # B. ProxLB Tags (plb_affinity_*, plb_anti_affinity_*)
        for tag in guest_data.get("tags", []):
            if tag.startswith("plb_affinity"):
                affinity_groups[(tag, "plb")].append(name)
            elif tag.startswith("plb_anti_affinity"):
                anti_affinity_groups[(tag, "plb")].append(name)

        # C. ProxLB Resource Pools
        for pool_name in guest_data.get("pools", []):
            # Resolve pool configuration from balancing settings
            if isinstance(balancing_cfg, dict):
                pool_cfg = balancing_cfg.get("pools", {}).get(pool_name)
            else:
                pool_cfg = getattr(balancing_cfg, "pools", {}).get(pool_name)

            if pool_cfg:
                p_type = pool_cfg.get("type") if isinstance(pool_cfg, dict) else getattr(pool_cfg, "type", None)
                if p_type == "affinity":
                    affinity_groups[(pool_name, "plb")].append(name)
                elif p_type == "anti-affinity":
                    anti_affinity_groups[(pool_name, "plb")].append(name)

        # D. Node Pinning (Tags, Pools, or HA restricted nodes)
        if guest_data.get("node_relationships"):
            origins: list[dict] = []

            # Record where the pin came from for logging/debugging
            for tag in guest_data.get("tags", []):
                if tag.startswith("plb_pin"): origins.append({"origin": "tag", "source": tag})

            for pool in guest_data.get("pools", []):
                p_cfg = balancing_cfg.get("pools", {}).get(pool, {}) if isinstance(balancing_cfg, dict) else getattr(balancing_cfg, "pools", {}).get(pool, {})
                has_pin = p_cfg.get("pin") if isinstance(p_cfg, dict) else getattr(p_cfg, "pin", None)
                if has_pin: origins.append({"origin": "pool", "source": pool})

            for rule in guest_data.get("ha_rules", []):
                if isinstance(rule, dict):
                    r_type, r_nodes, r_id = rule.get("type"), rule.get("nodes"), rule.get("rule")
                else:
                    r_type, r_nodes, r_id = getattr(rule, "type", None), getattr(rule, "nodes", None), getattr(rule, "rule", None)

                if r_type == "affinity" and r_nodes:
                    origins.append({"origin": "pve", "source": r_id})

            pin_rules.append({
                "vm": name,
                "nodes": guest_data["node_relationships"],
                "origins": origins,
            })

        # E. Active-mode feedback (pin VMs that failed to migrate previously)
        if pin_vms and name in pin_vms:
            pin_rules.append({
                "vm": name,
                "nodes": [_get_guest_val("node_current", default=guest_data.get("node_current"))],
                "origins": [{"origin": "solver", "source": "migration_failed"}],
            })

        # F. Ignore flags
        if guest_data.get("ignore"):
            ignore_list.append(name)

    # 4. Finalize Constraints Model
    # Note: We only create groups if they have more than one member.
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
