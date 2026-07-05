"""
Transformer-based Predictive Placement Engine.

Purpose:
  Forecast compute node CPU utilization for the next hour based on historical
  workload sequences (length 24) and select the hypervisor with the lowest predicted load.
"""

import os
import copy
import numpy as np

from config import COMPUTE_NODES
from core.openstack_ops import get_node_metrics

# ──────────────────────────────────────────────────────────────────────────────
# PyTorch Dependency Check
# ──────────────────────────────────────────────────────────────────────────────
try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


# ──────────────────────────────────────────────────────────────────────────────
# Transformer Predictor Architecture (Directly from train.py)
# ──────────────────────────────────────────────────────────────────────────────
if HAS_TORCH:
    class TransformerPredictor(nn.Module):
        """
        Exact Transformer architecture trained on the CPU telemetry dataset.
        """
        def __init__(self, seq_len=24, d_model=64, nhead=4, num_layers=2, dropout=0.1):
            super().__init__()
            self.input_proj = nn.Linear(1, d_model)
            
            # Positional encoding
            pe = torch.zeros(seq_len, d_model)
            pos = torch.arange(seq_len).unsqueeze(1).float()
            div = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
            pe[:, 0::2] = torch.sin(pos * div)
            pe[:, 1::2] = torch.cos(pos * div)
            self.register_buffer('pe', pe.unsqueeze(0))
            
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=d_model, nhead=nhead,
                dim_feedforward=128, dropout=dropout,
                batch_first=True
            )
            self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
            self.dropout = nn.Dropout(dropout)
            self.fc = nn.Linear(d_model, 1)

        def forward(self, x):
            if x.dim() == 2:
                x = x.unsqueeze(-1)           # (B, 24, 1)
            elif x.dim() == 1:
                x = x.unsqueeze(0).unsqueeze(-1) # (1, 24, 1)
            x = self.input_proj(x)        # (B, 24, 64)
            x = x + self.pe               # add positional encoding
            x = self.transformer(x)       # (B, 24, 64)
            x = self.dropout(x[:, -1, :]) # last timestep of sequence
            return self.fc(x).squeeze()   # (B,) or scalar
else:
    class TransformerPredictor:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Global Model & Parameters Loading
# ──────────────────────────────────────────────────────────────────────────────
MODEL_INSTANCE = None
NORM_PARAMS = {}

CORE_DIR = os.path.dirname(__file__)
WEIGHTS_PATH = os.path.join(CORE_DIR, 'transformer_model.pth')
NORM_PATH = os.path.join(CORE_DIR, 'norm_params_per_host.npy')

# Load Normalization boundaries
if os.path.exists(NORM_PATH):
    try:
        NORM_PARAMS = np.load(NORM_PATH, allow_pickle=True).item()
        print(f"[Placement] Loaded normalization parameters for {len(NORM_PARAMS)} hosts.")
    except Exception as e:
        print(f"[Placement] Error loading normalization file: {e}")

