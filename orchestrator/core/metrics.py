"""
Collect live metrics from all VNFs and nodes.
Called by dashboard polling endpoint every 5 seconds.
"""

import subprocess
import json
import requests
from config import SSH_USER, SSH_KEY, SSH_OPTS
from core.openstack_ops import get_node_metrics

def get_vnf_cpu(float_ip) -> float:
    """Get total CPU% from a VNF via SSH."""
    cmd = (
        f"ssh {SSH_OPTS} -i {SSH_KEY} {SSH_USER}@{float_ip} "
        f"\"python3 -c \\\"import time; "
        f"l1=open('/proc/stat').readline().split(); time.sleep(1); "
        f"l2=open('/proc/stat').readline().split(); "
        f"i1=int(l1[4]); i2=int(l2[4]); "
        f"t1=sum(int(x) for x in l1[1:]); t2=sum(int(x) for x in l2[1:]); "
        f"print(f'{{100*(1-(i2-i1)/(t2-t1)):.1f}}')\\\" 2>/dev/null\""
    )
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                          text=True, timeout=8)
        return float(r.stdout.strip())
    except:
        return -1.0

def get_ids_metrics(float_ip) -> dict:
    """Get IDS blocked IPs from metrics API on port 8080."""
    try:
        r = requests.get(f"http://{float_ip}:8080/metrics", timeout=5)
        return r.json()
    except:
        return {'blocked_ips': {}, 'total_blocked': 0}

def get_monitor_metrics(float_ip) -> dict:
    """Get traffic log from monitor API on port 8080."""
    try:
        r = requests.get(f"http://{float_ip}:8080/metrics", timeout=5)
        return r.json()
    except:
        return {'recent': [], 'total_packets': 0, 'top_sources': {}}

def collect_all_metrics(deployment_state) -> dict:
    """
    Collect all metrics for dashboard.
    deployment_state contains active VNF info.
    """
    metrics = {
        'nodes':       get_node_metrics(),
        'vnfs':        {},
        'ids_data':    {},
        'monitor_data': {},
    }
    
    for vnf_id, info in deployment_state.get('vnfs', {}).items():
        float_ip  = info.get('float_ip')
        vnf_type  = info.get('type')
        
        if not float_ip:
            continue
        
        cpu = get_vnf_cpu(float_ip)
        metrics['vnfs'][vnf_id] = {
            'cpu':      cpu,
            'type':     vnf_type,
            'node':     info.get('node'),
            'float_ip': float_ip,
            'status':   'active' if cpu >= 0 else 'unreachable',
        }
        
        if vnf_type == 'ids':
            metrics['ids_data'][vnf_id] = get_ids_metrics(float_ip)
        
        if vnf_type == 'monitor':
            metrics['monitor_data'][vnf_id] = get_monitor_metrics(float_ip)
    
    return metrics
