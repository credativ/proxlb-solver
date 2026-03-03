"""
Exporter to extract proxlb_data from a live cluster using ProxLB logic.
Run this script from within the ProxLB source directory.
"""
import sys
import os
import yaml
import json
from pathlib import Path

# Add current directory to path so ProxLB modules find each other
sys.path.append(os.getcwd())

try:
    from proxlb.utils.config_parser import ConfigParser
    from proxlb.utils.proxmox_api import ProxmoxApi
    from proxlb.models.nodes import Nodes
    from proxlb.models.guests import Guests
    from proxlb.models.ha_rules import HaRules
    from proxlb.models.pools import Pools
    from proxlb.models.features import Features
    from proxlb.models.groups import Groups
    from proxlb.utils.proxlb_data import ProxLbData
except ImportError as e:
    print(f"Error: Could not import ProxLB modules. Ensure you are in the 'ProxLB' directory.")
    print(f"Details: {e}")
    sys.exit(1)

def export_data(config_path, output_path):
    if not os.path.exists(config_path):
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)

    print(f"Loading Config...")
    config_parser = ConfigParser(Path(config_path))
    proxlb_config = config_parser.get_config()

    print(f"Connecting to Proxmox API...")
    proxmox_api = ProxmoxApi(proxlb_config)

    print("Gathering Cluster Data...")
    nodes = Nodes.get_nodes(proxmox_api, proxlb_config)
    meta = Features.validate_any_non_pve9_node(proxlb_config, nodes)
    pools = Pools.get_pools(proxmox_api)
    ha_rules = HaRules.get_ha_rules(proxmox_api, meta)
    guests = Guests.get_guests(proxmox_api, pools, ha_rules, nodes, proxlb_config)
    groups = Groups.get_groups(guests, nodes)

    proxlb_data = ProxLbData(
        meta=meta,
        nodes=nodes,
        guests=guests,
        pools=pools,
        ha_rules=ha_rules,
        groups=groups,
    )

    with open(output_path, 'w') as f:
        # Pydantic v2 support
        if hasattr(proxlb_data, "model_dump_json"):
            f.write(proxlb_data.model_dump_json(indent=2, by_alias=True))
        else:
            f.write(proxlb_data.json(indent=2, by_alias=True))
    print(f"Success: Data exported to {output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 export_proxlb_data.py <proxlb_config.yaml> <output_dump.json>")
        sys.exit(1)
    export_data(sys.argv[1], sys.argv[2])
