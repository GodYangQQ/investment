#!/usr/bin/env python3
"""
PyTorch Dataset — 动态序列切片
===============================
从 features.parquet 按日期动态切出 (180, 34) 序列样本，
不做预生成，内存占用仅 batch_size × 180 × 34 张量。

时间切分（严格无未来函数）:
  训练集: 2022-01-01 ~ 2024-06-30
  验证集: 2024-07-01 ~ 2025-03-31
  测试集: 2025-04-01 ~ 2026-05-23

用法:
    from ml.dataset import StockSequenceDataset, prepare_dataloaders

    loaders = prepare_dataloaders(batch_size=256)
    for X, y in loaders["train"]:
        ...  # X: (B, 180, 34), y: (B,)
"""

import os
import sys
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.sampler import Sampler

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML_DIR = os.path.join(ROOT_DIR, "data", "ml")
FEATURES_PARQUET = os.path.join(ML_DIR, "features.parquet")
BENCHMARK_PARQUET = os.path.join(ML_DIR, "benchmark.parquet")

# 特征维度
from ml.build_features import FEATURE_COLS

N_FEATURES = len(FEATURE_COLS)

# 时间切分点（根据实际数据范围动态调整）
# 默认数据范围: 2024-03 ~ 2026-05
TRAIN_END = pd.Timestamp("2025-03-31")
VAL_END = pd.Timestamp("2025-10-31")
TEST_END = pd.Timestamp("2026-05-23")

# 序列窗口
SEQ_LEN = 120        # 输入: 过去120个交易日
FUTURE_DAYS = 20     # 输出: 未来20个交易日超额收益 > 0


