#!/usr/bin/env python3
"""
特征矩阵构建脚本
================
拉取全A股历史K线 → 计算技术指标 → 存为 Parquet 文件。
生成后供 dataset.py 动态切片，避免预先生成庞大的序列文件。

用法:
    python ml/build_features.py                  # 首次构建（约15-30分钟）
    python ml/build_features.py --update         # 增量更新（只补最新N天）
    python ml/build_features.py --pool-size 3000  # 限定股票数量

输出:
    data/ml/features.parquet    - 特征矩阵 (N_stocks × T_days, 30+特征列)
    data/ml/benchmark.parquet   - 基准指数K线（用于计算超额收益）
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.stock_strategy import (
    fetch_daily_kline,
    compute_indicators,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML_DIR = os.path.join(ROOT_DIR, "data", "ml")
FEATURES_PARQUET = os.path.join(ML_DIR, "features.parquet")
BENCHMARK_PARQUET = os.path.join(ML_DIR, "benchmark.parquet")
KLINE_DAYS = 500          # 取近500个交易日K线
DEFAULT_WORKERS = 8
BATCH_SAVE_INTERVAL = 100  # 每100只写盘一次

# 基本面过滤参数
MAX_PE = 500               # PE超过此值视为异常，跳过
EXCLUDE_NEGATIVE_PE = True  # 排除PE<=0（亏损/数据缺失）

# 特征列列表（v5：排序学习版，25因子）
FEATURE_COLS = [
    # ═══ 动量 ═══
    "return_5d",           # 5日收益
    "return_20d",          # 20日收益（动量因子）
    "return_60d",          # 60日收益
    "ret_short_div_long",  # 短期/长期收益比（反转vs趋势）

    # ═══ RSI ═══
    "rsi6", "rsi14", "rsi24",

    # ═══ 均线综合 ═══
    "ma_trend_score",      # MA排列+偏离
    "ma60_distance",       # 价格距MA60的百分比

    # ═══ MACD ═══
    "macd_hist",
    "macd_divergence",

    # ═══ 布林带 ═══
    "bb_position",

    # ═══ 成交量 ═══
    "vol_ratio_5_20",
    "vol_trend_10d",
    "vol_shrink",          # 缩量程度(当日量/20日均量最低值)

    # ═══ 量价关系 ═══
    "vol_up_down_ratio_10d",
    "price_vol_corr_20d",
    "volume_divergence",

    # ═══ 资金流 ═══
    "cmf_20d",
    "obv_ratio",

    # ═══ 波动率 ═══
    "atr_ratio",
    "volatility_20d",
    "inv_volatility",      # 波动率倒数（低波异象）

    # ═══ 价格位置 ═══
    "price_pos_60d",
    "high_low_range_20d",

    # ═══ 基本面占位（enrich填充）═══
    "pe_ttm", "pb", "turnover_rate",
]

assert len(FEATURE_COLS) == 28


def _get_all_a_stock_codes() -> list[str]:
    """获取全部A股代码（优先用akshare，失败降级到新浪）"""
    # 方法1: akshare (更稳定)
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        codes = [str(c).zfill(6) for c in df["code"].values]
        log.info("从akshare获取到 %d 个股票代码", len(codes))
        return codes
    except Exception as e:
        log.warning("akshare获取代码失败: %s，降级到新浪", e)

    # 方法2: 新浪 (可能被限流)
    import requests
    log.info("从新浪获取全A股代码列表...")
    count_url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                 "json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a")
    try:
        count = int(requests.get(count_url, timeout=10).text.strip('"'))
    except Exception:
        log.error("新浪API不可用，无法获取股票代码")
        raise RuntimeError("无法获取A股代码列表，请检查网络或安装akshare")

    log.info("全A股数量: %d", count)

    codes = []
    page_size = 80
    for page in range(1, count // page_size + 2):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
               "json_v2.php/Market_Center.getHQNodeData")
        params = {
            "page": page, "num": page_size, "sort": "symbol",
            "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page",
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
            if not data:
                break
            codes.extend([item["code"] for item in data])
        except Exception:
            break
        time.sleep(0.1)

    log.info("获取到 %d 个股票代码", len(codes))
    return codes


def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
    """手动计算RSI"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _extract_features(df: pd.DataFrame, df_bench: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    从 compute_indicators 输出中提取标准化数值特征（v2）。

    输入:
        df:      compute_indicators 输出的 DataFrame (含 56列指标)
        df_bench: 基准K线(含 date/close/return)，用于 rel_strength_20d
    输出:
        仅含 FEATURE_COLS (32列) 的 DataFrame
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    f = pd.DataFrame(index=df.index)

    # ═══ 动量 ═══
    f["return_5d"] = close.pct_change(5) * 100
    f["return_20d"] = close.pct_change(20) * 100
    f["return_60d"] = close.pct_change(60) * 100
    f["ret_short_div_long"] = f["return_5d"] / f["return_60d"].replace(0, np.nan)

    # ═══ RSI（多周期）═══
    f["rsi6"] = _compute_rsi(close, 6)
    f["rsi14"] = df.get("rsi14", _compute_rsi(close, 14))
    f["rsi24"] = _compute_rsi(close, 24)

    # ═══ 均线综合分 ═══
    # 综合 MA排列 和 偏离度：MA5>MA10>MA20=多头 + 价格离MA60不远
    f["ma_trend_score"] = (close / df["ma60"] - 1).clip(-0.5, 0.5) * 100
    # 多头排列加分
    ma_alignment = ((df["ma5"] > df["ma10"]).astype(float) +
                    (df["ma10"] > df["ma20"]).astype(float) +
                    (df["ma20"] > df["ma60"]).astype(float)) / 3  # 0~1
    f["ma_trend_score"] = f["ma_trend_score"] + ma_alignment * 50
    f["ma60_distance"] = (close / df["ma60"] - 1) * 100

    # ═══ MACD ═══
    f["macd_hist"] = df["macd_hist"]
    # MACD背离：价格创新低但MACD柱不创新低
    price_20d_low = close.rolling(20).min()
    macd_20d_low = df["macd_hist"].rolling(20).min()
    new_price_low = close <= price_20d_low.shift(1)
    macd_not_low = df["macd_hist"] > macd_20d_low.shift(1)
    f["macd_divergence"] = (new_price_low & macd_not_low).astype(float)

    # ═══ 布林带 ═══
    f["bb_position"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)

    # ═══ 成交量 ═══
    f["vol_ratio_5_20"] = df["vol_ma5"] / df["vol_ma20"].replace(0, np.nan)
    f["vol_trend_10d"] = df.get("vol_trend_10d", np.nan)
    vol_20d_min = volume.rolling(20).min().replace(0, np.nan)
    f["vol_shrink"] = volume / vol_20d_min

    # ═══ 量价关系 ═══
    f["vol_up_down_ratio_10d"] = (df.get("vol_up_ma10", np.nan) /
                                  df.get("vol_down_ma10", np.nan).replace(0, np.nan))
    f["price_vol_corr_20d"] = df.get("price_vol_corr_20d", np.nan)
    # 量价背离：涨但缩量，或跌但放量
    price_up = close.diff(5) > 0
    vol_up = volume.rolling(5).mean().diff(5) > 0
    f["volume_divergence"] = ((price_up & ~vol_up) | (~price_up & vol_up)).astype(float)

    # ═══ 资金流（技术面）═══
    f["cmf_20d"] = df.get("cmf_20d", np.nan)
    f["obv_ratio"] = df.get("obv_ma10", np.nan) / df.get("obv_ma20", np.nan).replace(0, np.nan)

    # ═══ 波动率 ═══
    f["atr_ratio"] = df["atr14"] / close
    f["volatility_20d"] = close.pct_change().rolling(20).std() * 100
    f["inv_volatility"] = 1 / f["volatility_20d"].replace(0, np.nan)

    # ═══ 价格位置 ═══
    high60 = high.rolling(60).max()
    low60 = low.rolling(60).min()
    f["price_pos_60d"] = (close - low60) / (high60 - low60).replace(0, np.nan) * 100
    f["high_low_range_20d"] = (high.rolling(20).max() / low.rolling(20).min().replace(0, np.nan) - 1) * 100

    # ═══ 基本面占位（由 enrich 填充真实值）═══
    f["pe_ttm"] = np.nan
    f["pb"] = np.nan
    f["turnover_rate"] = np.nan

    return f


