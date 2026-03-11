import json
import urllib3
import re
from proxmoxer import ProxmoxAPI

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def inspect_pve_storage():
    # Credentials
    HOST = "pve.snow-crash.eu"
    PORT = 8006
    USER = "root@pam"
    TOKEN_NAME = "proxlb2"
    TOKEN_VALUE = "a599b0ea-5a7c-4522-8fe8-dd91c0643739"

    proxmox = ProxmoxAPI(
        HOST, port=PORT, user=USER,
        token_name=TOKEN_NAME, token_value=TOKEN_VALUE,
        verify_ssl=False
    )

    print(f"--- Storage Inspection on {HOST} ---")

    # 1. Cluster-wide Storage List
    print("\n[Cluster Storages]")
    try:
        storages = proxmox.storage.get()
        for s in storages:
            stype = s.get('type', 'unknown')
            shared = "yes" if s.get('shared') else "no"
            content = s.get('content', '')
            print(f"Storage: {s['storage']} (Type: {stype}, Shared: {shared}, Content: {content})")
    except Exception as e:
        print(f"  Error fetching cluster storages: {e}")

    # 2. Node-specific Storage Status
    nodes = proxmox.nodes.get()
    for node in nodes:
        node_name = node['node']
        print(f"\n[Storage Status on {node_name}]")
        try:
            node_storages = proxmox.nodes(node_name).storage.get()
            for ns in node_storages:
                if ns.get('active'):
                    total_gb = ns['total'] / (1024**3)
                    used_gb = ns['used'] / (1024**3)
                    print(f"  {ns['storage']}: {used_gb:.1f} / {total_gb:.1f} GB ({ns['avail']/(1024**3):.1f} GB free)")
        except Exception as e:
            print(f"  Error fetching node storage: {e}")

    # 3. VM Disk Analysis (Targeting VMs from earlier)
    if nodes:
        sample_node = nodes[0]['node']
        print(f"\n[VM Disk Mapping on {sample_node}]")
        try:
            vms = proxmox.nodes(sample_node).qemu.get()
            for vm in vms[:3]:
                vmid = vm['vmid']
                name = vm.get('name', 'unknown')
                print(f"VM {vmid} ({name}):")
                config = proxmox.nodes(sample_node).qemu(vmid).config.get()

                # Search for disk keys (ideX, scsiX, virtioX, sataX)
                disk_pattern = re.compile(r'^(?:ide|scsi|virtio|sata)\d+$')
                found_disks = False
                for key, value in config.items():
                    if disk_pattern.match(key):
                        print(f"  - {key}: {value}")
                        found_disks = True
                if not found_disks:
                    print("  - No virtual disks found in config (Cloud-init only?)")
        except Exception as e:
            print(f"  Error mapping VM disks: {e}")

if __name__ == "__main__":
    inspect_pve_storage()
