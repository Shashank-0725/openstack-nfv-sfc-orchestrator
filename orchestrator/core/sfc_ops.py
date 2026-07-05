"""
SFC chain management.
Dynamically creates/deletes:
  - port pairs
  - port pair groups
  - flow classifiers
  - port chains

OpenStack handles:
  transparent packet steering

VNFs themselves now run as:
  containers inside thin runtime VMs
"""

import subprocess

from core.openstack_ops import get_port_id


# ──────────────────────────────────────────────────────────────────────────────
# Shell Runner
# ──────────────────────────────────────────────────────────────────────────────

def _run(cmd):

    try:

        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )

        if r.returncode != 0:

            print(f"[SFC] ERROR")
            print(f"[SFC] CMD: {cmd}")
            print(f"[SFC] STDERR: {r.stderr.strip()}")

            return False, r.stderr.strip()

        return True, r.stdout.strip()

    except subprocess.TimeoutExpired:

        print(f"[SFC] TIMEOUT: {cmd}")

        return False, "TIMEOUT"

    except Exception as e:

        print(f"[SFC] EXCEPTION: {e}")

        return False, str(e)


# ──────────────────────────────────────────────────────────────────────────────
# Port Pairs
# ──────────────────────────────────────────────────────────────────────────────

def create_port_pair(
    name,
    in_port_name,
    out_port_name
):

    in_id = get_port_id(in_port_name)

    out_id = get_port_id(out_port_name)

    if not in_id or not out_id:

        print(
            f"[SFC] Port lookup failed "
            f"for {name}"
        )

        return False

    ok, _ = _run(
        f"openstack sfc port pair create "
        f"--ingress {in_id} "
        f"--egress {out_id} "
        f"{name}"
    )

    print(
        f"[SFC] Port pair "
        f"{'created' if ok else 'FAILED'}: "
        f"{name}"
    )

    return ok


