import pandas as pd
import numpy as np
import os

print("--- Step 1: Load and clean ---")
file_path = "data/vrops_compute_host_cpu_usage_capacity_all.csv"
df = pd.read_csv(file_path)

df['Timestamp'] = pd.to_datetime(df['Timestamp'])
df = df.dropna(subset=['Value'])
df = df[df['Value'] >= 0]
df = df.sort_values(by='Timestamp', ascending=True)

print("\n--- Step 2: Per-host approach ---")
qualifying_hosts = []
host_data_dict = {}
host_stds = {}

df['Entity'] = df['Host'].fillna(df['BB'])
grouped = df.groupby('Entity')
for host, group in grouped:
    # Extract time series
    host_df = group[['Timestamp', 'Value']].copy()
    host_df.set_index('Timestamp', inplace=True)
    
    # Resample
    host_df = host_df.resample('1h').mean()
    host_df['Value'] = host_df['Value'].ffill(limit=2)
    host_df = host_df.dropna()
    
    if len(host_df) < 100:
        continue
        
    std_val = host_df['Value'].std()
    if pd.isna(std_val):
        continue
        
    host_stds[host] = std_val
    if std_val > 5:
        qualifying_hosts.append(host)
        host_data_dict[host] = host_df

print(f"Number of hosts with std > 5: {len(qualifying_hosts)} out of {len(grouped)}")

print("\n--- Step 3: Build sequences per host ---")
SEQ_LEN = 24
X_list, y_list, t_list = [], [], []
norm_params_per_host = {}

for host in qualifying_hosts:
    host_df = host_data_dict[host]
    
    vmin = host_df['Value'].min()
    vmax = host_df['Value'].max()
    
    if vmax == vmin:
        continue
        
    norm_params_per_host[host] = [vmin, vmax]
    host_df['Value_Norm'] = (host_df['Value'] - vmin) / (vmax - vmin)
    
    values = host_df['Value_Norm'].values
    timestamps = host_df.index.values
    
    for i in range(len(values) - SEQ_LEN):
        X_list.append(values[i : i+SEQ_LEN])
        y_list.append(values[i+SEQ_LEN])
        t_list.append(timestamps[i+SEQ_LEN])

print(f"Total sequences collected: {len(X_list)}")

print("\n--- Step 4: Time-based split ---")
X_arr = np.array(X_list)
y_arr = np.array(y_list)
t_arr = np.array(t_list)

# Sort by source timestamp
sort_indices = np.argsort(t_arr)
X_sorted = X_arr[sort_indices]
y_sorted = y_arr[sort_indices]

split_idx = int(len(X_sorted) * 0.8)
X_train = X_sorted[:split_idx]
X_test = X_sorted[split_idx:]
y_train = y_sorted[:split_idx]
y_test = y_sorted[split_idx:]

print(f"X_train shape: {X_train.shape}")
print(f"X_test shape: {X_test.shape}")

print("\n--- Step 5: Save files ---")
np.save("X_train.npy", X_train)
np.save("X_test.npy", X_test)
np.save("y_train.npy", y_train)
np.save("y_test.npy", y_test)
np.save("norm_params_per_host.npy", norm_params_per_host)
np.save("host_stds.npy", host_stds)
print("Saved all output files.")

print("\n--- Step 6: Validation ---")
print(f"Hosts with std > 5:        {len(qualifying_hosts)} should be > 10")
print(f"X_train shape:             {X_train.shape} should be (N, 24) where N > 1000")
print(f"X_test shape:              {X_test.shape} should be (M, 24)")

if len(X_train) > 0 and len(X_test) > 0:
    all_X = np.concatenate((X_train, X_test))
    print(f"Value range:               [{all_X.min():.4f}, {all_X.max():.4f}] should be [0.0, 1.0]")
    print(f"Any NaN:                   {np.isnan(all_X).any()} should be False")
    print(f"Overall std across all seq:{all_X.std():.4f} should be > 0.1")
