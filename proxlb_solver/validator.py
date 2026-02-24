"""Validator for merging ProxLB and Proxmox HA rules and detecting conflicts."""

from __future__ import annotations
from typing import Dict, List, Set, Any
from .models import Cluster, Constraints

class RuleConflictError(Exception):
    """Raised when conflicting placement rules are detected."""
    pass

def validate_and_merge_constraints(cluster: Cluster) -> Constraints:
    """
    Merge all constraint sources and detect logical conflicts.
    
    Current implementation focuses on:
    1. Transitive Affinity (merging overlapping groups)
    2. Affinity vs Anti-Affinity conflicts
    3. Pinning intersection conflicts within affinity groups
    """
    vms = {vm.name for vm in cluster.vms}
    orig_cons = cluster.constraints
    
    # 1. Merge Affinity Groups (Transitive)
    # Using Disjoint Set Union (DSU) logic or simple iterative merging
    merged_affinity: List[Set[str]] = []
    for rule in orig_cons.affinity:
        members = set(rule["vms"]) & vms
        if not members:
            continue
        
        # Find all existing groups that overlap with this rule
        overlapping = [g for g in merged_affinity if g & members]
        if not overlapping:
            merged_affinity.append(members)
        else:
            # Merge all overlapping groups into one
            new_group = members
            for g in overlapping:
                new_group.update(g)
                merged_affinity.remove(g)
            merged_affinity.append(new_group)

    # 2. Check for Affinity vs Anti-Affinity conflicts
    # Two VMs in the same merged affinity group must not be in the same anti-affinity group
    for aa_rule in orig_cons.anti_affinity:
        aa_members = set(aa_rule["vms"]) & vms
        for aff_group in merged_affinity:
            conflict_vms = aff_group & aa_members
            if len(conflict_vms) > 1:
                raise RuleConflictError(
                    f"Conflict in rule '{aa_rule.get('name', 'unknown')}': "
                    f"VMs {conflict_vms} are in an affinity group but also "
                    f"marked as anti-affine to each other."
                )

    # 3. Pinning Conflict Detection
    # Calculate effective pins for each VM
    effective_pins: Dict[str, Set[str]] = {}
    for pin_rule in orig_cons.pin:
        vm = pin_rule["vm"]
        if vm not in vms:
            continue
        nodes = set(pin_rule["nodes"])
        if vm in effective_pins:
            effective_pins[vm] &= nodes
        else:
            effective_pins[vm] = nodes
        
        if not effective_pins[vm]:
            raise RuleConflictError(f"VM '{vm}' has empty intersection of allowed nodes from multiple pin rules.")

    # 4. Affinity-aware Pinning Intersection
    # All VMs in an affinity group must be able to land on the same node
    for aff_group in merged_affinity:
        # Start with all nodes, then intersect with all pins in the group
        group_allowed_nodes: Set[str] | None = None
        for vm in aff_group:
            if vm in effective_pins:
                if group_allowed_nodes is None:
                    group_allowed_nodes = set(effective_pins[vm])
                else:
                    group_allowed_nodes &= effective_pins[vm]
        
        if group_allowed_nodes is not None and not group_allowed_nodes:
            raise RuleConflictError(
                f"Affinity group {aff_group} is unplaceable: "
                f"the intersection of pinned nodes for its members is empty."
            )

    # Return clean merged constraints
    # (Note: we return the original format but validated/potentially cleaned)
    return orig_cons
