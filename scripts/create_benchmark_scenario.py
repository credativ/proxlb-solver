#!/usr/bin/env python3
"""
Benchmark Scenario Generator for VM Placement
==============================================
Generates a hard VM placement test case in YAML format using Falkenauer-style
bin packing. Items are constructed by partitioning each bin's target fill,
then shuffled to hide the solution — a valid packing always exists.

All VMs start on node-spare-init. Strict pin constraints force every VM
onto a real node, so the solver must find a valid bin packing from scratch.

Usage:
  python create_benchmark_scenario.py \\
      --num-bins 10 --bin-capacity 100 \\
      --min-group-size 3 --max-group-size 6 \\
      --min-vm-size 5 --max-vm-size 20 \\
      --output scenario.yaml
"""

import random
import argparse
import textwrap
from typing import TypedDict


class ScenarioMetadata(TypedDict):
    num_bins: int
    bin_capacity: int
    num_vms: int
    min_group_size: int
    max_group_size: int
    min_vm_size: int
    max_vm_size: int | None
    seed: int


# ---------------------------------------------------------------------------
# Bin packing instance generator (Falkenauer-style)
# ---------------------------------------------------------------------------

def _random_partition(
    total: int,
    k: int,
    rng: random.Random,
    min_size: int = 1,
    max_size: int | None = None,
) -> list[int]:
    """
    Partition total into exactly k positive integers, each within
    [min_size, max_size].
    """
    if max_size is None:
        max_size = total

    # Start with each part at min_size, then distribute the remainder
    parts = [min_size] * k
    remainder = total - k * min_size

    for i in range(k - 1):
        headroom = min(max_size - parts[i], remainder)
        if headroom <= 0:
            continue
        add = rng.randint(0, headroom)
        parts[i] += add
        remainder -= add

    parts[-1] += remainder

    # If last part exceeds max_size, rebalance by stealing from others
    while parts[-1] > max_size:
        excess = parts[-1] - max_size
        parts[-1] = max_size
        for i in range(k - 1):
            give = min(excess, max_size - parts[i])
            parts[i] += give
            excess -= give
            if excess == 0:
                break

    rng.shuffle(parts)
    return parts


def generate_vms(
    num_bins: int,
    bin_capacity: int,
    min_group_size: int,
    max_group_size: int,
    min_vm_size: int,
    max_vm_size: int | None,
    rng: random.Random,
) -> tuple[list[int], list[int]]:
    """
    Build VM sizes by construction: each bin gets a group whose sizes sum to
    bin_capacity, with each VM size within [min_vm_size, max_vm_size].

    Returns (item_sizes, ground_truth_node_assignment).
    """
    effective_max_vm = max_vm_size if max_vm_size is not None else bin_capacity
    max_feasible_k = bin_capacity // min_vm_size
    min_feasible_k = (bin_capacity + effective_max_vm - 1) // effective_max_vm
    clamped_min = max(min_group_size, min_feasible_k)
    clamped_max = min(max_group_size, max_feasible_k)
    if clamped_min > clamped_max:
        raise ValueError(
            f"Cannot partition bin_capacity={bin_capacity} into "
            f"[{min_group_size}, {max_group_size}] pieces each within "
            f"[{min_vm_size}, {effective_max_vm}]. "
            f"Feasible group size range is [{min_feasible_k}, {max_feasible_k}]."
        )

    groups: list[list[int]] = []
    for bin_idx in range(num_bins):
        group_size = rng.randint(clamped_min, clamped_max)
        groups.append(_random_partition(bin_capacity, group_size, rng,
                                        min_size=min_vm_size, max_size=max_vm_size))

    item_sizes: list[int] = []
    ground_truth: list[int] = []
    for bin_idx, group in enumerate(groups):
        for s in group:
            item_sizes.append(s)
            ground_truth.append(bin_idx)

    combined = list(zip(item_sizes, ground_truth))
    rng.shuffle(combined)
    shuffled_sizes, shuffled_ground_truth = zip(*combined)
    return list(shuffled_sizes), list(shuffled_ground_truth)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _header(
    name: str,
    description: str,
    metadata: ScenarioMetadata | None,
    ground_truth_lines: list[str] | None = None,
) -> list[str]:
    lines = [f'name: "{name}"']
    desc_wrapped = textwrap.fill(description, width=72, subsequent_indent='  ')
    if '\n' in desc_wrapped:
        lines += ['description: >'] + [f'  {l}' for l in desc_wrapped.splitlines()]
    else:
        lines.append(f'description: "{description}"')
    if metadata:
        lines += ['', '# Generation metadata']
        lines += [f'#   {k}: {v}' for k, v in metadata.items()]
    if ground_truth_lines:
        lines += ['', '# Ground truth solution']
        lines += ground_truth_lines
    lines += ['', 'balancing:', '  method: memory']
    return lines