class StockSequenceDataset(Dataset):
    """
    动态序列切片 Dataset。

    每次 __getitem__:
      1. 从 parquet 中取出某只股票 [T-SEQ_LEN, T] 行的特征
      2. 从 parquet 中取出 [T+1, T+FUTURE_DAYS] 的 close 计算标签
      3. 从 benchmark 中取出同期指数收益，计算超额收益
      4. 返回 (feature_tensor, label)

    支持日期采样策略:
      - "random": 随机采样 (训练用，增加多样性)
      - "sequential": 按股票×日期顺序遍历 (验证/测试用)
    """

    def __init__(
        self,
        df_features: pd.DataFrame,
        df_bench: pd.DataFrame,
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
        mode: str = "random",
        normalize: bool = True,
        stats: Optional[dict] = None,  # 外部传入的标准化参数
    ):
        self.df = df_features
        self.bench = df_bench
        self.mode = mode
        self.normalize = normalize

        # 过滤日期范围
        self.df = self.df[
            (self.df["date"] >= start_date) &
            (self.df["date"] <= end_date)
        ].copy()

        # 构建有效样本索引: (code, date) 对
        # 每个样本需要: 前180天有数据 + 后5天有数据
        self.samples = self._build_sample_index()

        # 标准化
        if normalize:
            if stats is not None:
                self.mean = stats["mean"]
                self.std = stats["std"]
            else:
                self.mean, self.std = self._compute_stats()
        else:
            self.mean, self.std = None, None

        # 构建日期 → benchmark return 映射
        self._build_bench_map()

        # 按 code+date 建立快速索引
        self._build_feature_index()

    def _build_sample_index(self) -> list[tuple[str, pd.Timestamp]]:
        """
        找到所有有效样本点。
        有效条件: 该 code 在 [T-SEQ_LEN, T+FUTURE_DAYS] 范围内都有数据。
        """
        samples = []
        for code, grp in self.df.groupby("code"):
            dates = grp["date"].sort_values().values
            if len(dates) < SEQ_LEN + FUTURE_DAYS + 1:
                continue

            # 遍历每个可能的 T
            for i in range(SEQ_LEN - 1, len(dates) - FUTURE_DAYS):
                # 验证前 SEQ_LEN 天是否连续（检查跨度）
                seq_dates = dates[i - SEQ_LEN + 1: i + 1]
                if len(seq_dates) < SEQ_LEN:
                    continue
                # 简单检查：首尾日期差不超过 SEQ_LEN * 3 天（允许周末/假期）
                date_span = seq_dates[-1] - seq_dates[0]
                if date_span > pd.Timedelta(days=SEQ_LEN * 3):
                    continue

                samples.append((code, dates[i]))

        return samples

    def _build_bench_map(self):
        """构建日期 → 基准收益映射"""
        self.bench_map = {}
        for _, row in self.bench.iterrows():
            self.bench_map[row["date"]] = row["return"]

    def _build_feature_index(self):
        """按 (code, date) 快速定位特征行"""
        self.feat_index = {}
        if "date" not in self.df.columns:
            return

        # 按code分组建索引
        self._code_data = {}
        for code, grp in self.df.groupby("code"):
            grp_sorted = grp.sort_values("date")
            self._code_data[code] = {
                "dates": grp_sorted["date"].values,
                "feat": grp_sorted[FEATURE_COLS].values.astype(np.float32),
                "close": grp_sorted["close"].values.astype(np.float32),
            }

    def _compute_stats(self) -> tuple[np.ndarray, np.ndarray]:
        """计算训练集特征均值和标准差（用于标准化）"""
        all_feat = []
        for code, grp in self.df.groupby("code"):
            feat = grp[FEATURE_COLS].values
            all_feat.append(feat[~np.isnan(feat).any(axis=1)])

        all_feat = np.concatenate(all_feat, axis=0)
        mean = np.nanmean(all_feat, axis=0).astype(np.float32)
        std = np.nanstd(all_feat, axis=0).astype(np.float32)
        std = np.clip(std, 1e-8, None)  # 防止除零
        return mean, std

    def get_stats(self) -> dict:
        return {"mean": self.mean, "std": self.std}

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        code, date_t = self.samples[idx]

        # 从索引中获取该股票的数据
        code_data = self._code_data[code]
        dates = code_data["dates"]
        feat = code_data["feat"]
        closes = code_data["close"]

        # 定位 T 的位置
        t_pos = np.searchsorted(dates, date_t)
        if t_pos >= len(dates) or dates[t_pos] != date_t:
            # 降级：找最近日期
            t_pos = np.argmin(np.abs(dates - date_t))

        # 切出 [T-SEQ_LEN+1, T] 的特征
        start_pos = max(0, t_pos - SEQ_LEN + 1)
        seq_feat = feat[start_pos: t_pos + 1]

        # 如果不足 SEQ_LEN，pad
        if len(seq_feat) < SEQ_LEN:
            pad_len = SEQ_LEN - len(seq_feat)
            pad = np.zeros((pad_len, N_FEATURES), dtype=np.float32)
            seq_feat = np.concatenate([pad, seq_feat], axis=0)

        # 标准化
        if self.normalize and self.mean is not None:
            seq_feat = (seq_feat - self.mean) / self.std

        # 填充 NaN
        seq_feat = np.nan_to_num(seq_feat, nan=0.0, posinf=5.0, neginf=-5.0)

        seq_feat = torch.from_numpy(seq_feat).float()

        # 标签: 未来5日超额收益
        # 个股未来5日收益
        if t_pos + FUTURE_DAYS < len(closes):
            future_close = closes[t_pos + FUTURE_DAYS]
            current_close = closes[t_pos]
            stock_return = (future_close / current_close - 1) * 100
        else:
            stock_return = 0.0

        # 基准未来5日收益
        bench_return = 0.0
        future_dates = pd.date_range(date_t + pd.Timedelta(days=1),
                                     periods=FUTURE_DAYS, freq="B")
        last_bench_return = 0.0
        last_bench_close = 1.0
        for fd in future_dates:
            fd_ts = pd.Timestamp(fd)
            if fd_ts in self.bench_map:
                last_bench_return = self.bench_map[fd_ts]

        # 简化：用基准5日后累积收益
        # 查找 T 时刻基准位置
        bench_dates = list(self.bench_map.keys())
        bench_returns = list(self.bench_map.values())
        try:
            t_bench_pos = bench_dates.index(date_t)
        except ValueError:
            # 找最近日期
            bench_date_arr = np.array([d.to_numpy() for d in bench_dates], dtype="datetime64[D]")
            t_bench_pos = np.argmin(np.abs(bench_date_arr - np.datetime64(date_t.to_numpy())))

        # 未来5日累积
        total_bench = 0.0
        for i in range(1, FUTURE_DAYS + 1):
            pos = t_bench_pos + i
            if pos < len(bench_returns):
                total_bench += bench_returns[pos]
        bench_return = total_bench

        excess_return = stock_return - bench_return
        label = 1.0 if excess_return > 0 else 0.0

        return seq_feat, torch.tensor(label, dtype=torch.float32), torch.tensor(excess_return, dtype=torch.float32)


