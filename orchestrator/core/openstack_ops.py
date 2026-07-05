id="osfull1"
"""
OpenStack SDK wrapper.
VM CRUD, port management, node metrics.
"""

import openstack
import time
import subprocess
import json

from config import (
    AUTH,
    IMAGE_NAME,
    CLIENT_IMAGE,
    SERVER_IMAGE,
    FLAVOR_NAME,
    INT_NET,
    EXT_NET,
    SSH_OPTS,
    SSH_USER,
    SSH_KEY
)

_conn = None


# ──────────────────────────────────────────────────────────────────────────────
# OpenStack Connection
# ──────────────────────────────────────────────────────────────────────────────

def get_conn():

    global _conn

    if _conn is None:
        _conn = openstack.connect(**AUTH)

    return _conn


# ──────────────────────────────────────────────────────────────────────────────
# Node Metrics
# ──────────────────────────────────────────────────────────────────────────────

def run_cmd(cmd):

    return subprocess.check_output(
        cmd,
        shell=True
    ).decode().strip()


def get_node_metrics():

    metrics = {}

    hosts_raw = run_cmd(
        "openstack hypervisor list -f value -c 'Hypervisor Hostname'"
    )

    hosts = hosts_raw.splitlines()

    conn = get_conn()

    servers = list(
        conn.compute.servers(all_projects=True)
    )

    for host in hosts:

        raw = run_cmd(
            f"openstack hypervisor show {host} -f json"
        )

        data = json.loads(raw)

        # Count VNFs on this node
        # Exclude infrastructure VMs
        vnf_count = sum(
            1 for s in servers
            if getattr(s, 'hypervisor_hostname', '') == host
            and s.name not in ('client-vm', 'server-vm')
        )

        load_str = data.get("load_average", "0,0,0")

        try:
            load_1m = float(
                load_str.split(",")[0].strip()
            )

        except Exception:
            load_1m = 0.0

        cpu_util = round(
            min((load_1m / 2) * 100, 100),
            2
        )

        ram_util = round(
            min(vnf_count * 12, 100),
            2
        )

        ram_free_mb = max(
            0,
            24000 - (vnf_count * 2048)
        )

        metrics[host] = {
            'cpu_util':     cpu_util,
            'ram_util':     ram_util,
            'ram_free_mb':  ram_free_mb,
            'vcpus_used':   vnf_count,
            'vcpus_total':  6,
            'vnf_count':    vnf_count,
            'load_average': load_str,
            'state':        data.get('state'),
            'status':       data.get('status'),
            'host_ip':      data.get('host_ip'),
            'uptime':       data.get('uptime'),
            'users':        data.get('users'),
        }

    return metrics


# ──────────────────────────────────────────────────────────────────────────────
# Image Resolution
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_image(image_id_or_name):
    """
    Resolve image by ID or name.
    Falls back to IMAGE_NAME if not found.
    """

    conn = get_conn()

    # Try direct ID lookup first
    try:

        image = conn.compute.get_image(
            image_id_or_name
        )

        if image:
            print(
                f"[OS] Using image: "
                f"{image.name} "
                f"({image_id_or_name[:8]}...)"
            )

            return image

    except Exception:
        pass

    # Fallback to name lookup
    image = conn.compute.find_image(
        image_id_or_name
    )

    if image:

        print(f"[OS] Using image: {image.name}")

        return image

    print(
        f"[OS] Image not found: "
        f"{image_id_or_name} "
        f"-- falling back to {IMAGE_NAME}"
    )

    return conn.compute.find_image(IMAGE_NAME)


# ──────────────────────────────────────────────────────────────────────────────
# Port Management
# ──────────────────────────────────────────────────────────────────────────────

def create_port(name):
    """
    Create Neutron port on int-net
    with port security disabled.
    """

    conn = get_conn()

    network = conn.network.find_network(INT_NET)

    port = conn.network.create_port(
        name=name,
        network_id=network.id
    )

    conn.network.update_port(
        port.id,
        security_groups=[],
        port_security_enabled=False,
    )

    print(
        f"[OS] Port created: "
        f"{name} → "
        f"{port.fixed_ips[0]['ip_address']}"
    )

    return port


def delete_port(name):

    conn = get_conn()

    port = conn.network.find_port(name)

    if port:

        conn.network.delete_port(port.id)

        print(f"[OS] Port deleted: {name}")


def get_port_id(name):

    conn = get_conn()

    port = conn.network.find_port(name)

    return port.id if port else None


def get_port_ip(name):

    conn = get_conn()

    port = conn.network.find_port(name)

    if port and port.fixed_ips:
        return port.fixed_ips[0]['ip_address']

    return None


# ──────────────────────────────────────────────────────────────────────────────
# VM Management
# ──────────────────────────────────────────────────────────────────────────────