# ---------------------------------------------------------------------------
# YAML builder
# ---------------------------------------------------------------------------

def build_yaml(
    name: str,
    description: str,
    num_bins: int,
    bin_capacity: int,
    item_sizes: list[int],
    ground_truth: list[int],
    metadata: ScenarioMetadata | None = None,
) -> str:
    num_vms = len(item_sizes)
    total_size = sum(item_sizes)

    gt_lines = [
        f'#   node-{node_idx} [{sum(item_sizes[i] for i, n in enumerate(ground_truth) if n == node_idx)}/{bin_capacity}]: '
        + ', '.join(f'vm-{i}({item_sizes[i]})' for i, n in enumerate(ground_truth) if n == node_idx)
        for node_idx in range(num_bins)
    ]
    lines = _header(name, description, metadata, ground_truth_lines=gt_lines)

    lines += ['', 'nodes:']
    lines += [
        '  node-spare-init:',
        f'    cpu_total: {total_size}',
        f'    memory_total_gb: {total_size}',
    ]
    for i in range(num_bins):
        lines += [
            f'  node-{i}:',
            f'    cpu_total: {bin_capacity}',
            f'    memory_total_gb: {bin_capacity}',
        ]

    lines += ['', 'vms:']
    for idx, size in enumerate(item_sizes):
        lines += [
            f'  vm-{idx}:',
            f'    node: node-spare-init',
            f'    cpu: {size}',
            f'    memory_gb: {size}',
            f'    type: vm',
        ]

    lines += ['', 'constraints:', '  pin:']
    real_nodes = ', '.join(f'node-{i}' for i in range(num_bins))
    for v in range(num_vms):
        lines += [
            f'    - vm: vm-{v}',
            f'      nodes: [{real_nodes}]',
            f'      strict: true',
        ]

    lines += ['', 'expect:', '  feasible: true', '  constraints_satisfied: true']

    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a hard bin packing VM placement benchmark scenario"
    )
    parser.add_argument("--num-bins", type=int, default=10,
                        help="Number of bins (= real nodes)")
    parser.add_argument("--bin-capacity", type=int, default=100,
                        help="Memory capacity of each bin")
    parser.add_argument("--min-group-size", type=int, default=2,
                        help="Min VMs per bin in ground truth")
    parser.add_argument("--max-group-size", type=int, default=5,
                        help="Max VMs per bin in ground truth (higher = harder)")
    parser.add_argument("--min-vm-size", type=int, default=1,
                        help="Minimum size of each VM (default: 1)")
    parser.add_argument("--max-vm-size", type=int, default=None,
                        help="Maximum size of each VM (default: unconstrained)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=str, default="scenario.yaml")

    args = parser.parse_args()

    rng = random.Random(args.seed)

    item_sizes, ground_truth = generate_vms(
        args.num_bins, args.bin_capacity,
        args.min_group_size, args.max_group_size,
        args.min_vm_size, args.max_vm_size, rng,
    )

    meta: ScenarioMetadata = {
        "num_bins": args.num_bins,
        "bin_capacity": args.bin_capacity,
        "num_vms": len(item_sizes),
        "min_group_size": args.min_group_size,
        "max_group_size": args.max_group_size,
        "min_vm_size": args.min_vm_size,
        "max_vm_size": args.max_vm_size,
        "seed": args.seed,
    }
    name = (f"Bin Packing {args.num_bins}x{args.bin_capacity} "
            f"({len(item_sizes)} VMs, seed={args.seed})")
    desc = (f"Falkenauer-style bin packing: {len(item_sizes)} VMs into "
            f"{args.num_bins} bins of capacity {args.bin_capacity}. "
            f"Items constructed by partitioning each bin's capacity, "
            f"then shuffled to hide the solution. "
            f"A valid packing exists by construction.")

    y = build_yaml(name, desc, args.num_bins, args.bin_capacity,
                   item_sizes, ground_truth, metadata=meta)

    with open(args.output, "w") as f:
        f.write(y)
    print(f"Saved: {args.output}  "
          f"(bins={args.num_bins}, capacity={args.bin_capacity}, "
          f"vms={len(item_sizes)})")

    print()
    print("Ground truth solution:")
    node_vms: dict[str, list[str]] = {}
    for idx, node_idx in enumerate(ground_truth):
        node_vms.setdefault(f"node-{node_idx}", []).append(
            f"vm-{idx}({item_sizes[idx]})")
    for node, vms in sorted(node_vms.items()):
        total = sum(int(v.split("(")[1].rstrip(")")) for v in vms)
        print(f"  {node} [{total}/{args.bin_capacity}]: {', '.join(vms)}")


if __name__ == "__main__":
    main()
