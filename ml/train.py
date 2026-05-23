#!/usr/bin/env python3
"""
深度学习训练脚本
=================
基于 1D Conv + MLP 的序列预测模型。
输入: (B, 180天, 34特征)
输出: P(未来5日超额收益 > 0)

架构:
  Conv1d → BatchNorm → GELU → AdaptiveAvgPool → MLP → Sigmoid

训练策略:
  - 严格时间切分 (train/val/test)
  - Early stopping on val AUC
  - 分层评估 (按预测概率分5组，验证单调性)

用法:
    python ml/train.py                           # 默认参数训练
    python ml/train.py --epochs 100 --lr 0.001    # 自定义超参
    python ml/train.py --checkpoint best.pt       # 从检查点恢复
"""

import argparse
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ml.dataset import (
    prepare_dataloaders,
    SEQ_LEN,
    N_FEATURES,
    FEATURE_COLS,
)
from ml.build_features import FEATURE_COLS as _FC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("train")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML_DIR = os.path.join(ROOT_DIR, "data", "ml")
MODEL_DIR = os.path.join(ML_DIR, "models")
os.makedirs(MODEL_DIR, exist_ok=True)

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
log.info("Device: %s", DEVICE)


# ============================================================================
# 模型
# ============================================================================

class ConvFeatureExtractor(nn.Module):
    """
    将 (B, 180, 34) 序列压缩为 (B, 128) 特征向量。

    使用三层 Conv1d + MaxPool 逐级降时间维度:
      180 → 90 → 45 → 22
    然后 AdaptiveAvgPool1d → 1 时间步。
    """
    def __init__(self, n_features: int, hidden_dim: int = 128):
        super().__init__()
        self.conv1 = nn.Sequential(
            nn.Conv1d(n_features, hidden_dim, kernel_size=5, padding=2),
            nn.BatchNorm1d(hidden_dim),
            nn.GELU(),
            nn.MaxPool1d(2),      # 180 → 90
        )
        self.conv2 = nn.Sequential(
            nn.Conv1d(hidden_dim, hidden_dim * 2, kernel_size=5, padding=2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.GELU(),
            nn.MaxPool1d(2),      # 90 → 45
        )
        self.conv3 = nn.Sequential(
            nn.Conv1d(hidden_dim * 2, hidden_dim * 4, kernel_size=5, padding=2),
            nn.BatchNorm1d(hidden_dim * 4),
            nn.GELU(),
            nn.MaxPool1d(2),      # 45 → 22
        )
        self.pool = nn.AdaptiveAvgPool1d(1)  # 22 → 1
        self.out_dim = hidden_dim * 4

    def forward(self, x):
        # x: (B, T, C) → (B, C, T) for Conv1d
        x = x.permute(0, 2, 1)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.pool(x)          # (B, C_out, 1)
        x = x.squeeze(-1)         # (B, C_out)
        return x


class StockPredictor(nn.Module):
    """
    完整预测模型: Conv特征提取 + MLP分类头。
    """
    def __init__(
        self,
        n_features: int = N_FEATURES,
        hidden_dim: int = 128,
        mlp_dims: tuple = (512, 256, 128),
        dropout: float = 0.3,
    ):
        super().__init__()
        self.encoder = ConvFeatureExtractor(n_features, hidden_dim)
        enc_out = self.encoder.out_dim

        layers = []
        in_dim = enc_out
        for out_dim in mlp_dims:
            layers.extend([
                nn.Linear(in_dim, out_dim),
                nn.BatchNorm1d(out_dim),
                nn.GELU(),
                nn.Dropout(dropout),
            ])
            in_dim = out_dim

        layers.append(nn.Linear(in_dim, 1))
        self.classifier = nn.Sequential(*layers)

    def forward(self, x):
        feat = self.encoder(x)
        logit = self.classifier(feat).squeeze(-1)
        return logit


# ============================================================================
# 评估
# ============================================================================

@torch.no_grad()
def evaluate(model: nn.Module, loader, device: torch.device):
    """计算 AUC 和 准确率"""
    model.eval()
    all_probs = []
    all_labels = []
    all_excess = []

    for X, y, excess in loader:
        X = X.to(device)
        logits = model(X)
        probs = torch.sigmoid(logits)

        all_probs.append(probs.cpu().numpy())
        all_labels.append(y.cpu().numpy())
        all_excess.append(excess.cpu().numpy())

    probs = np.concatenate(all_probs)
    labels = np.concatenate(all_labels)
    excess = np.concatenate(all_excess)

    auc = roc_auc_score(labels, probs) if len(np.unique(labels)) > 1 else 0.5
    acc = ((probs > 0.5) == labels).mean()

    return auc, acc, probs, labels, excess


def stratified_eval(probs: np.ndarray, labels: np.ndarray, excess: np.ndarray,
                    n_bins: int = 5):
    """
    分层评估: 按预测概率分 n_bins 组，看各组实际超额收益是否单调。
    理想情况: 概率越高 → 实际超额收益均值越大。
    """
    boundaries = np.percentile(probs, np.linspace(0, 100, n_bins + 1))
    boundaries[0] = -0.01
    boundaries[-1] = 1.01

    result = []
    for i in range(n_bins):
        mask = (probs > boundaries[i]) & (probs <= boundaries[i + 1])
        if mask.sum() == 0:
            result.append({
                "bin": i + 1,
                "prob_range": f"[{boundaries[i]:.2f}, {boundaries[i + 1]:.2f}]",
                "n": 0,
                "hit_rate": 0.0,
                "mean_excess": 0.0,
            })
            continue

        bin_labels = labels[mask]
        bin_excess = excess[mask]
        result.append({
            "bin": i + 1,
            "prob_range": f"[{boundaries[i]:.2f}, {boundaries[i + 1]:.2f}]",
            "n": int(mask.sum()),
            "hit_rate": float(bin_labels.mean()),
            "mean_excess": float(np.mean(bin_excess)),
        })

    return result


def print_stratified(result: list[dict], title: str = "分层评估"):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"  {'组别':<6} {'概率区间':<20} {'样本数':<8} {'命中率':<8} {'平均超额%':<10}")
    print(f"  {'-'*52}")
    for r in result:
        print(f"  {r['bin']:<6} {r['prob_range']:<20} {r['n']:<8} "
              f"{r['hit_rate']:<8.2%} {r['mean_excess']:<10.4f}")