def fetch_benchmark() -> pd.DataFrame:
    """获取沪深300指数K线作为基准"""
    log.info("获取沪深300(000300)基准K线...")
    # 使用腾讯K线接口
    pure, prefix = "000300", "sh"
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={prefix}{pure},day,,,{KLINE_DAYS},qfq")
    import requests
    r = requests.get(url, timeout=15)
    data = r.json()

    key = f"{prefix}{pure}"
    klines = data.get("data", {}).get(key, {}).get("qfqday", [])
    if not klines:
        klines = data.get("data", {}).get(key, {}).get("day", [])

    rows = []
    for k in klines:
        if len(k) >= 6:
            rows.append({
                "date": k[0],
                "close": float(k[2]),
            })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["return"] = df["close"].pct_change() * 100
    return df


def process_single_stock(args: tuple) -> pd.DataFrame | None:
    """处理单只股票：拉K线 → 算指标 → 提取特征"""
    code, df_bench = args
    try:
        df_kline = fetch_daily_kline(code, days=KLINE_DAYS)
        if df_kline.empty or len(df_kline) < 200:
            return None

        df_ind = compute_indicators(df_kline)
        df_feat = _extract_features(df_ind, df_bench)
        df_feat["code"] = code
        df_feat["date"] = df_kline["date"].values
        df_feat["close"] = df_kline["close"].values
        return df_feat
    except Exception as e:
        log.debug("处理 %s 失败: %s", code, e)
        return None


