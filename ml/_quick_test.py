#!/usr/bin/env python3
"""快速验证训练循环"""
import sys, os, torch, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ml.dataset import prepare_dataloaders
from ml.train import StockPredictor
import torch.nn as nn
from torch.optim import AdamW

print("Loading data...")
loaders = prepare_dataloaders(batch_size=32)
print(f"Train batches: {len(loaders['train'])}")

device = torch.device("cpu")
model = StockPredictor().to(device)
opt = AdamW(model.parameters(), lr=3e-4)
criterion = nn.BCEWithLogitsLoss()

print("Training 2 epochs...")
for epoch in range(1, 3):
    t0 = time.time()
    model.train()
    total_loss = 0
    batches = 0
    for X, y, _ in loaders["train"]:
        X, y = X.to(device), y.to(device)
        opt.zero_grad()
        logits = model(X)
        loss = criterion(logits, y)
        loss.backward()
        opt.step()
        total_loss += loss.item()
        batches += 1
        if batches <= 2:
            print(f"  batch {batches}: loss={loss.item():.4f}")

    t = time.time() - t0
    print(f"Epoch {epoch}: loss={total_loss / max(batches, 1):.4f}, time={t:.1f}s, batches={batches}")

print("Training OK!")
