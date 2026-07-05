"""
Flask application — GUI + API + Live Dashboard
Accessible from host machine at http://<controller-ip>:5050
"""

import threading
import json
import time
import subprocess
from flask import Flask, render_template, request, jsonify

from config import (
    FLASK_HOST, FLASK_PORT, CHAIN_ORDER,
    SSH_OPTS, SSH_USER, SSH_KEY, PREFIX_MAP,
    CLIENT_IMAGE, SERVER_IMAGE,
)
from core.openstack_ops import (
    get_conn, get_node_metrics,
    create_port, delete_port, get_port_ip,
    create_vnf_vm, create_regular_vm,
    assign_floating_ip, delete_vm,
    list_servers, wait_ssh,
)
from core.placement  import optimize_placement, explain_placement
from core.vnf_config import configure_vnf, configure_client, configure_server
from core.sfc_ops    import build_chain, teardown_chain
from core.metrics    import collect_all_metrics

app = Flask(__name__)

# ── Global state (single deployment at a time for demo) ──────────────────────
state = {
    'status':     'idle',   # idle | deploying | active | tearing_down
    'progress':   [],       # log messages shown in UI
    'vnfs':       {},       # {vnf_id: {type, node, float_ip, in_ip, out_ip}}
    'sfc':        {},       # created SFC resources
    'client':     {},       # client VM info
    'server':     {},       # server VM info
    'chain_spec': None,
}


def log(msg):
    print(msg)
    state['progress'].append(msg)
    if len(state['progress']) > 100:
        state['progress'] = state['progress'][-100:]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    nodes = get_node_metrics()
    return render_template('index.html', nodes=nodes)


@app.route('/api/nodes')
def api_nodes():
    return jsonify(get_node_metrics())


@app.route('/api/state')
def api_state():
    return jsonify({
        'status':   state['status'],
        'progress': state['progress'][-20:],
        'vnfs': {
            k: {'type': v['type'], 'node': v['node']}
            for k, v in state['vnfs'].items()
        },
    })


@app.route('/api/plan', methods=['POST'])
def api_plan():
    """Generate placement plan without deploying."""
    data      = request.json
    vnf_list  = _build_vnf_list(data)
    nodes     = get_node_metrics()
    placement = optimize_placement(vnf_list)
    explained = explain_placement(placement, nodes)
    return jsonify({'placement': explained, 'nodes': nodes})


@app.route('/api/deploy', methods=['POST'])
def api_deploy():
    """Start full deployment in background thread."""
    if state['status'] != 'idle':
        return jsonify({'error': 'System busy'}), 400
    data = request.json
    threading.Thread(target=_deploy, args=(data,), daemon=True).start()
    return jsonify({'status': 'deploying'})


@app.route('/api/teardown', methods=['POST'])
def api_teardown():
    """Tear down deployment safely."""
    if state['status'] == 'tearing_down':
        return jsonify({'error': 'Teardown already running'}), 400
    threading.Thread(target=_teardown, daemon=True).start()
    return jsonify({'status': 'tearing_down'})


@app.route('/api/metrics')
def api_metrics():
    """Live metrics for dashboard polling."""
    if state['status'] != 'active':
        return jsonify({'status': state['status']})
    metrics             = collect_all_metrics(state)
    metrics['status']   = state['status']
    metrics['chain']    = _build_chain_display()
    metrics['progress'] = state['progress'][-5:]
    return jsonify(metrics)


@app.route('/api/traffic/start', methods=['POST'])
def api_traffic_start():
    """Start HTTP traffic from client to server via ab."""
    if not state.get('client', {}).get('float_ip'):
        return jsonify({'error': 'No client VM'}), 400
    if not state.get('server', {}).get('internal_ip'):
        return jsonify({'error': 'No server VM'}), 400

    client_fip = state['client']['float_ip']
    server_ip  = state['server']['internal_ip']
    rate       = request.json.get('rate', 10)      # requests/second
    duration   = request.json.get('duration', 60)  # seconds

    cmd = (
        f"ssh {SSH_OPTS} -i {SSH_KEY} {SSH_USER}@{client_fip} "
        f"\"nohup ab -n {rate * duration} -c {rate} "
        f"-r http://{server_ip}/ > /tmp/ab.log 2>&1 &\""
    )
    subprocess.run(cmd, shell=True, timeout=15)
    log(f"[Traffic] Started: {rate} req/s for {duration}s → {server_ip}")
    return jsonify({'status': 'started'})


