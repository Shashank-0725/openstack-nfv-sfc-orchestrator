import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import TensorDataset, DataLoader
import os

print("--- Step 1: Load data ---")
X_train = np.load("X_train.npy").astype(np.float32)
X_test = np.load("X_test.npy").astype(np.float32)
y_train = np.load("y_train.npy").astype(np.float32)
y_test = np.load("y_test.npy").astype(np.float32)
norm_params_per_host = np.load("norm_params_per_host.npy", allow_pickle=True).item()

print("--- Step 2: Model architecture ---")
class TransformerPredictor(nn.Module):
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
        x = x.unsqueeze(-1)           # (B, 24, 1)
        x = self.input_proj(x)        # (B, 24, 64)
        x = x + self.pe               # add positional encoding
        x = self.transformer(x)       # (B, 24, 64)
        x = self.dropout(x[:, -1, :]) # last timestep
        return self.fc(x).squeeze()   # (B,)

print("--- Step 3: Training setup ---")
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

model = TransformerPredictor().to(device)

train_dataset = TensorDataset(torch.from_numpy(X_train), torch.from_numpy(y_train))
test_dataset = TensorDataset(torch.from_numpy(X_test), torch.from_numpy(y_test))

batch_size = 64
train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False)

epochs = 50
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)
criterion = nn.MSELoss()

# Global approximation for RMSE denormalization
vmax_vmin_diffs = [v[1] - v[0] for v in norm_params_per_host.values()]
mean_diff = np.mean(vmax_vmin_diffs)
mean_vmin = np.mean([v[0] for v in norm_params_per_host.values()])

print("\n--- Step 4: Training loop ---")
best_val_loss = float('inf')
best_epoch = 0
patience_counter = 0
early_stop_patience = 10

for epoch in range(epochs):
    model.train()
    train_losses = []
    for batch_x, batch_y in train_loader:
        batch_x, batch_y = batch_x.to(device), batch_y.to(device)
        
        optimizer.zero_grad()
        preds = model(batch_x)
        loss = criterion(preds, batch_y)
        loss.backward()
        optimizer.step()
        
        train_losses.append(loss.item())
        
    model.eval()
    val_losses = []
    with torch.no_grad():
        for batch_x, batch_y in test_loader:
            batch_x, batch_y = batch_x.to(device), batch_y.to(device)
            preds = model(batch_x)
            loss = criterion(preds, batch_y)
            val_losses.append(loss.item())
            
    train_loss = np.mean(train_losses)
    val_loss = np.mean(val_losses)
    
    scheduler.step(val_loss)
    
    # Calculate RMSE in real CPU %
    rmse_normalized = np.sqrt(val_loss)
    rmse_real = rmse_normalized * mean_diff
    
    print(f"Epoch {epoch:02d} | Train Loss: {train_loss:.5f} | Val Loss: {val_loss:.5f} | RMSE: {rmse_real:.2f}%")
    
    if val_loss < best_val_loss:
        best_val_loss = val_loss
        best_epoch = epoch
        patience_counter = 0
        torch.save(model.state_dict(), "transformer_cpu_best.pth")
    else:
        patience_counter += 1
        
    if patience_counter >= early_stop_patience:
        print(f"Early stopping triggered at epoch {epoch}")
        break

print("\n--- Step 5: After training ---")
model.load_state_dict(torch.load("transformer_cpu_best.pth", weights_only=True))
model.eval()

all_preds = []
all_targets = []
with torch.no_grad():
    for batch_x, batch_y in test_loader:
        batch_x = batch_x.to(device)
        preds = model(batch_x)
        all_preds.extend(preds.cpu().numpy())
        all_targets.extend(batch_y.numpy())

all_preds = np.array(all_preds)
all_targets = np.array(all_targets)

final_val_loss = np.mean((all_preds - all_targets)**2)
final_rmse_norm = np.sqrt(final_val_loss)
final_rmse = final_rmse_norm * mean_diff
final_mae_norm = np.mean(np.abs(all_preds - all_targets))
final_mae = final_mae_norm * mean_diff

print(f"Best Val Loss:     {final_val_loss:.5f}")
print(f"Final RMSE:        {final_rmse:.2f}%")
print(f"MAE:               {final_mae:.2f}%")

print("\nSample predictions (last 5 from test set):")
for i in range(-5, 0):
    pred_real = all_preds[i] * mean_diff + mean_vmin
    act_real = all_targets[i] * mean_diff + mean_vmin
    print(f"  Predicted: {pred_real:.1f}%   Actual: {act_real:.1f}%")

print("\n--- Step 6: Save ---")
torch.save(model.state_dict(), "transformer_cpu.pth")
torch.save({
    'model_state': model.state_dict(),
    'seq_len': 24,
    'd_model': 64,
    'nhead': 4,
    'num_layers': 2
}, "transformer_checkpoint.pth")
print("Saved models.")

print("\n--- Step 7: Validation checks to print ---")
total_params = sum(p.numel() for p in model.parameters())
print(f"Model parameters:        {total_params}")
print(f"Best epoch:              {best_epoch}")
print(f"Early stopping at:       {epoch if patience_counter >= early_stop_patience else 'N/A'}")
print(f"transformer_cpu.pth:     {'Saved' if os.path.exists('transformer_cpu.pth') else 'Missing'}")
print(f"Final test RMSE:         {final_rmse:.2f}%")
