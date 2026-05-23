#!/usr/bin/env python3
"""
预测与选股脚本
===============
加载训练好的模型，对最近一个交易日全市场股票进行预测，
输出排序后的选股列表。

用法:
    python ml/predict.py                          # 默认预测
    python ml/predict.py --model best.pt --top 30  # Top30
    python ml/predict.py --date 2026-05-22          # 指定历史日期（回测用）

输出:
    output/ml_predictions_YYYYMMDD.csv  - 全市场预测排名
"""

import argparse
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ml.dataset import (
    FEATURES_PARQUET,
    BENCHMARK_PARQUET,
    SEQ_LEN,
    N_FEATURES,
    FEATURE_COLS,
)
from ml.train import StockPredictor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("predict")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML_DIR = os.path.join(ROOT_DIR, "data", "ml")
MODEL_DIR = os.path.join(ML_DIR, "models")
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model_and_stats(model_path: str) -> tuple[StockPredictor, dict]:
    """加载模型和标准化参数"""
    if not os.path.isabs(model_path):
        model_path = os.path.join(MODEL_DIR, os.path.basename(model_path))

    checkpoint = torch.load(model_path, map_location=DEVICE, weights_only=False)
    model_args = checkpoint.get("args", {})
    stats = checkpoint.get("stats", {})

    model = StockPredictor(
        n_features=N_FEATURES,
        hidden_dim=model_args.get("hidden_dim", 128),
        mlp_dims=tuple(model_args.get("mlp_dims", [512, 256, 128])),
        dropout=model_args.get("dropout", 0.3),
    ).to(DEVICE)

    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    log.info("模型加载成功: epoch=%d, val_auc=%.4f",
             checkpoint.get("epoch", 0), checkpoint.get("val_auc", 0))
    return model, stats


def predict_latest(
    model: StockPredictor,
    stats: dict,
    target_date: str | None = None,
    top_n: int = 50,
    output_csv: str | None = None,
):
    """
    对 target_date 当天所有股票的预测。

    处理流程:
      1. 加载特征矩阵
      2. 对每只股票，取 [target_date - 180, target_date] 的特征序列
      3. 标准化 → 模型预测
      4. 按概率排序输出
    """
    # 1. 加载数据
    df = pd.read_parquet(FEATURES_PARQUET)
    df["date"] = pd.to_datetime(df["date"])

    if target_date is None:
        target_date = str(df["date"].max().date())
        log.info("使用最新日期: %s", target_date)

    dt = pd.Timestamp(target_date)
    mean = stats["mean"]
    std = stats["std"]

    # 2. 逐只预测
    results = []
    all_codes = sorted(df["code"].unique())
    log.info("预测日期: %s, 股票数: %d", target_date, len(all_codes))

    for code in all_codes:
        code_df = df[df["code"] == code].sort_values("date")

        # 找到 target_date 的位置
        mask = code_df["date"] <= dt
        if mask.sum() < SEQ_LEN:
            continue

        code_data = code_df[mask].tail(SEQ_LEN)

        # 检查数据充足
        if len(code_data) < SEQ_LEN * 0.8:
            continue

        # 取特征
        feat = code_data[FEATURE_COLS].values[-SEQ_LEN:].astype(np.float32)

        # 如果不足SEQ_LEN，在前面补零
        if len(feat) < SEQ_LEN:
            pad = np.zeros((SEQ_LEN - len(feat), N_FEATURES), dtype=np.float32)
            feat = np.concatenate([pad, feat], axis=0)

        # 标准化
        feat = (feat - mean) / std
        feat = np.nan_to_num(feat, nan=0.0, posinf=5.0, neginf=-5.0)

        # 预测
        X = torch.from_numpy(feat).unsqueeze(0).to(DEVICE)  # (1, 180, 34)
        with torch.no_grad():
            logit = model(X)
            prob = torch.sigmoid(logit).item()

        # 获得当前价格
        current_close = code_df[mask].tail(1)["close"].values[0]

        results.append({
            "代码": code,
            "预测概率": round(prob, 4),
            "当前价格": round(float(current_close), 2),
        })

    # 3. 排序输出
    df_result = pd.DataFrame(results).sort_values("预测概率", ascending=False)
    df_result["排名"] = range(1, len(df_result) + 1)
    df_result = df_result[["排名", "代码", "预测概率", "当前价格"]]

    # Top N
    df_top = df_result.head(top_n)

    # 4. 保存
    date_str = dt.strftime("%Y%m%d")
    if output_csv is None:
        output_csv = os.path.join(OUTPUT_DIR, f"ml_predictions_{date_str}.csv")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df_result.to_csv(output_csv, index=False, encoding="utf-8-sig")
    log.info("全市场预测已保存: %s (%d行)", output_csv, len(df_result))

    # 打印Top N
    print(f"\n{'='*60}")
    print(f"  预测日期: {target_date}")
    print(f"  Top {top_n} 选股")
    print(f"{'='*60}")
    print(df_top.to_string(index=False))

    # 概率分布统计
    print(f"\n  概率分布:")
    print(f"    均值:   {df_result['预测概率'].mean():.4f}")
    print(f"    中位数: {df_result['预测概率'].median():.4f}")
    print(f"    标准差: {df_result['预测概率'].std():.4f}")
    for thresh in [0.5, 0.6, 0.7, 0.8, 0.9]:
        n = (df_result["预测概率"] > thresh).sum()
        print(f"    >{thresh:.1f}: {n} 只 ({n/len(df_result):.1%})")

    return df_result


# ============================================================================
# 回测模式：逐日预测（验证历史排序能力）
# ============================================================================