def build_features(pool_size: int | None = None, update: bool = False):
    """主流程：构建全市场特征矩阵"""
    os.makedirs(ML_DIR, exist_ok=True)

    # 0. 构建/加载基准（必须，用于 rel_strength_20d）
    if not os.path.exists(BENCHMARK_PARQUET):
        df_bench = fetch_benchmark()
        df_bench.to_parquet(BENCHMARK_PARQUET, index=False)
        log.info("基准数据已保存: %s", BENCHMARK_PARQUET)
    else:
        df_bench = pd.read_parquet(BENCHMARK_PARQUET)
        df_bench["date"] = pd.to_datetime(df_bench["date"])

    # 1. 获取股票代码
    all_codes = _get_all_a_stock_codes()
    if pool_size and pool_size < len(all_codes):
        all_codes = all_codes[:pool_size]
        log.info("限定股票池: %d只", pool_size)

    # 2. 增量模式：加载已有数据，跳过已处理的
    existing_codes = set()
    if update and os.path.exists(FEATURES_PARQUET):
        existing = pd.read_parquet(FEATURES_PARQUET, columns=["code", "date"])
        max_existing_date = existing["date"].max()
        log.info("增量模式：已有数据截止 %s", max_existing_date)
        recent_cutoff = pd.Timestamp.now() - pd.Timedelta(days=2)
        for code, grp in existing.groupby("code"):
            if grp["date"].max() >= recent_cutoff:
                existing_codes.add(code)
        to_process = [c for c in all_codes if c not in existing_codes]
        log.info("需更新: %d只 (跳过%d只)", len(to_process), len(existing_codes))
    else:
        to_process = all_codes

    if not to_process:
        log.info("所有股票数据已是最新，无需更新")
        return

    # 3. 多线程处理（传入benchmark）
    results = []
    failed = 0
    processed = 0
    lock = Lock()

    log.info("开始处理 %d 只股票 (%d线程)...", len(to_process), DEFAULT_WORKERS)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as executor:
        futures = {executor.submit(process_single_stock, (code, df_bench)): code
                   for code in to_process}

        for fut in as_completed(futures):
            code = futures[fut]
            try:
                df_feat = fut.result()
                if df_feat is not None and not df_feat.empty:
                    with lock:
                        results.append(df_feat)
                        processed += 1

                        if processed % BATCH_SAVE_INTERVAL == 0:
                            elapsed = time.time() - t0
                            speed = processed / elapsed * 60
                            log.info("进度: %d/%d (%.0f只/分钟), 失败%d",
                                     processed, len(to_process), speed, failed)

                        # 每500只自动存盘
                        if len(results) >= 500:
                            _save_results(results, existing_codes, update)
                            results = []
                else:
                    with lock:
                        failed += 1
            except Exception:
                with lock:
                    failed += 1

    # 4. 最后一批存盘
    if results:
        _save_results(results, existing_codes, update)

    elapsed = time.time() - t0
    log.info("完成! 成功=%d, 失败=%d, 耗时=%.1f分钟",
             processed, failed, elapsed / 60)