def delete_port_pair(name):

    _run(
        f"openstack sfc port pair delete {name}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Port Pair Groups
# ──────────────────────────────────────────────────────────────────────────────

def create_port_pair_group(
    name,
    port_pair_names
):

    pairs = ' '.join(
        f"--port-pair {p}"
        for p in port_pair_names
    )

    ok, _ = _run(
        f"openstack sfc port pair group create "
        f"{pairs} {name}"
    )

    print(
        f"[SFC] Port pair group "
        f"{'created' if ok else 'FAILED'}: "
        f"{name}"
    )

    return ok


def delete_port_pair_group(name):

    _run(
        f"openstack sfc port pair group delete {name}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Port Chains
# ──────────────────────────────────────────────────────────────────────────────

def create_port_chain(
    name,
    ppg_names,
    flow_classifier_names
):

    ppgs = ' '.join(
        f"--port-pair-group {p}"
        for p in ppg_names
    )

    fcs = ' '.join(
        f"--flow-classifier {f}"
        for f in flow_classifier_names
    )

    ok, _ = _run(
        f"openstack sfc port chain create "
        f"{ppgs} "
        f"{fcs} "
        f"{name}"
    )

    print(
        f"[SFC] Port chain "
        f"{'created' if ok else 'FAILED'}: "
        f"{name}"
    )

    return ok


def delete_port_chain(name):

    _run(
        f"openstack sfc port chain delete {name}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Flow Classifiers
# ──────────────────────────────────────────────────────────────────────────────

def create_flow_classifier(
    name,
    client_port_name,
    dst_port=80
):
    """
    Match:
      TCP traffic to dst_port
      originating from client port
    """

    client_port_id = get_port_id(
        client_port_name
    )

    if not client_port_id:

        print(
            f"[SFC] Client port lookup failed: "
            f"{client_port_name}"
        )

        return False

    ok, _ = _run(
        f"openstack sfc flow classifier create "
        f"--ethertype IPv4 "
        f"--protocol tcp "
        f"--destination-port {dst_port}:{dst_port} "
        f"--logical-source-port {client_port_id} "
        f"{name}"
    )

    print(
        f"[SFC] Flow classifier "
        f"{'created' if ok else 'FAILED'}: "
        f"{name}"
    )

    return ok


def delete_flow_classifier(name):

    _run(
        f"openstack sfc flow classifier delete {name}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Build Full Chain
# ──────────────────────────────────────────────────────────────────────────────

def build_chain(chain_spec):
    """
    Build complete SFC chain.

    chain_spec example:

    {
        'chain_name': 'chain-1',

        'client_port': 'client-vm-port',

        'vnf_groups': [

            {
                'type': 'firewall',

                'instances': [
                    {
                        'name': 'firewall-1',
                        'in_port': 'fw1-in',
                        'out_port': 'fw1-out'
                    }
                ]
            },

            {
                'type': 'ids',

                'instances': [
                    {
                        'name': 'ids-1',
                        'in_port': 'ids1-in',
                        'out_port': 'ids1-out'
                    }
                ]
            }
        ]
    }

    Returns:
      created resource list
      for cleanup later
    """

    created = {
        'port_pairs': [],
        'port_pair_groups': [],
        'flow_classifiers': [],
        'port_chains': [],
    }

    ppg_names = []

    # ──────────────────────────────────────────────────────────────────
    # Build Port Pairs + Groups
    # ──────────────────────────────────────────────────────────────────

    for group in chain_spec['vnf_groups']:

        vtype = group['type']

        pp_names = []

        for inst in group['instances']:

            pp_name = f"pp-{inst['name']}"

            ok = create_port_pair(
                pp_name,
                inst['in_port'],
                inst['out_port']
            )

            if not ok:

                raise RuntimeError(
                    f"Failed creating "
                    f"port pair: {pp_name}"
                )

            pp_names.append(pp_name)

            created['port_pairs'].append(
                pp_name
            )

        ppg_name = f"ppg-{vtype}"

        ok = create_port_pair_group(
            ppg_name,
            pp_names
        )

        if not ok:

            raise RuntimeError(
                f"Failed creating "
                f"port pair group: {ppg_name}"
            )

        ppg_names.append(ppg_name)

        created['port_pair_groups'].append(
            ppg_name
        )

    # ──────────────────────────────────────────────────────────────────
    # Flow Classifier
    # ──────────────────────────────────────────────────────────────────

    fc_name = f"fc-{chain_spec['chain_name']}"

    ok = create_flow_classifier(
        fc_name,
        chain_spec['client_port']
    )

    if not ok:

        raise RuntimeError(
            f"Failed creating "
            f"flow classifier: {fc_name}"
        )

    created['flow_classifiers'].append(
        fc_name
    )

    # ──────────────────────────────────────────────────────────────────
    # Port Chain
    # ──────────────────────────────────────────────────────────────────

    ok = create_port_chain(
        chain_spec['chain_name'],
        ppg_names,
        [fc_name]
    )

    if not ok:

        raise RuntimeError(
            f"Failed creating "
            f"port chain: "
            f"{chain_spec['chain_name']}"
        )

    created['port_chains'].append(
        chain_spec['chain_name']
    )

    print(
        f"[SFC] Chain ready: "
        f"{chain_spec['chain_name']}"
    )

    return created


# ──────────────────────────────────────────────────────────────────────────────
# Teardown
# ──────────────────────────────────────────────────────────────────────────────

def teardown_chain(created_resources):
    """
    Delete all SFC resources
    in reverse order.
    """

    for name in created_resources.get(
        'port_chains',
        []
    ):

        delete_port_chain(name)

    for name in created_resources.get(
        'flow_classifiers',
        []
    ):

        delete_flow_classifier(name)

    for name in created_resources.get(
        'port_pair_groups',
        []
    ):

        delete_port_pair_group(name)

    for name in created_resources.get(
        'port_pairs',
        []
    ):

        delete_port_pair(name)

    print("[SFC] Teardown complete")

