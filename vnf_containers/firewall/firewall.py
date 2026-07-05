import os
import subprocess
import time
import sys

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[FW] {' '.join(cmd)} -> {r.returncode}")
    if r.returncode != 0:
        print(r.stderr)
    return r

IN_IF  = os.environ.get("VNF_IN_IF")
OUT_IF = os.environ.get("VNF_OUT_IF")

print(f"[Firewall] Starting IN={IN_IF} OUT={OUT_IF}")

# Enable forwarding
run(["sysctl", "-w", "net.ipv4.ip_forward=1"])

# Clean rules
run(["iptables", "-F", "FORWARD"])

# Default deny
run(["iptables", "-P", "FORWARD", "DROP"])

# Allow return traffic
run([
    "iptables",
    "-A", "FORWARD",
    "-m", "state",
    "--state", "ESTABLISHED,RELATED",
    "-j", "ACCEPT"
])

# Allow HTTP only
run([
    "iptables",
    "-A", "FORWARD",
    "-i", IN_IF,
    "-o", OUT_IF,
    "-p", "tcp",
    "--dport", "80",
    "-j", "ACCEPT"
])

print("[Firewall] FIREWALL_OK")
print("[Firewall] POLICY = ALLOW TCP/80 ONLY")

while True:
    time.sleep(60)