@app.route('/api/traffic/stop', methods=['POST'])
def api_traffic_stop():
    client_fip = state.get('client', {}).get('float_ip')
    if client_fip:
        subprocess.run(
            f"ssh {SSH_OPTS} -i {SSH_KEY} {SSH_USER}@{client_fip} "
            f"\"pkill ab 2>/dev/null; pkill curl 2>/dev/null\"",
            shell=True, timeout=10,
        )
    return jsonify({'status': 'stopped'})


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


# ── Deployment Logic ──────────────────────────────────────────────────────────

def _build_vnf_list(data):
    """Convert user form data to ordered VNF list."""
    vnf_list = []
    for vtype in CHAIN_ORDER:
        count = int(data.get(f'{vtype}_count', 0))
        for i in range(1, count + 1):
            vnf_list.append({'id': f"{vtype}-{i}", 'type': vtype})
    return vnf_list


def _deploy(data):
    state['status']   = 'deploying'
    state['progress'] = []
    state['vnfs']     = {}

    try:
        vnf_list = _build_vnf_list(data)
        if not vnf_list:
            log("ERROR: No VNFs selected")
            state['status'] = 'idle'
            return

        log(f"Deploying {len(vnf_list)} VNFs: {[v['id'] for v in vnf_list]}")

        # ── Ensure client-vm and server-vm exist
        _ensure_base_vms()

        # ── Run placement optimizer
        log("Running placement optimizer...")
        placement = optimize_placement(vnf_list)
        for vnf_id, info in placement.items():
            log(f"  {vnf_id} → {info['node']} (score={info['score']})")

        # ── Deploy each VNF
        chain_vnf_groups = {}

        for vnf in vnf_list:
            vnf_id   = vnf['id']
            vnf_type = vnf['type']
            node     = placement[vnf_id]['node']
            az       = placement[vnf_id]['az']

            # Port naming: use PREFIX_MAP to avoid 'fir' prefix bug
            idx      = vnf_id.split('-')[-1]           # '1', '2', ...
            prefix   = PREFIX_MAP[vnf_type]            # 'fw', 'ids', 'mon'
            in_name  = f"{prefix}{idx}-in"             # e.g. fw1-in
            out_name = f"{prefix}{idx}-out"            # e.g. fw1-out

            log(f"Creating ports: {in_name}, {out_name}")
            in_port  = create_port(in_name)
            out_port = create_port(out_name)
            in_ip    = in_port.fixed_ips[0]['ip_address']
            out_ip   = out_port.fixed_ips[0]['ip_address']

            # ── Create VM using the golden image for this VNF type
            log(f"Creating VM {vnf_id} on {node} (image=vnf-thin-golden)...")

            vm_id = create_vnf_vm(vnf_id, in_name, out_name, az, image_name="vnf-thin-golden")

            # ── Assign floating IP
            float_ip = assign_floating_ip(vnf_id)

            # ── Wait for SSH (golden image boots faster — usually < 30s)
            log(f"Waiting for SSH on {float_ip}...")
            wait_ssh(float_ip)

            # ── Configure VNF (pure config — no downloads)
            log(f"Configuring {vnf_type} on {float_ip}...")

            log(f"[DEBUG] {vnf_id}")
            log(f"[DEBUG] float_ip={float_ip}")
            log(f"[DEBUG] in_ip={in_ip}")
            log(f"[DEBUG] out_ip={out_ip}")

            ok = configure_vnf(vnf_type, float_ip, in_ip, out_ip)
            
            if not ok:
                raise RuntimeError(f"{vnf_id} configuration failed")

            log(f"  OK: {vnf_id}")
            # Store VNF state
            state['vnfs'][vnf_id] = {
                'type':     vnf_type,
                'node':     node,
                'float_ip': float_ip,
                'in_port':  in_name,
                'out_port': out_name,
                'in_ip':    in_ip,
                'out_ip':   out_ip,
                'vm_id':    vm_id,
            }

            # Group VNFs by type for SFC
            if vnf_type not in chain_vnf_groups:
                chain_vnf_groups[vnf_type] = []
            chain_vnf_groups[vnf_type].append({
                'name':     vnf_id,
                'in_port':  in_name,
                'out_port': out_name,
            })

        # ── Build SFC chain
        log("Building SFC chain...")
        vnf_groups = [
            {'type': vtype, 'instances': chain_vnf_groups[vtype]}
            for vtype in CHAIN_ORDER
            if vtype in chain_vnf_groups
        ]

        client_port = _get_client_port_id()
        chain_spec  = {
            'chain_name':  'chain-1',
            'client_port': client_port,
            'vnf_groups':  vnf_groups,
        }
        state['chain_spec'] = chain_spec
        created      = build_chain(chain_spec)
        state['sfc'] = created

        log("✓ Deployment complete!")
        state['status'] = 'active'

    except Exception as e:
        log(f"ERROR: {e}")
        state['status'] = 'idle'
        import traceback
        traceback.print_exc()


