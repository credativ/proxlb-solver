"""
Adapter to convert ProxLB internal data structures to Solver models.

This module provides the 'bridge' from the existing ProxLB code base 
to the new CP-SAT solver. It translates the nested dictionary structure 
(proxlb_data) into the strictly typed Cluster, Node, and VM objects.
"""

from __future__ import annotations
from typing import Dict, Any, List, Set
from collections import defaultdict
from .models import Cluster, Node, VM, Constraints, Balancing, Expect

def from_proxlb_data(proxlb_data: Dict[str, Any]) -> Cluster:
    """
    Main conversion function. 
    
    Expects a dictionary with 'meta', 'nodes', 'guests', 'pools', 
    'ha_rules', and 'groups' keys.
    """
    meta = proxlb_data.get("meta", {})
    balancing_cfg = meta.get("balancing", {})
    
    # 1. Map Balancing Configuration
    balancing = Balancing(
        method=balancing_cfg.get("method", "memory"),
        balanciness=balancing_cfg.get("balanciness", 3),
        cpu_overcommit=balancing_cfg.get("cpu_overcommit", 2.0),
        # Default safety margins for ProxLB
        max_node_inflow=balancing_cfg.get("max_node_inflow", 1),
        max_parallel_migrations=balancing_cfg.get("max_parallel_migrations")
    )

    # 2. Map Nodes
    nodes = []
    for name, nd in proxlb_data.get("nodes", {}).items():
        # Mapping generic disk_free to a 'local' pool for simulation stability
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

    # 3. Map Guests (VMs/Containers) and extract implicit constraints
    vms = []
    # rules: {(name, origin): [vm_names]}
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
            disks={}, # We skip specific disk pools in simulation for now
            priority=gd.get("priority", 2),
            vm_type=gd.get("type", "vm")
        ))

        # Map group memberships with origin tracking
        # PVE Native HA Rules
        for rule in gd.get("ha_rules", []):
            rule_id = rule["rule"]
            if rule["type"] == "affinity":
                affinity_map[(rule_id, "pve")].append(name)
            else:
                anti_affinity_map[(rule_id, "pve")].append(name)

        # ProxLB Tags
        for tag in gd.get("tags", []):
            if tag.startswith("plb_affinity"):
                affinity_map[(tag, "plb")].append(name)
            elif tag.startswith("plb_anti_affinity"):
                anti_affinity_map[(tag, "plb")].append(name)
        
        # ProxLB Pools
        for pool in gd.get("pools", []):
            pool_cfg = meta.get("balancing", {}).get("pools", {}).get(pool)
            if pool_cfg:
                if pool_cfg.get("type") == "affinity":
                    affinity_map[(pool, "plb")].append(name)
                elif pool_cfg.get("type") == "anti-affinity":
                    anti_affinity_map[(pool, "plb")].append(name)
            
        # Map explicit pins
        if gd.get("node_relationships"):
            pin_rules.append({"vm": name, "nodes": gd["node_relationships"]})
            
        # Map ignore flags
        if gd.get("ignore"): ignore_list.append(name)

    # 4. Filter and build Constraints object
    # Only keep groups with more than 1 member
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
