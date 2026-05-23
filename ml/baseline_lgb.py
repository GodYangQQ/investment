#!/usr/bin/env python3
"""
LightGBM 排序学习基线（v2）
============================
预测目标：未来20日收益的横截面分位数（0~1）
损失函数：LambdaRank（LGB原生支持）
评估指标：Spearman排名相关性 + 分组收益单调性

用法:
    python ml/baseline_lgb.py
"""

import os
import sys
import time
import warnings
import numpy as np
import pandas as pd
import lightgbm as lgb
from scipy.stats import spearmanr

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ml.dataset import (
    FEATURES_PARQUET,
    FEATURE_COLS,
    SEQ_LEN,
    TRAIN_END,
    VAL_END,
    TEST_END,
)

print("=" * 60)
print("  LightGBM 排序学习基线")
print("=" * 60)

# 1. 加载数据
df = pd.read_parquet(FEATURES_PARQUET)
df["date"] = pd.to_datetime(df["date"]).copy()

# 2. 构建横截面特征
print("\n构建横截面特征矩阵...")
t0 = time.time()

records = []
for code, grp in df.groupby("code"):
    grp = grp.sort_values("date")
    closes = grp["close"].values
    feat = grp[FEATURE_COLS].copy()
    feat = feat.fillna(0).values

    for i in range(SEQ_LEN, len(grp) - 20):
        latest_feat = feat[i].copy()
        past_20 = feat[max(0, i - 20): i + 1]
        past_60 = feat[max(0, i - 60): i + 1]

        f_vec = np.concatenate([
            latest_feat,
            np.nanmean(past_20, axis=0),
            np.nanstd(past_20, axis=0),
            np.nanmean(past_60, axis=0),
            np.nanstd(past_60, axis=0),
        ])

        if i + 20 < len(closes):
            stock_ret = (closes[i + 20] / closes[i] - 1) * 100
        else:
            stock_ret = 0.0

        records.append({
            "date": grp.iloc[i]["date"],
            "code": code,
            "features": f_vec,
            "stock_ret_20d": stock_ret,
        })

df_feat = pd.DataFrame(records)
df_feat["date"] = pd.to_datetime(df_feat["date"])

# 排序标签：每天全市场收益分位数 (0~1)
print("计算横截面排序标签...")
df_feat["rank_label"] = np.nan
for date_key, idx in df_feat.groupby("date").groups.items():
    df_feat.loc[idx, "rank_label"] = df_feat.loc[idx, "stock_ret_20d"].rank(pct=True)

df_feat = df_feat.dropna(subset=["rank_label"])

elapsed = time.time() - t0
print(f"构建完成: {len(df_feat)} 样本, 耗时 {elapsed:.1f}s")

# 3. 时间切分
train_mask = df_feat["date"] <= TRAIN_END
val_mask = (df_feat["date"] > TRAIN_END) & (df_feat["date"] <= VAL_END)
test_mask = df_feat["date"] > VAL_END

X_train = np.stack(df_feat[train_mask]["features"].values)
X_val = np.stack(df_feat[val_mask]["features"].values)
X_test = np.stack(df_feat[test_mask]["features"].values)

y_train = df_feat[train_mask]["rank_label"].values
y_val = df_feat[val_mask]["rank_label"].values
y_test = df_feat[test_mask]["rank_label"].values
ret_test = df_feat[test_mask]["stock_ret_20d"].values

print(f"训练: {len(X_train)}, 验证: {len(X_val)}, 测试: {len(X_test)}")

# 4. 训练 LightGBM（排序损失）
print("\n训练 LightGBM (LambdaRank)...")
t0 = time.time()

# 按日期分组做 query
def build_groups(dates):
    groups = []
    current = 0
    prev = dates[0]
    for d in dates:
        if d != prev:
            groups.append(current)
            current = 0
        current += 1
        prev = d
    groups.append(current)
    return groups

train_group = build_groups(df_feat[train_mask]["date"].values)
val_group = build_groups(df_feat[val_mask]["date"].values)

dtrain = lgb.Dataset(X_train, y_train, group=train_group)
dval = lgb.Dataset(X_val, y_val, group=val_group, reference=dtrain)

params = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "ndcg_eval_at": [10, 30],
    "boosting_type": "gbdt",
    "num_leaves": 64,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 50,
    "verbose": 0,
    "num_threads": 4,
}

model = lgb.train(
    params,
    dtrain,
    valid_sets=[dtrain, dval],
    valid_names=["train", "val"],
    num_boost_round=300,
    callbacks=[
        lgb.early_stopping(stopping_rounds=50),
        lgb.log_evaluation(period=50),
    ],
)

elapsed = time.time() - t0
print(f"训练完成: {elapsed:.1f}s")

# 5. 评估
y_prob = model.predict(X_test)
spearman, _ = spearmanr(y_prob, y_test)
print(f"\n{'='*60}")
print(f"  测试集结果")
print(f"  Spearman: {spearman:.4f}")
print(f"{'='*60}")

# 分层评估
n_bins = 5
boundaries = np.percentile(y_prob, np.linspace(0, 100, n_bins + 1))
boundaries[0] = -0.01; boundaries[-1] = 1.01

print(f"\n  分组收益 (预测排序):")
print(f"  {'组别':<8} {'预测区间':<20} {'样本':<8} {'均收益%':<10} {'中位收益%':<10}")
print(f"  {'-'*50}")
for i in range(n_bins):
    mask = (y_prob > boundaries[i]) & (y_prob <= boundaries[i + 1])
    n = mask.sum()
    if n > 0:
        name = ["最低", "较低", "中性", "较高", "最高"][i]
        print(f"  {name:<8} [{boundaries[i]:.2f},{boundaries[i+1]:.2f}]     "
              f"{n:<8} {ret_test[mask].mean():+.<10.4f} {np.median(ret_test[mask]):+.<10.4f}")

# 特征重要性
importance = pd.DataFrame({
    "feature": [f"{FEATURE_COLS[i % len(FEATURE_COLS)]}_"
                f"{['latest','m20','s20','m60','s60'][i//len(FEATURE_COLS)]}"
                for i in range(X_train.shape[1])],
    "importance": model.feature_importance(importance_type="gain"),
}).sort_values("importance", ascending=False)

print(f"\n  Top 15 特征:")
for _, row in importance.head(15).iterrows():
    print(f"    {row['feature']:40s} {row['importance']:10.0f}")

if spearman > 0.03:
    print(f"\n  ✅ Spearman={spearman:.4f} > 0.03 — 排序学习有信号！")
else:
    print(f"\n  ⚠️ Spearman={spearman:.4f} — 仍缺乏预测信号")
