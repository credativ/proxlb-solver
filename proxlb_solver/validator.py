"""Validator for merging ProxLB and Proxmox HA rules and detecting conflicts."""

from __future__ import annotations
from typing import Dict, List, Set, Any
from .models import Cluster, Constraints

class RuleConflictError(Exception):
    """Raised when conflicting placement rules are detected."""
    pass

def validate_and_merge_constraints(cluster: Cluster) -> Constraints:
    """
    Merge all constraint sources and detect logical conflicts for HARD rules.
    """
    vms = {vm.name for vm in cluster.vms}
    orig_cons = cluster.constraints
    
    # 1. Merge HARD Affinity Groups (Transitive)
    merged_affinity: List[Set[str]] = []
    for rule in orig_cons.affinity:
        if not rule.get("hard", True): continue
        members = set(rule["vms"]) & vms
        if not members:
            continue
        overlapping = [g for g in merged_affinity if g & members]
        if not overlapping:
            merged_affinity.append(members)
        else:
            new_group = members
            for g in overlapping:
                new_group.update(g)
                merged_affinity.remove(g)
            merged_affinity.append(new_group)

    # 2. Check for HARD Affinity vs HARD Anti-Affinity conflicts
    for aa_rule in orig_cons.anti_affinity:
        if not aa_rule.get("hard", True): continue
        aa_members = set(aa_rule["vms"]) & vms
        for aff_group in merged_affinity:
            conflict_vms = aff_group & aa_members
            if len(conflict_vms) > 1:
                raise RuleConflictError(
                    f"Conflict in rule '{aa_rule.get('name', 'unknown')}': "
                    f"VMs {conflict_vms} are in a hard affinity group but also "
                    f"marked as hard anti-affine to each other."
                )

    # 3. Pinning Conflict Detection (Always Hard)
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

    # 4. HARD Affinity-aware Pinning Intersection
    for aff_group in merged_affinity:
        group_allowed_nodes: Set[str] | None = None
        for vm in aff_group:
            if vm in effective_pins:
                if group_allowed_nodes is None:
                    group_allowed_nodes = set(effective_pins[vm])
                else:
                    group_allowed_nodes &= effective_pins[vm]
        
        if group_allowed_nodes is not None and not group_allowed_nodes:
            raise RuleConflictError(
                f"Hard affinity group {aff_group} is unplaceable: "
                f"the intersection of pinned nodes for its members is empty."
            )

    return orig_cons