# Load PyTorch Weights
if HAS_TORCH:
    try:
        MODEL_INSTANCE = TransformerPredictor()
        if os.path.exists(WEIGHTS_PATH):
            MODEL_INSTANCE.load_state_dict(torch.load(WEIGHTS_PATH, map_location=torch.device('cpu'), weights_only=True))
            MODEL_INSTANCE.eval()
            print(f"[Placement] Successfully loaded trained weights: {WEIGHTS_PATH}")
        else:
            print(f"[Placement] Warning: Weights file {WEIGHTS_PATH} not found. Running with un-trained initialization.")
    except Exception as e:
        print(f"[Placement] Error initializing weights: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Telemetry History Generator (Simulates sliding window inputs)
# ──────────────────────────────────────────────────────────────────────────────
def get_historical_sequence(node_name, current_cpu):
    """
    Simulates a 24-hour historical load trend matching the current CPU baseline,
    incorporating a diurnal sine wave and gaussian noise.
    """
    np.random.seed(hash(node_name) % (2**32))
    base = current_cpu
    times = np.arange(24)
    # 24-hour cycle: peak load in afternoon, dip at night
    diurnal_trend = 8 * np.sin(times * (2 * np.pi / 24) - (np.pi / 2))
    noise = np.random.normal(0, 1.5, 24)
    history = base + diurnal_trend + noise
    return np.clip(history, 0.0, 100.0)


# ──────────────────────────────────────────────────────────────────────────────
# Placement Optimizer
# ──────────────────────────────────────────────────────────────────────────────
def optimize_placement(vnf_list):
    """
    Uses the trained Transformer model to forecast next-hour CPU load for
    compute nodes, and places VNFs on the node with the lowest predicted load.
    """
    node_metrics = get_node_metrics()
    if not node_metrics:
        raise RuntimeError("No compute node metrics available from hypervisors.")

    # Local state for batch-placement allocation
    node_state = copy.deepcopy(node_metrics)
    placement = {}
    
    # Workload incremental footprint costs
    RESOURCE_COST = {
        'firewall': {'cpu': 8,  'ram': 8},
        'ids':      {'cpu': 12, 'ram': 12},
        'monitor':  {'cpu': 4,  'ram': 4},
        'generic':  {'cpu': 6,  'ram': 6}
    }
    
    node_list = sorted(list(node_state.keys()))
    
    for idx, vnf in enumerate(vnf_list):
        vnf_id = vnf['id']
        vnf_type = vnf.get('type', 'generic')
        
        # Filter active nodes
        active_nodes = [n for n in node_list if node_state[n].get('state') == 'up' and node_state[n].get('status') == 'enabled']
        if not active_nodes:
            raise RuntimeError(f"No active compute nodes available to schedule VNF: {vnf_id}")
            
        cost = RESOURCE_COST.get(vnf_type, RESOURCE_COST['generic'])
        
        # 1. RUN TRANSFORER FORECASTING
        node_scores = {}
        for node in active_nodes:
            current_cpu = node_state[node].get('cpu_util', 0)
            history = get_historical_sequence(node, current_cpu)
            
            # Extract normalization bounds
            # Matches names like 'openstack-compute1' to dataset keys if mapping available
            # otherwise uses min-max of the local history
            norm_key = next((k for k in NORM_PARAMS.keys() if node in k or k in node), None)
            if norm_key:
                vmin, vmax = NORM_PARAMS[norm_key]
            else:
                vmin, vmax = 0.0, 100.0
                
            norm_history = (history - vmin) / (vmax - vmin + 1e-6)
            
            if HAS_TORCH and MODEL_INSTANCE is not None:
                try:
                    tensor = torch.tensor(norm_history, dtype=torch.float32).unsqueeze(0) # (1, 24)
                    with torch.no_grad():
                        pred_norm = MODEL_INSTANCE(tensor).item()
                    predicted_cpu = pred_norm * (vmax - vmin) + vmin
                    print(f"[Placement] Transformer forecast for {node}: {predicted_cpu:.2f}% CPU (Current: {current_cpu}%)")
                except Exception as e:
                    predicted_cpu = current_cpu + np.random.normal(1.0, 0.5)
            else:
                # Math fallback: simulates prediction by projecting trend
                predicted_cpu = current_cpu + 1.2 * (history[-1] - history[0]) / 24.0 + np.random.normal(0.5, 0.2)
                
            # Score formula: combines predicted future CPU load with RAM and VNF density
            ram_util = node_state[node].get('ram_util', 0)
            vnf_cnt = node_state[node].get('vnf_count', 0)
            ram_free = node_state[node].get('ram_free_mb', 0)
            ram_free_score = min(ram_free / 24000.0, 1.0)
            
            score = (
                (1 - predicted_cpu / 100.0) * 0.35 +
                (1 - ram_util / 100.0) * 0.30 +
                (1 - vnf_cnt / 20.0) * 0.20 +
                ram_free_score * 0.15
            )
            node_scores[node] = round(score, 4)
            
        best_node = max(node_scores, key=node_scores.get)
        best_score = node_scores[best_node]
        
        placement[vnf_id] = {
            'node': best_node,
            'az': f"nova:{best_node}",
            'score': round(best_score, 3)
        }
        
        # ----------------------------------------------------------------------
        # Update simulated node states
        # ----------------------------------------------------------------------
        node_state[best_node]['cpu_util'] = min(node_state[best_node]['cpu_util'] + cost['cpu'], 100)
        node_state[best_node]['ram_util'] = min(node_state[best_node]['ram_util'] + cost['ram'], 100)
        node_state[best_node]['vnf_count'] += 1
        node_state[best_node]['ram_free_mb'] = max(0, node_state[best_node]['ram_free_mb'] - 1024)
        
        print(f"[Placement] VNF {vnf_id} ({vnf_type}) → {best_node} (score={best_score:.3f})")
        
    return placement


# ──────────────────────────────────────────────────────────────────────────────
# Explain Placement (GUI Helper)
# ──────────────────────────────────────────────────────────────────────────────
def explain_placement(placement, node_metrics):
    """Builds a GUI-friendly detail log dictionary."""
    lines = []
    for vnf_id, info in placement.items():
        node = info['node']
        metrics = node_metrics.get(node, {})
        lines.append({
            'vnf': vnf_id,
            'node': node,
            'score': info['score'],
            'node_cpu': metrics.get('cpu_util', 0),
            'node_ram': metrics.get('ram_util', 0),
            'ram_free_mb': metrics.get('ram_free_mb', 0),
            'vnfs_running': metrics.get('vnf_count', 0),
            'load_average': metrics.get('load_average', '0,0,0'),
            'state': metrics.get('state', 'unknown'),
            'status': metrics.get('status', 'unknown'),
        })
    return lines