def _save_results(results: list[pd.DataFrame], existing_codes: set, update: bool):
    """将结果合并到已有 parquet"""
    if not results:
        return

    df_new = pd.concat(results, ignore_index=True)

    if update and os.path.exists(FEATURES_PARQUET):
        df_old = pd.read_parquet(FEATURES_PARQUET)
        # 移除这些股票在旧数据中的记录，替换为新数据
        new_codes = set(df_new["code"].unique())
        df_old = df_old[~df_old["code"].isin(new_codes)]
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new

    df_all.to_parquet(FEATURES_PARQUET, index=False, compression="zstd")
    log.info("已存盘: %d行, %d只股票", len(df_all), df_all["code"].nunique())


# ============================================================================
# 基本面 + 资金流向 enrichment（对已有 parquet 补充真实数据）
# ============================================================================

def enrich_features():
    """
    对已有 features.parquet 补充 PE/PB/换手率。
    通过腾讯实时行情快速获取，不调用 akshare。
    """
    if not os.path.exists(FEATURES_PARQUET):
        log.error("特征文件不存在，请先运行 build_features.py")
        return

    df = pd.read_parquet(FEATURES_PARQUET)
    codes = sorted(df["code"].unique())
    log.info("开始丰富 %d 只股票的 PE/PB/换手率...", len(codes))

    for i, code in enumerate(codes):
        mask = df["code"] == code
        try:
            quote = fetch_realtime_quote(code)
            pe = quote.get("pe_ttm", 0) or 0
            pb = quote.get("pb", 0) or 0
            turnover = quote.get("turnover", 0) or 0
            df.loc[mask, "pe_ttm"] = pe if pe > 0 else np.nan
            df.loc[mask, "pb"] = pb if pb > 0 else np.nan
            df.loc[mask, "turnover_rate"] = turnover if turnover > 0 else np.nan
        except Exception:
            pass

        if (i + 1) % 200 == 0:
            log.info("Enrich 进度: %d/%d", i + 1, len(codes))

    df.to_parquet(FEATURES_PARQUET, index=False, compression="zstd")
    log.info("Enrichment 完成! PE/PB/换手率已更新 (%d行)", len(df))


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="构建全A股特征矩阵")
    parser.add_argument("--update", action="store_true", help="增量更新")
    parser.add_argument("--pool-size", type=int, default=None,
                        help="限定股票数量（调试用）")
    parser.add_argument("--enrich", action="store_true",
                        help="对已有 parquet 补充基本面+资金流向数据")
    args = parser.parse_args()

    if args.enrich:
        enrich_features()
    else:
        build_features(pool_size=args.pool_size, update=args.update)
