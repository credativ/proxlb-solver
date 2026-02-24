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
    from utils.proxmox_api import ProxmoxApi
    from models.nodes import Nodes
    from models.guests import Guests
    from models.ha_rules import HaRules
    from models.pools import Pools
    from models.features import Features
    from models.groups import Groups
except ImportError as e:
    print(f"Error: Could not import ProxLB modules. Ensure you are in the 'proxlb' subdirectory of ProxLB.")
    print(f"Details: {e}")
    sys.exit(1)

def export_data(config_path, output_path):
    if not os.path.exists(config_path):
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    print(f"Connecting to Proxmox API...")
    proxmox_api = ProxmoxApi(config)
    
    print("Gathering Cluster Data...")
    nodes = Nodes.get_nodes(proxmox_api, config)
    meta = {"meta": config}
    meta = Features.validate_any_non_pve9_node(meta, nodes)
    pools = Pools.get_pools(proxmox_api)
    ha_rules = HaRules.get_ha_rules(proxmox_api, meta)
    guests = Guests.get_guests(proxmox_api, pools, ha_rules, nodes, meta, config)
    groups = Groups.get_groups(guests, nodes)

    proxlb_data = {**meta, **nodes, **guests, **pools, **ha_rules, **groups}
    
    with open(output_path, 'w') as f:
        json.dump(proxlb_data, f, indent=2)
    print(f"Success: Data exported to {output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 export_proxlb_data.py <proxlb_config.yaml> <output_dump.json>")
        sys.exit(1)
    export_data(sys.argv[1], sys.argv[2])
