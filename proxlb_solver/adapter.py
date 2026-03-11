"""
Adapter to convert ProxLB data into Solver models.

Accepts either:
  - a plain dict  (standalone CLI use, unit tests, ProxLB ≤ 1.x)
  - a ProxLbData Pydantic model  (ProxLB 2.0 integration)

When a Pydantic model is received it is first normalised to the same flat-dict
layout that the rest of this module expects, so there is a single code path
after normalisation.

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
from typing import TYPE_CHECKING, Any, Dict

from .models import Balancing, Cluster, Constraints, Expect, Node, VM

if TYPE_CHECKING:
    from proxlb.utils.proxlb_data import ProxLbData


# ---------------------------------------------------------------------------
# Normalisation: Pydantic → flat dict
# ---------------------------------------------------------------------------

def _reserve_to_dict(raw: Any) -> dict[str, Any]:
    """Convert node_resource_reserve (may have StrEnum keys) to plain str keys."""
    if not raw:
        return {}
    result = {}
    for node_name, res_dict in raw.items():
        result[str(node_name)] = {str(k): v for k, v in res_dict.items()}
    return result


def _pydantic_to_dict(proxlb_data: "ProxLbData") -> Dict[str, Any]:
    """
    Flatten a ProxLbData Pydantic model into the dict layout expected by
    from_proxlb_data().  Only the fields the adapter actually reads are mapped.
    """
    bc = proxlb_data.meta.balancing

    balancing_dict: Dict[str, Any] = {
        "method":                  str(bc.method),
        "mode":                    str(bc.mode),
        "balanciness":             bc.balanciness,
        "cpu_overcommit":          getattr(bc, "cpu_overcommit", 2.0),
        "memory_threshold":        bc.memory_threshold,
        "cpu_threshold":           bc.cpu_threshold,
        "disk_threshold":          bc.disk_threshold,
        "max_node_inflow":         getattr(bc, "max_node_inflow", 1),
        "max_parallel_migrations": getattr(bc, "max_parallel_migrations", None),
        "node_resource_reserve":   _reserve_to_dict(bc.node_resource_reserve),
        "pools":                   bc.pools,
    }

    nodes: Dict[str, Any] = {}
    for name, nd in proxlb_data.nodes.items():
        nodes[name] = {
            "cpu_total":                    nd.cpu.total,
            "memory_used":                  int(nd.memory.used),
            "memory_free":                  int(nd.memory.free),
            "memory_total":                 nd.memory.total,
            "disk_free":                    int(nd.disk.free),
            "cpu_pressure_some_percent":    nd.cpu.pressure_some_percent,
            "memory_pressure_some_percent": nd.memory.pressure_some_percent,
            "disk_pressure_some_percent":   nd.disk.pressure_some_percent,
            "maintenance":                  nd.maintenance,
        }

    guests: Dict[str, Any] = {}
    for name, gd in proxlb_data.guests.items():
        guests[name] = {
            "node_current":                 gd.node_current,
            "node_target":                  gd.node_target,
            "cpu_total":                    gd.cpu.total,
            "memory_total":                 gd.memory.total,
            "cpu_used":                     gd.cpu.used,
            "disk_used":                    int(gd.disk.used),
            "cpu_pressure_some_percent":    gd.cpu.pressure_some_percent,
            "memory_pressure_some_percent": gd.memory.pressure_some_percent,
            "disk_pressure_some_percent":   gd.disk.pressure_some_percent,
            "type":                         str(gd.type),
            "ha_rules": [
                {"rule": r.rule, "type": str(r.type), "nodes": r.nodes}
                for r in gd.ha_rules
            ],
            "tags":               gd.tags,
            "pools":              gd.pools,
            "node_relationships": gd.node_relationships,
            "ignore":             gd.ignore,
        }

    return {
        "meta":     {"balancing": balancing_dict, "cluster_name": "Live Cluster"},
        "nodes":    nodes,
        "guests":   guests,
        "groups":   {},
        "ha_rules": {},
        "pools":    {},
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_memory_reserve_bytes(node_name: str, reserve_cfg: dict[str, Any]) -> int:
    """
    Returns the configured memory reservation for a specific node in bytes.

    Resolution order:
    1. Node-specific override  (e.g. reserve_cfg['pve-01']['memory'])
    2. Global default          (reserve_cfg['defaults']['memory'])
    3. Zero                    (nothing configured)
    """
    node_config    = reserve_cfg.get(node_name, {})
    default_config = reserve_cfg.get("defaults", {})
    gb = node_config.get("memory") or default_config.get("memory", 0)
    if not isinstance(gb, (int, float)):
        gb = 0
    return int(gb * 1024 ** 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def from_proxlb_data(
    proxlb_data: "Dict[str, Any] | ProxLbData",
    use_reservations: bool = True,
    pin_vms: set[str] | None = None,
) -> Cluster:
    """
    Convert ProxLB data into a Cluster ready for the solver.

    Accepts either a plain dict (standalone / tests) or a ProxLbData Pydantic
    model (ProxLB 2.0 integration).  Pydantic models are normalised to a flat
    dict internally so only one code path exists after this point.

    Args:
        proxlb_data:      Dict or ProxLbData from the ProxLB main loop.
        use_reservations: If True, node memory reservations are applied explicitly.
        pin_vms:          VM names to force-pin to their current node (active-mode
                          retry: VMs that failed to migrate previously).
    """
    if not isinstance(proxlb_data, dict):
        proxlb_data = _pydantic_to_dict(proxlb_data)

    meta         = proxlb_data.get("meta", {})
    balancing_cfg = meta.get("balancing", {}) if isinstance(meta, dict) else {}

    # 1. Balancing configuration -------------------------------------------
    balancing = Balancing(
        method=balancing_cfg.get("method", "memory"),
        mode=balancing_cfg.get("mode", "used"),
        balanciness=balancing_cfg.get("balanciness", 3),
        cpu_overcommit=balancing_cfg.get("cpu_overcommit", 2.0),
        memory_threshold=balancing_cfg.get("memory_threshold"),
        cpu_threshold=balancing_cfg.get("cpu_threshold"),
        disk_threshold=balancing_cfg.get("disk_threshold"),
        max_node_inflow=balancing_cfg.get("max_node_inflow", 1),
        max_parallel_migrations=balancing_cfg.get("max_parallel_migrations"),
    )
    reserve_cfg: dict[str, Any] = balancing_cfg.get("node_resource_reserve") or {}

    # 2. Physical nodes --------------------------------------------------------
    nodes = []
    for name, nd in proxlb_data.get("nodes", {}).items():

        def _nd(key: str, default: Any = 0, _nd: dict[str, Any] = nd) -> Any:  # noqa: E731
            return _nd.get(key, default)

        mem_used  = int(_nd("memory_used"))
        mem_free  = int(_nd("memory_free"))
        raw_memory = mem_used + mem_free

        if raw_memory > 0:
            memory_reserve = (
                int(_get_memory_reserve_bytes(name, reserve_cfg))
                if use_reservations else 0
            )
            memory_reserve = min(memory_reserve, raw_memory)
        else:
            raw_memory     = int(_nd("memory_total"))
            memory_reserve = 0

        nodes.append(Node(
            name=name,
            cpu_total=int(_nd("cpu_total")),
            memory_total=raw_memory,
            memory_reserve=memory_reserve,
            storage_free={"local": int(_nd("disk_free"))},
            cpu_pressure=_nd("cpu_pressure_some_percent", 0.0),
            memory_pressure=_nd("memory_pressure_some_percent", 0.0),
            io_pressure=_nd("disk_pressure_some_percent", 0.0),
            maintenance=bool(_nd("maintenance", False)),
        ))

    # 3. Guests + constraint extraction ----------------------------------------
    vms: list[VM] = []
    affinity_groups:      dict[tuple[str, str], list[str]] = defaultdict(list)
    anti_affinity_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    pin_rules:   list[dict[str, Any]] = []
    ignore_list: list[str]  = []

    pools_config = balancing_cfg.get("pools") or {}

    for name, gd in proxlb_data.get("guests", {}).items():

        def _gd(key: str, default: Any = 0, _gd: dict[str, Any] = gd) -> Any:  # noqa: E731
            return _gd.get(key, default)

        vms.append(VM(
            name=name,
            node=_gd("node_current", ""),
            cpu=int(_gd("cpu_total", 1)),
            memory=int(_gd("memory_total")),
            cpu_usage=float(_gd("cpu_used")),
            cpu_pressure=_gd("cpu_pressure_some_percent", 0.0),
            memory_pressure=_gd("memory_pressure_some_percent", 0.0),
            io_pressure=_gd("disk_pressure_some_percent", 0.0),
            disks={"local": int(_gd("disk_used"))},
            priority=int(_gd("priority", 2)),
            vm_type=str(_gd("type", "vm")),
        ))

        # A. Native Proxmox HA rules
        for rule in _gd("ha_rules", []):
            r_id   = str(rule.get("rule") if isinstance(rule, dict) else getattr(rule, "rule", ""))
            r_type = rule.get("type") if isinstance(rule, dict) else str(getattr(rule, "type", ""))
            if r_type == "affinity":
                affinity_groups[(r_id, "pve")].append(name)
            elif r_type == "anti-affinity":
                anti_affinity_groups[(r_id, "pve")].append(name)

        # B. ProxLB tags  (plb_affinity_*, plb_anti_affinity_*)
        for tag in _gd("tags", []):
            if tag.startswith("plb_affinity"):
                affinity_groups[(tag, "plb")].append(name)
            elif tag.startswith("plb_anti_affinity"):
                anti_affinity_groups[(tag, "plb")].append(name)

        # C. Resource pool rules
        for pool_name in _gd("pools", []):
            pool_cfg = pools_config.get(pool_name) if isinstance(pools_config, dict) else None
            if pool_cfg:
                p_type = pool_cfg.get("type") if isinstance(pool_cfg, dict) else str(getattr(pool_cfg, "type", ""))
                if p_type == "affinity":
                    affinity_groups[(pool_name, "plb")].append(name)
                elif p_type == "anti-affinity":
                    anti_affinity_groups[(pool_name, "plb")].append(name)

        # D. Node pinning  (tags, pools, HA restricted nodes)
        node_rels = _gd("node_relationships", [])
        if node_rels:
            origins: list[dict[str, Any]] = []
            for tag in _gd("tags", []):
                if tag.startswith("plb_pin"):
                    origins.append({"origin": "tag", "source": tag})
            for pool in _gd("pools", []):
                pool_cfg = pools_config.get(pool) if isinstance(pools_config, dict) else None
                if pool_cfg:
                    has_pin = pool_cfg.get("pin") if isinstance(pool_cfg, dict) else getattr(pool_cfg, "pin", None)
                    if has_pin:
                        origins.append({"origin": "pool", "source": pool})
            for rule in _gd("ha_rules", []):
                r_type  = rule.get("type")  if isinstance(rule, dict) else str(getattr(rule, "type", ""))
                r_nodes = rule.get("nodes") if isinstance(rule, dict) else getattr(rule, "nodes", [])
                r_id    = str(rule.get("rule") if isinstance(rule, dict) else getattr(rule, "rule", ""))
                if r_type == "affinity" and r_nodes:
                    origins.append({"origin": "pve", "source": r_id})
            pin_rules.append({
                "vm":      name,
                "nodes":   node_rels,
                "origins": origins,
            })

        # E. Active-mode feedback: pin VMs that failed to migrate previously
        if pin_vms and name in pin_vms:
            pin_rules.append({
                "vm":      name,
                "nodes":   [_gd("node_current", "")],
                "origins": [{"origin": "solver", "source": "migration_failed"}],
            })

        # F. Ignore flag
        if _gd("ignore", False):
            ignore_list.append(name)

    # 4. Assemble Constraints --------------------------------------------------
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

    cluster_name = meta.get("cluster_name", "Live Cluster") if isinstance(meta, dict) else "Live Cluster"

    return Cluster(
        name=cluster_name,
        description="Auto-generated from ProxLB live data",
        balancing=balancing,
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=Expect(feasible=True),
    )
