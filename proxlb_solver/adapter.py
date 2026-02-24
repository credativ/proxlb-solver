"""Adapter to convert ProxLB internal data structures to Solver models."""

from __future__ import annotations
from typing import Dict, Any, List, Set
from collections import defaultdict
from .models import Cluster, Node, VM, Constraints, Balancing, Expect

def from_proxlb_data(proxlb_data: Dict[str, Any]) -> Cluster:
    """
    Convert the 'proxlb_data' dictionary from ProxLB into a Cluster model.
    """
    meta = proxlb_data.get("meta", {})
    balancing_cfg = meta.get("balancing", {})
    
    # 1. Map Balancing settings
    balancing = Balancing(
        method=balancing_cfg.get("method", "memory"),
        balanciness=balancing_cfg.get("balanciness", 3),
        cpu_overcommit=balancing_cfg.get("cpu_overcommit", 2.0),
    )

    # 2. Map Nodes
    nodes = []
    for name, nd in proxlb_data.get("nodes", {}).items():
        # Map generic disk_free to 'local' for simulation stability
        storage_free = {"local": nd.get("disk_free", 0)}
        nodes.append(Node(
            name=name,
            cpu_total=nd.get("cpu_total", 0),
            memory_total=nd.get("memory_total", 0),
            storage_free=storage_free,
            cpu_pressure=nd.get("cpu_pressure_some_percent", 0.0),
            memory_pressure=nd.get("memory_pressure_some_percent", 0.0),
            io_pressure=nd.get("disk_pressure_some_percent", 0.0),
            maintenance=nd.get("maintenance", False)
        ))

    # 3. Map VMs and extract implicit constraints
    vms = []
    affinity_map = defaultdict(list)
    anti_affinity_map = defaultdict(list)
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
            disks={}, # Skip disks for now to avoid false INFEASIBLE in simulation
            vm_type=gd.get("type", "vm")
        ))

        # Collect affinity group memberships
        for group in gd.get("affinity_groups", []):
            affinity_map[group].append(name)
        
        # Collect anti-affinity group memberships
        for group in gd.get("anti_affinity_groups", []):
            anti_affinity_map[group].append(name)
            
        # Pinning
        if gd.get("node_relationships"):
            pin_rules.append({
                "vm": name,
                "nodes": gd["node_relationships"]
            })
            
        # Ignore
        if gd.get("ignore"):
            ignore_list.append(name)

    # 4. Construct Constraints object
    constraints = Constraints(
        affinity=[{"name": k, "vms": v} for k, v in affinity_map.items() if len(v) > 1],
        anti_affinity=[{"name": k, "vms": v} for k, v in anti_affinity_map.items() if len(v) > 1],
        pin=pin_rules,
        ignore=ignore_list
    )

    return Cluster(
        name=meta.get("cluster_name", "Live Cluster"),
        description="Simulated from live Proxmox data",
        balancing=balancing,
        nodes=nodes,
        vms=vms,
        constraints=constraints,
        expect=Expect(feasible=True)
    )
