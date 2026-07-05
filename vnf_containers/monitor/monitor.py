from scapy.all import sniff, IP, TCP, UDP, ICMP
from collections import Counter
from threading import Lock, Thread
from queue import Queue, Full
import time
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

print("[MONITOR] Starting...")

# Determine interfaces
ifaces_env = os.environ.get("MONITOR_IFACES")
if not ifaces_env:
    in_if = os.environ.get("VNF_IN_IF")
    out_if = os.environ.get("VNF_OUT_IF")
    if in_if and out_if:
        IFACES = [in_if, out_if]
    else:
        IFACES = ["ens3", "ens4"]
else:
    IFACES = ifaces_env.split(",")

INTERVAL = int(os.environ.get("MONITOR_INTERVAL", 5))

stats = {
    "total_packets": 0, "tcp_packets": 0,
    "udp_packets":   0, "icmp_packets": 0,
    "total_bytes":   0, "dropped_packets": 0
}
top_src       = Counter()
top_dst_port  = Counter()
lock          = Lock()
pkt_queue     = Queue(maxsize=50000)

# Rolling list of recent packets to serve to the dashboard
recent_packets = []  # max length 20
max_recent_len = 20

def process(pkt):
    try:
        pkt_queue.put_nowait(pkt)
    except Full:
        with lock:
            stats["dropped_packets"] += 1

def stats_loop():
    global recent_packets
    while True:
        pkt = pkt_queue.get()
        is_tcp  = TCP  in pkt
        is_udp  = UDP  in pkt
        is_icmp = ICMP in pkt
        size    = len(bytes(pkt))
        src     = pkt[IP].src if IP in pkt else None
        dst     = pkt[IP].dst if IP in pkt else None
        
        dport   = pkt[TCP].dport if is_tcp else (
                  pkt[UDP].dport if is_udp else None)
        sport   = pkt[TCP].sport if is_tcp else (
                  pkt[UDP].sport if is_udp else None)
                  
        proto = "TCP" if is_tcp else ("UDP" if is_udp else ("ICMP" if is_icmp else "IP"))
        
        with lock:
            stats["total_packets"] += 1
            stats["total_bytes"]   += size
            if is_tcp:   stats["tcp_packets"]  += 1
            elif is_udp: stats["udp_packets"]  += 1
            elif is_icmp: stats["icmp_packets"] += 1
            if src:   top_src[src] += 1
            if dport: top_dst_port[dport] += 1
            
            # Format and add to recent_packets
            if src and dst:
                pkt_info = {
                    "src": f"{src}:{sport}" if sport else src,
                    "dst": f"{dst}:{dport}" if dport else dst,
                    "proto": proto
                }
                recent_packets.append(pkt_info)
                if len(recent_packets) > max_recent_len:
                    recent_packets = recent_packets[-max_recent_len:]

def reporter():
    prev = {k: 0 for k in stats}
    while True:
        time.sleep(INTERVAL)
        with lock:
            snapshot    = stats.copy()
            top_s       = top_src.most_common(3)
            top_p       = top_dst_port.most_common(3)

        dpkts  = snapshot["total_packets"] - prev["total_packets"]
        dbytes = snapshot["total_bytes"]   - prev["total_bytes"]
        pps    = dpkts  / INTERVAL
        mbps   = (dbytes * 8) / INTERVAL / 1_000_000

        print(f"\n[MONITOR] — {INTERVAL}s window")
        print(f"  Packets/s   : {pps:.1f}")
        print(f"  Throughput  : {mbps:.3f} Mbps")
        print(f"  TCP / UDP / ICMP : "
              f"{snapshot['tcp_packets']} / "
              f"{snapshot['udp_packets']} / "
              f"{snapshot['icmp_packets']}")
        print(f"  Total pkts  : {snapshot['total_packets']}")
        print(f"  Dropped     : {snapshot['dropped_packets']}")
        print(f"  Top sources : {top_s}")
        print(f"  Top ports   : {top_p}")
        print("-" * 40)
        prev = snapshot

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Metrics API Server (Port 8080)
# ──────────────────────────────────────────────────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress logging

    def do_GET(self):
        if self.path == '/metrics':
            with lock:
                response_data = {
                    'recent': recent_packets.copy(),
                    'total_packets': stats["total_packets"],
                    'top_sources': dict(top_src.most_common(5))
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
    print("[MONITOR API] Listening on port 8080")
    server.serve_forever()

Thread(target=stats_loop, daemon=True).start()
Thread(target=reporter,   daemon=True).start()
Thread(target=run_metrics_server, daemon=True).start()

print(f"[MONITOR] Listening on {IFACES}")
sniff(iface=IFACES, prn=process, store=False)
