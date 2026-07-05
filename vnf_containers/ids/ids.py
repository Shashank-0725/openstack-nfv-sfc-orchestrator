from scapy.all import sniff, IP, TCP, Raw
from collections import defaultdict
from queue import Queue, Full
from threading import Thread, Lock
import time
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

print("[IDS] Starting...")

# Determine interfaces
ifaces_env = os.environ.get("IDS_IFACES")
if not ifaces_env:
    in_if = os.environ.get("VNF_IN_IF")
    out_if = os.environ.get("VNF_OUT_IF")
    if in_if and out_if:
        IFACES = [in_if, out_if]
    else:
        IFACES = ["ens3", "ens4"]
else:
    IFACES = ifaces_env.split(",")

SCAN_WINDOW   = int(os.environ.get("IDS_SCAN_WINDOW", 10))
SCAN_THRESH   = int(os.environ.get("IDS_SCAN_THRESH", 5))

scan_tracker  = defaultdict(list)
pkt_queue     = Queue(maxsize=10000)
last_cleanup  = time.time()

# Thread-safe stats collection
stats_lock = Lock()
BLOCKED_IPS = {}  # { ip: count of alerts }

SQLI_PATTERNS = ["SELECT ", "UNION SELECT", "OR 1=1",
                 "DROP TABLE", "'; --", "xp_cmdshell"]
XSS_PATTERNS  = ["<script>", "javascript:", "onerror=", "onload="]

def check_portscan(src, dst_port):
    now = time.time()
    scan_tracker[src].append((dst_port, now))
    scan_tracker[src] = [
        (p, t) for p, t in scan_tracker[src]
        if now - t <= SCAN_WINDOW
    ]
    unique = {p for p, t in scan_tracker[src]}
    if len(unique) >= SCAN_THRESH:
        print(f"[IDS ALERT] Port scan: {src} hit {len(unique)} ports in {SCAN_WINDOW}s")
        with stats_lock:
            BLOCKED_IPS[src] = BLOCKED_IPS.get(src, 0) + 1

def check_payload(src, payload):
    up = payload.upper()
    for p in SQLI_PATTERNS:
        if p in up:
            print(f"[IDS ALERT] SQLi '{p}' from {src}")
            with stats_lock:
                BLOCKED_IPS[src] = BLOCKED_IPS.get(src, 0) + 1
            return
    for p in XSS_PATTERNS:
        if p in payload.lower():
            print(f"[IDS ALERT] XSS '{p}' from {src}")
            with stats_lock:
                BLOCKED_IPS[src] = BLOCKED_IPS.get(src, 0) + 1
            return

def cleanup():
    global last_cleanup
    now = time.time()
    if now - last_cleanup > 300:
        stale = [ip for ip, e in scan_tracker.items()
                 if not e or now - e[-1][1] > SCAN_WINDOW * 10]
        for ip in stale:
            del scan_tracker[ip]
        last_cleanup = now

def process_loop():
    while True:
        pkt = pkt_queue.get()
        try:
            if IP in pkt and TCP in pkt:
                src = pkt[IP].src
                if pkt[TCP].flags == "S":
                    check_portscan(src, pkt[TCP].dport)
                if Raw in pkt:
                    payload = pkt[Raw].load.decode(errors="ignore")
                    check_payload(src, payload)
            cleanup()
        except Exception as e:
            print(f"[IDS] Processing error: {e}")

def enqueue(pkt):
    try:
        pkt_queue.put_nowait(pkt)
    except Full:
        pass  # intentional drop — never block capture

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Metrics API Server (Port 8080)
# ──────────────────────────────────────────────────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress standard logs to keep container stdout clean

    def do_GET(self):
        if self.path == '/metrics':
            with stats_lock:
                response_data = {
                    'blocked_ips': BLOCKED_IPS.copy(),
                    'total_blocked': sum(BLOCKED_IPS.values()),
                    'rate_limit': SCAN_THRESH
                }
            
            data = json.dumps(response_data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

def run_metrics_server():
    server = HTTPServer(('0.0.0.0', 8080), MetricsHandler)
    print("[IDS API] Listening on port 8080")
    server.serve_forever()

# Start background workers
Thread(target=process_loop, daemon=True).start()
Thread(target=run_metrics_server, daemon=True).start()

print(f"[IDS] Listening on {IFACES}")
sniff(iface=IFACES, prn=enqueue, store=False)
