"""
Validator for merging ProxLB and Proxmox HA rules and detecting conflicts.

This module acts as a pre-processor for the solver. It validates that the
requested constraints are logically consistent. If two rules contradict
each other (e.g., VM A must be with B, but B must be away from A), this
module raises an error before the solver even starts.
"""

from __future__ import annotations
from typing import Dict, List, Set, Any
from .models import Cluster, Constraints

class RuleConflictError(Exception):
    """Raised when conflicting placement rules are detected."""
    pass

def validate_and_merge_constraints(cluster: Cluster) -> Constraints:
    """
    Analyzes all HARD rules and detects logical deadlocks.

    Why do we do this?
    CP-SAT would just say 'INFEASIBLE' without a helpful explanation. This
    validator can tell the user EXACTLY which rules are clashing.
    """
    vms = {vm.name for vm in cluster.vms}
    orig_cons = cluster.constraints

    # 1. Merge HARD Affinity Groups (Transitivity)
    # If VM A must be with B, and B must be with C, then A must also be with C.
    # We use a simple iterative merging process to group these together.
    merged_affinity: List[Set[str]] = []
    for rule in orig_cons.affinity:
        if not rule.get("hard", True):
            # We skip soft rules here because they can never cause an
            # unsolveable logical conflict (they just get violated).
            continue

        members = set(rule["vms"]) & vms
        if not members:
            continue

        overlapping = [g for g in merged_affinity if g & members]
        if not overlapping:
            merged_affinity.append(members)
        else:
            # Found a chain: merge the new rule into existing groups
            new_group = members
            for g in overlapping:
                new_group.update(g)
                merged_affinity.remove(g)
            merged_affinity.append(new_group)

    # 2. Detect HARD Affinity vs HARD Anti-Affinity conflicts
    # Logic: Two VMs in the same affinity group cannot also be in an
    # anti-affinity group together.
    for aa_rule in orig_cons.anti_affinity:
        if not aa_rule.get("hard", True):
            continue

        aa_members = set(aa_rule["vms"]) & vms
        for aff_group in merged_affinity:
            conflict_vms = aff_group & aa_members
            if len(conflict_vms) > 1:
                raise RuleConflictError(
                    f"Conflict in rule '{aa_rule.get('name', 'unknown')}': "
                    f"VMs {conflict_vms} are forced to be together (affinity) "
                    f"but also forced to be apart (anti-affinity)."
                )

    # 3. Pinning Conflict Detection (Always Hard)
    # A VM cannot be pinned to two nodes that don't overlap.
    effective_pins: Dict[str, Set[str]] = {}
    for pin_rule in orig_cons.pin:
        vm = pin_rule["vm"]
        if vm not in vms:
            continue
        nodes = set(pin_rule["nodes"])
        if vm in effective_pins:
            # VM has multiple pin rules: only the intersection counts
            effective_pins[vm] &= nodes
        else:
            effective_pins[vm] = nodes

        if not effective_pins[vm]:
            raise RuleConflictError(
                f"VM '{vm}' has a pinning conflict: it is pinned to multiple "
                f"node sets that have no common nodes."
            )

    # 4. Affinity-aware Pinning Intersection
    # Logic: If VM A and B must be on the same node (affinity), and A is
    # pinned to Node 1 and B is pinned to Node 2, the group is unplaceable.
    for aff_group in merged_affinity:
        # Check if all pinned nodes in this group have a common target node
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

    # We return the original constraints if everything is fine.
    return orig_cons