def predict_history(
    model: StockPredictor,
    stats: dict,
    start_date: str = "2025-04-01",
    end_date: str = "2026-05-20",
    top_n: int = 50,
    step_days: int = 5,
):
    """
    对历史日期逐日预测，然后计算 Top-K 实际收益。
    用于验证模型在测试集上的选股表现。
    """
    df = pd.read_parquet(FEATURES_PARQUET)
    df["date"] = pd.to_datetime(df["date"])

    # 生成所有调仓日期
    all_dates = df["date"].unique()
    all_dates = all_dates[(all_dates >= start_date) & (all_dates <= end_date)]
    all_dates = sorted(all_dates)

    # 每隔 step_days 取一个调仓日
    rebalance_dates = all_dates[::step_days]
    log.info("回测日期范围: %s ~ %s, 调仓日: %d",
             start_date, end_date, len(rebalance_dates))

    mean = stats["mean"]
    std = stats["std"]

    records = []
    for dt in rebalance_dates:
        dt = pd.Timestamp(dt)

        # 对当天所有股票预测
        predictions = []
        for code in sorted(df["code"].unique()):
            code_df = df[df["code"] == code].sort_values("date")
            mask = code_df["date"] <= dt
            if mask.sum() < SEQ_LEN:
                continue

            code_data = code_df[mask].tail(SEQ_LEN)
            if len(code_data) < SEQ_LEN * 0.8:
                continue

            feat = code_data[FEATURE_COLS].values[-SEQ_LEN:].astype(np.float32)
            if len(feat) < SEQ_LEN:
                pad = np.zeros((SEQ_LEN - len(feat), N_FEATURES), dtype=np.float32)
                feat = np.concatenate([pad, feat], axis=0)

            feat = (feat - mean) / std
            feat = np.nan_to_num(feat, nan=0.0, posinf=5.0, neginf=-5.0)

            X = torch.from_numpy(feat).unsqueeze(0).to(DEVICE)
            with torch.no_grad():
                prob = torch.sigmoid(model(X)).item()

            # 当前价格
            current_close = code_df[mask].tail(1)["close"].values[0]

            predictions.append({
                "code": code,
                "prob": prob,
                "close_t": float(current_close),
            })

        if len(predictions) < top_n:
            continue

        df_pred = pd.DataFrame(predictions).sort_values("prob", ascending=False)
        topk = df_pred.head(top_n)

        # 计算未来5日真实收益
        future_dt = dt + pd.Timedelta(days=10)  # 找T+5附近的真实交易日
        returns = []
        for _, row in topk.iterrows():
            code = row["code"]
            code_df = df[df["code"] == code].sort_values("date")
            future_mask = (code_df["date"] > dt) & (code_df["date"] <= future_dt)
            future_data = code_df[future_mask]
            if len(future_data) >= 3:  # 至少3个交易日
                close_after = future_data["close"].iloc[-1]
                ret_5d = (close_after / row["close_t"] - 1) * 100
            else:
                ret_5d = 0.0
            returns.append(ret_5d)

        mean_ret = np.mean(returns)
        hit_rate = np.mean([r > 0 for r in returns])

        records.append({
            "date": dt.strftime("%Y-%m-%d"),
            "mean_return_5d": round(float(mean_ret), 4),
            "hit_rate": round(float(hit_rate), 4),
        })

        if len(records) % 10 == 0:
            log.info("回测进度: %d/%d, 最近均收益=%.4f",
                     len(records), len(rebalance_dates),
                     records[-1]["mean_return_5d"])

    # 汇总
    df_records = pd.DataFrame(records)
    print(f"\n{'='*60}")
    print(f"  回测结果 ({start_date} ~ {end_date})")
    print(f"{'='*60}")
    print(f"  调仓次数: {len(df_records)}")
    print(f"  平均5日收益: {df_records['mean_return_5d'].mean():.4f}%")
    print(f"  平均命中率:  {df_records['hit_rate'].mean():.2%}")
    print(f"  胜率(正收益): {(df_records['mean_return_5d'] > 0).mean():.2%}")

    # 保存
    output_csv = os.path.join(OUTPUT_DIR, "ml_backtest_summary.csv")
    df_records.to_csv(output_csv, index=False, encoding="utf-8-sig")
    log.info("回测汇总已保存: %s", output_csv)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ML预测选股")
    parser.add_argument("--model", type=str, default=None,
                        help="模型路径（默认使用models/下最新的）")
    parser.add_argument("--date", type=str, default=None,
                        help="预测日期（默认最新）")
    parser.add_argument("--top", type=int, default=30,
                        help="输出Top N")
    parser.add_argument("--output", type=str, default=None)

    # 回测模式
    parser.add_argument("--backtest", action="store_true",
                        help="回测模式：逐日预测历史")
    parser.add_argument("--start", type=str, default="2025-04-01",
                        help="回测起始日期")
    parser.add_argument("--end", type=str, default="2026-05-20",
                        help="回测结束日期")

    args = parser.parse_args()

    # 找到最新模型
    model_path = args.model
    if model_path is None:
        models = sorted([
            f for f in os.listdir(MODEL_DIR) if f.endswith(".pt")
        ], reverse=True)
        if not models:
            raise FileNotFoundError(
                f"没有找到模型文件，请先运行: python ml/train.py")
        model_path = os.path.join(MODEL_DIR, models[0])
        log.info("使用最新模型: %s", models[0])

    model, stats = load_model_and_stats(model_path)

    if args.backtest:
        predict_history(
            model, stats,
            start_date=args.start,
            end_date=args.end,
            top_n=args.top,
        )
    else:
        predict_latest(
            model, stats,
            target_date=args.date,
            top_n=args.top,
            output_csv=args.output,
        )
