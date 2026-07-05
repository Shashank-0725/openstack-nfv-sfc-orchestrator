"""
Containerized VNF Runtime Orchestrator
======================================

Thin OpenStack VMs run containerized VNFs:
  - firewall-vnf
  - ids-vnf
  - monitor-vnf

This module:
  1. Detects ingress/egress interfaces dynamically
  2. Pulls the correct VNF container
  3. Launches it via Docker
  4. Verifies runtime success

No startup scripts.
No cloud-init logic.
No package installation.
"""

import subprocess
import time

from config import SSH_USER, SSH_KEY, SSH_OPTS


# ──────────────────────────────────────────────────────────────────────────────
# Registry + Images
# ──────────────────────────────────────────────────────────────────────────────

REGISTRY = "192.168.137.184:5000"

VNF_IMAGES = {
    "firewall": f"{REGISTRY}/firewall-vnf:v2",
    "ids":      f"{REGISTRY}/ids-vnf:v1",
    "monitor":  f"{REGISTRY}/monitor-vnf:v1",
}


# ──────────────────────────────────────────────────────────────────────────────
# SSH Helper
# ──────────────────────────────────────────────────────────────────────────────

def _ssh(float_ip, command, timeout=60):

    try:
        result = subprocess.run(
            [
                "ssh",
                *SSH_OPTS.split(),
                "-i",
                SSH_KEY,
                f"{SSH_USER}@{float_ip}",
                command
            ],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )

        return result.returncode, result.stdout.strip()

    except subprocess.TimeoutExpired:
        print(f"[Config] SSH timeout to {float_ip}")
        return -1, "TIMEOUT"

    except Exception as e:
        print(f"[Config] SSH error to {float_ip}: {e}")
        return -1, str(e)


# ──────────────────────────────────────────────────────────────────────────────
# Detect interface name from IP
# ──────────────────────────────────────────────────────────────────────────────

def detect_interface(float_ip, ip_addr):

    cmd = (
        f"ip -o addr show | "
        f"grep -w '{ip_addr}' | "
        f"awk '{{print $2}}' | "
        f"head -1"
    )

    rc, out = _ssh(float_ip, cmd)

    iface = out.strip().splitlines()[-1]

    if rc != 0 or not iface:
        print(f"[Config] ERROR: interface not found for {ip_addr}")
        return None

    print(f"[Config] {ip_addr} → {iface}")

    return iface


# ──────────────────────────────────────────────────────────────────────────────
# Remove old container
# ──────────────────────────────────────────────────────────────────────────────

def cleanup_container(float_ip, name):

    _ssh(
        float_ip,
        f"sudo docker rm -f {name} >/dev/null 2>&1 || true",
        timeout=20
    )


# ──────────────────────────────────────────────────────────────────────────────
# Wait for container to become healthy/running
# ──────────────────────────────────────────────────────────────────────────────

def wait_for_container(float_ip, name, timeout=30):

    deadline = time.time() + timeout

    while time.time() < deadline:

        rc, out = _ssh(
            float_ip,
            f"sudo docker inspect -f '{{{{.State.Running}}}}' {name}",
            timeout=10
        )

        if rc == 0 and "true" in out.lower():
            return True

        time.sleep(2)

    return False


# ──────────────────────────────────────────────────────────────────────────────
# Launch containerized VNF
# ──────────────────────────────────────────────────────────────────────────────
def launch_container(vnf_type, float_ip, in_ip, out_ip):

    image = VNF_IMAGES[vnf_type]

    print(f"[DEBUG] launch_container()")
    print(f"[DEBUG] image={image}")

    in_if = detect_interface(float_ip, in_ip)
    out_if = detect_interface(float_ip, out_ip)

    print(f"[DEBUG] in_if={in_if}")
    print(f"[DEBUG] out_if={out_if}")

    if not in_if or not out_if:
        print("[Config] ERROR: interface detection failed")
        return False

    print(f"[Config] Pulling {image}")

    rc, out = _ssh(
        float_ip,
        f"sudo docker pull {image}",
        timeout=120
    )

    print(f"[DEBUG] pull rc={rc}")
    print(f"[DEBUG] pull out={out}")

    if rc != 0:
        print("[Config] ERROR: docker pull failed")
        return False

    _ssh(
        float_ip,
        f"sudo docker rm -f {vnf_type}",
        timeout=20
    )

    run_cmd = (
        f"sudo docker run -d "
        f"--name {vnf_type} "
        f"--restart unless-stopped "
        f"--privileged "
        f"--network host "
        f"-e VNF_IN_IF={in_if} "
        f"-e VNF_OUT_IF={out_if} "
        f"{image}"
    )

    print(f"[DEBUG] run_cmd={run_cmd}")

    rc, out = _ssh(
        float_ip,
        run_cmd,
        timeout=60
    )

    print(f"[DEBUG] run rc={rc}")
    print(f"[DEBUG] run out={out}")

    if rc != 0:
        print("[Config] ERROR: docker run failed")

        rc2, ps = _ssh(
            float_ip,
            "sudo docker ps -a",
            timeout=20
        )

        print("[DEBUG] docker ps -a:")
        print(ps)

        return False

    print("[Config]  Container launched successfully...")


    print(f"[Config] {vnf_type.upper()} OK")
    return True

# ──────────────────────────────────────────────────────────────────────────────
# Main Entry
# ──────────────────────────────────────────────────────────────────────────────

def configure_vnf(vnf_type, float_ip, in_ip, out_ip):

    print(f"\n[Config] === {vnf_type.upper()} CONFIG ===")
    print(f"[Config] float_ip={float_ip}")
    print(f"[Config] in_ip={in_ip}")
    print(f"[Config] out_ip={out_ip}")

    if vnf_type not in VNF_IMAGES:
        print(f"[Config] Unknown VNF type: {vnf_type}")
        return False

    return launch_container(
        vnf_type,
        float_ip,
        in_ip,
        out_ip
    )


# ──────────────────────────────────────────────────────────────────────────────
# Client Verification
# ──────────────────────────────────────────────────────────────────────────────

def configure_client(float_ip):

    print(f"[Config] Verifying client VM at {float_ip}")

    checks = {
        "curl": "which curl",
        "ab":   "which ab",
    }

    all_ok = True

    for tool, cmd in checks.items():

        rc, _ = _ssh(
            float_ip,
            cmd,
            timeout=20
        )

        if rc != 0:
            print(f"[Config] Client missing: {tool}")
            all_ok = False

    print(f"[Config] Client {'OK' if all_ok else 'FAILED'}")

    return all_ok


# ──────────────────────────────────────────────────────────────────────────────
# Server Verification
# ──────────────────────────────────────────────────────────────────────────────

def configure_server(float_ip):

    print(f"[Config] Verifying server VM at {float_ip}")

    rc, out = _ssh(
        float_ip,
        "curl -sf http://localhost/ -o /dev/null "
        "-w '%{http_code}' && echo SERVER_OK",
        timeout=20
    )

    success = "SERVER_OK" in out

    print(f"[Config] Server {'OK' if success else 'FAILED'}")

    return success