def create_vnf_vm(
    vm_name,
    in_port_name,
    out_port_name,
    az,
    image_name=None
):
    """
    Create thin runtime VM for
    containerized VNFs.

    VM contains:
      - Docker runtime
      - IP forwarding enabled
      - insecure registry config

    Actual VNF functionality
    runs as containers.
    """

    conn = get_conn()

    flavor = conn.compute.find_flavor(
        FLAVOR_NAME
    )

    # Thin runtime image
    image = _resolve_image(
        image_name or IMAGE_NAME
    )

    in_port = conn.network.find_port(
        in_port_name
    )

    out_port = conn.network.find_port(
        out_port_name
    )

    if not in_port or not out_port:

        raise RuntimeError(
            f"Port lookup failed for {vm_name}"
        )

    server = conn.compute.create_server(
        name=vm_name,
        image_id=image.id,
        key_name='mykey',
        security_groups=[{'name': 'test-sg'}],
        flavor_id=flavor.id,
        networks=[
            {'port': in_port.id},
            {'port': out_port.id},
        ],
        availability_zone=az,
    )

    print(
        f"[OS] Creating VM {vm_name} "
        f"(image={image.name}) "
        f"on {az}..."
    )

    conn.compute.wait_for_server(
        server,
        status='ACTIVE',
        wait=180
    )

    print(f"[OS] VM {vm_name} ACTIVE")

    return server.id


def create_regular_vm(
    vm_name,
    az,
    network=None,
    image_name=None
):
    """
    Create regular VM
    (client/server).
    """

    conn = get_conn()

    image = _resolve_image(
        image_name or IMAGE_NAME
    )

    flavor = conn.compute.find_flavor(
        FLAVOR_NAME
    )

    net_name = network or INT_NET

    network_obj = conn.network.find_network(
        net_name
    )

    server = conn.compute.create_server(
        name=vm_name,
        image_id=image.id,
        key_name='mykey',
        security_groups=[{'name': 'test-sg'}],
        flavor_id=flavor.id,
        networks=[{'uuid': network_obj.id}],
        availability_zone=az,
    )

    conn.compute.wait_for_server(
        server,
        status='ACTIVE',
        wait=120
    )

    print(
        f"[OS] VM {vm_name} ACTIVE "
        f"on {az} "
        f"(image={image.name})"
    )

    return server.id


def assign_floating_ip(server_name):
    """
    Create and attach floating IP.
    """

    conn = get_conn()

    server = conn.compute.find_server(
        server_name
    )

    if not server:
        raise RuntimeError(
            f"Server not found: {server_name}"
        )

    ports = list(
        conn.network.ports(
            device_id=server.id
        )
    )

    if not ports:
        raise RuntimeError(
            f"No ports found for {server_name}"
        )

    ext_net = conn.network.find_network(
        EXT_NET
    )

    fip = conn.network.create_ip(
        floating_network_id=ext_net.id,
        port_id=ports[0].id,
    )

    print(
        f"[OS] {server_name} → "
        f"{fip.floating_ip_address}"
    )

    return fip.floating_ip_address


def delete_vm(server_name):
    """
    Delete VM and release FIPs.
    """

    conn = get_conn()

    server = conn.compute.find_server(
        server_name
    )

    if not server:

        print(f"[OS] VM not found: {server_name}")

        return False

    try:

        server = conn.compute.get_server(
            server.id
        )

        # Release floating IPs
        for addr_list in server.addresses.values():

            for addr in addr_list:

                if addr.get('OS-EXT-IPS:type') == 'floating':

                    ip_addr = addr['addr']

                    fips = list(
                        conn.network.ips(
                            floating_ip_address=ip_addr
                        )
                    )

                    for fip in fips:

                        try:

                            conn.compute.remove_floating_ip_from_server(
                                server.id,
                                ip_addr
                            )

                            conn.network.delete_ip(
                                fip.id
                            )

                            print(
                                f"[OS] Released floating IP "
                                f"{ip_addr}"
                            )

                        except Exception as e:

                            print(
                                f"[OS] Failed releasing "
                                f"{ip_addr}: {e}"
                            )

        conn.compute.delete_server(server.id)

        conn.compute.wait_for_delete(server)

        print(f"[OS] Deleted VM: {server_name}")

        return True

    except Exception as e:

        print(
            f"[OS] Failed deleting VM "
            f"{server_name}: {e}"
        )

        return False


def list_servers():

    conn = get_conn()

    return [
        {
            'name':   s.name,
            'status': s.status,
            'host':   getattr(
                s,
                'hypervisor_hostname',
                'unknown'
            ),
            'ips': [
                ip['addr']
                for addrs in s.addresses.values()
                for ip in addrs
            ],
        }
        for s in conn.compute.servers(
            all_projects=True
        )
    ]


# ──────────────────────────────────────────────────────────────────────────────
# SSH Readiness
# ──────────────────────────────────────────────────────────────────────────────

def wait_ssh(float_ip, max_wait=120):
    """
    Poll SSH until VM is ready.
    """

    print(f"[SSH] Waiting for {float_ip}...")

    for i in range(max_wait // 5):

        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-i", SSH_KEY,
            f"{SSH_USER}@{float_ip}",
            "echo SSH_OK",
        ]

        try:

            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15
            )

            if (
                r.returncode == 0
                and "SSH_OK" in r.stdout
            ):

                print(
                    f"[SSH] {float_ip} "
                    f"ready after {i*5}s"
                )

                return True

        except Exception as e:

            print(f"[SSH] Retry {float_ip}: {e}")

        time.sleep(5)

    print(
        f"[SSH] {float_ip} "
        f"timeout after {max_wait}s"
    )

    return False