# ============================================================================
# 训练
# ============================================================================

def train(args):
    # 1. 数据
    log.info("加载数据...")
    loaders = prepare_dataloaders(
        batch_size=args.batch_size,
        num_workers=args.num_workers,
    )
    log.info("训练样本: %d", len(loaders["ds_val"].samples))

    # 2. 模型
    model = StockPredictor(
        n_features=N_FEATURES,
        hidden_dim=args.hidden_dim,
        mlp_dims=tuple(args.mlp_dims),
        dropout=args.dropout,
    ).to(DEVICE)

    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log.info("参数量: %d (可训练: %d)", total_params, trainable_params)

    # 3. 优化器 & 损失 & 调度器
    optimizer = AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=args.epochs // 3,
                                            T_mult=2, eta_min=args.lr * 0.01)
    criterion = nn.BCEWithLogitsLoss()

    # 类权重（处理标签不均衡）
    # 简单处理：如果正例%<40%，给正例更大权重
    train_loader = loaders["train"]

    # 4. 训练循环
    best_val_auc = 0.0
    best_epoch = 0
    patience_counter = 0

    for epoch in range(1, args.epochs + 1):
        # ---- Train ----
        model.train()
        train_loss = 0.0
        train_batches = 0

        for X, y, _ in train_loader:
            X, y = X.to(DEVICE), y.to(DEVICE)
            optimizer.zero_grad()
            logits = model(X)
            loss = criterion(logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            train_loss += loss.item()
            train_batches += 1

        scheduler.step()

        # ---- Eval ----
        val_auc, val_acc, val_probs, val_labels, val_excess = evaluate(
            model, loaders["val"], DEVICE)

        lr_now = optimizer.param_groups[0]["lr"]
        log.info("Epoch %3d | loss=%.4f | val_auc=%.4f | val_acc=%.4f | lr=%.6f",
                 epoch, train_loss / max(train_batches, 1),
                 val_auc, val_acc, lr_now)

        # ---- Early Stopping ----
        if val_auc > best_val_auc + 1e-4:
            best_val_auc = val_auc
            best_epoch = epoch
            patience_counter = 0
            # 保存最佳模型
            torch.save({
                "epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "val_auc": val_auc,
                "args": {k: v for k, v in vars(args).items()
                         if not k.startswith("_")},
                "stats": loaders.get("stats"),
            }, os.path.join(MODEL_DIR, args.model_name))
            log.info("  -> 保存最佳模型 (auc=%.4f)", val_auc)
        else:
            patience_counter += 1
            if patience_counter >= args.patience:
                log.info("Early stopping at epoch %d", epoch)
                break

    # 5. 加载最佳模型，测试集评估
    checkpoint = torch.load(os.path.join(MODEL_DIR, args.model_name),
                            map_location=DEVICE, weights_only=False)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.to(DEVICE)

    test_auc, test_acc, test_probs, test_labels, test_excess = evaluate(
        model, loaders["test"], DEVICE)

    print(f"\n{'='*60}")
    print(f"  测试集结果")
    print(f"  AUC:      {test_auc:.4f}")
    print(f"  Accuracy: {test_acc:.4f}")
    print(f"  最佳epoch: {best_epoch}")
    print(f"{'='*60}")

    # 分层评估
    result = stratified_eval(test_probs, test_labels, test_excess)
    print_stratified(result, "测试集分层评估")

    # 6. 不同阈值的Precision
    print(f"\n  阈值分析:")
    for threshold in [0.5, 0.6, 0.7, 0.8, 0.9]:
        preds = (test_probs > threshold).astype(int)
        if preds.sum() == 0:
            print(f"    阈值={threshold:.1f}: 无预测为正的样本")
            continue
        precision = (test_labels[preds == 1]).mean()
        recall = preds.mean()
        mean_excess = test_excess[preds == 1].mean()
        print(f"    阈值={threshold:.1f}: 样本={preds.sum():5d}, "
              f"命中率={precision:.2%}, 占比={recall:.2%}, 均超额={mean_excess:.4f}")

    return test_auc


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="深度学习训练")

    # 数据
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--num-workers", type=int, default=0)

    # 模型
    parser.add_argument("--hidden-dim", type=int, default=128)
    parser.add_argument("--mlp-dims", type=int, nargs="+",
                        default=[512, 256, 128])
    parser.add_argument("--dropout", type=float, default=0.3)

    # 优化
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--patience", type=int, default=10)

    # 保存
    parser.add_argument("--model-name", type=str,
                        default=f"model_{datetime.now().strftime('%Y%m%d_%H%M')}.pt")

    args = parser.parse_args()

    print(f"Device: {DEVICE}")
    print(f"Batch size: {args.batch_size}")
    print(f"MLP dims: {args.mlp_dims}")
    print(f"Learning rate: {args.lr}")

    train(args)