def _load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载特征和基准数据"""
    if not os.path.exists(FEATURES_PARQUET):
        raise FileNotFoundError(
            f"特征文件不存在: {FEATURES_PARQUET}\n"
            f"请先运行: python ml/build_features.py"
        )

    df = pd.read_parquet(FEATURES_PARQUET)
    df["date"] = pd.to_datetime(df["date"])

    bench = None
    if os.path.exists(BENCHMARK_PARQUET):
        bench = pd.read_parquet(BENCHMARK_PARQUET)
        bench["date"] = pd.to_datetime(bench["date"])
    else:
        # 创建空的benchmark
        bench = pd.DataFrame(columns=["date", "return"])

    return df, bench


def prepare_dataloaders(
    batch_size: int = 256,
    num_workers: int = 0,
    train_end: pd.Timestamp = TRAIN_END,
    val_end: pd.Timestamp = VAL_END,
    test_end: pd.Timestamp = TEST_END,
    seq_len: int = SEQ_LEN,
    future_days: int = FUTURE_DAYS,
) -> dict:
    """
    准备训练/验证/测试 DataLoader。

    Returns:
        {
            "train": DataLoader,
            "val": DataLoader,
            "test": DataLoader,
            "stats": dict (mean/std for inference),
            "n_features": int,
        }
    """
    global SEQ_LEN, FUTURE_DAYS
    SEQ_LEN = seq_len
    FUTURE_DAYS = future_days

    df, bench = _load_data()

    # 训练集: 用训练集数据计算标准化参数
    ds_train = StockSequenceDataset(
        df, bench,
        start_date=pd.Timestamp("2022-01-01"),
        end_date=train_end,
        mode="random",
        normalize=True,
    )
    stats = ds_train.get_stats()

    # 验证集
    ds_val = StockSequenceDataset(
        df, bench,
        start_date=train_end + pd.Timedelta(days=1),
        end_date=val_end,
        mode="sequential",
        normalize=True,
        stats=stats,
    )

    # 测试集
    ds_test = StockSequenceDataset(
        df, bench,
        start_date=val_end + pd.Timedelta(days=1),
        end_date=test_end,
        mode="sequential",
        normalize=True,
        stats=stats,
    )

    for name, ds in [("train", ds_train), ("val", ds_val), ("test", ds_test)]:
        print(f"[{name}] 样本数: {len(ds)}")

    loaders = {
        "train": DataLoader(ds_train, batch_size=batch_size, shuffle=True,
                            num_workers=num_workers, pin_memory=False, drop_last=True),
        "val": DataLoader(ds_val, batch_size=batch_size, shuffle=False,
                          num_workers=num_workers, pin_memory=False),
        "test": DataLoader(ds_test, batch_size=batch_size, shuffle=False,
                           num_workers=num_workers, pin_memory=False),
        "stats": stats,
        "ds_val": ds_val,   # 保留引用用于分层评估
        "ds_test": ds_test,
    }

    return loaders


# ============================================================================
# 验证
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("数据集验证")
    print("=" * 60)

    loaders = prepare_dataloaders(batch_size=64)

    # 检查一个batch
    for X, y, excess in loaders["train"]:
        print(f"特征形状: {X.shape}")        # (B, 180, 34)
        print(f"标签形状: {y.shape}")         # (B,)
        print(f"标签分布: 正例={y.sum():.0f}/{len(y)} ({y.mean():.1%})")
        print(f"超额收益范围: [{excess.min():.2f}, {excess.max():.2f}]")
        print(f"特征均值: {X.mean():.4f}, 特征标准差: {X.std():.4f}")
        break

    print(f"\n标准化参数:")
    stats = loaders["stats"]
    for i, (m, s) in enumerate(zip(stats["mean"], stats["std"])):
        print(f"  {FEATURE_COLS[i]:25s}  μ={m:8.4f}  σ={s:8.4f}")