def _teardown():
    state['status'] = 'tearing_down'
    log("Tearing down...")

    try:
        if state.get('sfc'):
            log("Removing SFC resources...")
            teardown_chain(state['sfc'])

        for vnf_id in list(state['vnfs'].keys()):
            try:
                log(f"Deleting VM {vnf_id}...")
                delete_vm(vnf_id)
            except Exception as e:
                log(f"Failed deleting VM {vnf_id}: {e}")

        # Wait for Nova to detach interfaces before deleting ports
        time.sleep(10)

        for vnf_id, info in list(state['vnfs'].items()):
            try:
                log(f"Deleting ports for {vnf_id}...")
                delete_port(info['in_port'])
                delete_port(info['out_port'])
            except Exception as e:
                log(f"Failed deleting ports for {vnf_id}: {e}")

        state['vnfs']     = {}
        state['sfc']      = {}
        state['progress'] = []
        log("Teardown complete")

    except Exception as e:
        log(f"Teardown error: {e}")

    finally:
        state['status'] = 'idle'


def _ensure_base_vms():
    """
    Create client-vm and server-vm if they don't exist.
    Uses golden images (CLIENT_IMAGE / SERVER_IMAGE) with pre-installed tools.
    Falls back gracefully via configure_client/configure_server if plain image.
    """
    servers = {s['name']: s for s in list_servers()}

    # ── Client VM
    if 'client-vm' not in servers:
        log("Creating client-vm...")
        create_regular_vm('client-vm', 'nova:openstack-compute1',
                          image_name=CLIENT_IMAGE)
        fip = assign_floating_ip('client-vm')
        wait_ssh(fip)
        configure_client(fip)   # verifies ab present; installs if not
        state['client'] = {'float_ip': fip}
    else:
        log("client-vm already exists")
        for ip in servers['client-vm'].get('ips', []):
            if ip.startswith('192.168.'):
                state['client'] = {'float_ip': ip}
                break

    # ── Server VM
    if 'server-vm' not in servers:
        log("Creating server-vm...")
        create_regular_vm('server-vm', 'nova:openstack-compute1',
                          image_name=SERVER_IMAGE)
        fip = assign_floating_ip('server-vm')
        wait_ssh(fip)
        configure_server(fip)   # verifies nginx running; installs if not
        # Get internal IP
        srv = {s['name']: s for s in list_servers()}['server-vm']
        internal_ip = next(
            (ip for ip in srv['ips'] if ip.startswith('172.')), None
        )
        state['server'] = {'float_ip': fip, 'internal_ip': internal_ip}
    else:
        log("server-vm already exists")
        srv = servers['server-vm']
        for ip in srv.get('ips', []):
            if ip.startswith('192.168.'):
                state['server']['float_ip'] = ip
            elif ip.startswith('172.'):
                state['server']['internal_ip'] = ip


def _get_client_port_id():
    """Get the neutron port ID for client-vm on int-net (172.16.x.x)."""
    conn   = get_conn()
    server = conn.compute.find_server('client-vm')
    ports  = list(conn.network.ports(device_id=server.id))
    for p in ports:
        if p.fixed_ips:
            ip = p.fixed_ips[0]['ip_address']
            if ip.startswith('172.16.'):
                return p.id
    return None


def _build_chain_display():
    """Build chain display data for dashboard."""
    if not state.get('chain_spec'):
        return []
    return [
        {
            'type':      g['type'],
            'count':     len(g['instances']),
            'instances': [i['name'] for i in g['instances']],
        }
        for g in state['chain_spec'].get('vnf_groups', [])
    ]


if __name__ == '__main__':
    print(f"Starting Hybrid OpenStack + Container NFV Platform...")
    print(f"Open in browser: http://192.168.61.200:{FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
