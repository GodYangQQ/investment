#!/usr/bin/env python3
"""
SOXX 标的池
==========
默认研究宇宙 = iShares Semiconductor ETF (SOXX) 前 20 大持仓。
优先从 iShares 官网拉最新权重，失败则回退到内置快照。

用法:
    from core.datasets.universe import get_soxx_universe, get_soxx_tickers, refresh_pool_csv
    stocks = get_soxx_universe(top_n=20)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
POOL_CSV = os.path.join(ROOT_DIR, "data", "my_pool.csv")
SNAPSHOT_CSV = os.path.join(ROOT_DIR, "data", "soxx_holdings.csv")

SOXX_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239705/"
    "ishares-phlx-semiconductor-etf/latest-holdings.csv"
)

# 2026-09-28 iShares SOXX 前20（权重快照，网络失败时使用）
SOXX_TOP20_FALLBACK = [
    {"code": "INTC", "name": "Intel", "weight": 9.38, "exchange": "NASDAQ", "industry": "CPU/逻辑"},
    {"code": "AMD", "name": "AMD", "weight": 9.34, "exchange": "NASDAQ", "industry": "GPU/AI加速"},
    {"code": "MU", "name": "Micron", "weight": 7.95, "exchange": "NASDAQ", "industry": "存储"},
    {"code": "NVDA", "name": "NVIDIA", "weight": 7.50, "exchange": "NASDAQ", "industry": "GPU/AI加速"},
    {"code": "AVGO", "name": "Broadcom", "weight": 6.83, "exchange": "NASDAQ", "industry": "连接/高速接口"},
    {"code": "MRVL", "name": "Marvell", "weight": 4.31, "exchange": "NASDAQ", "industry": "连接/高速接口"},
    {"code": "QCOM", "name": "Qualcomm", "weight": 3.98, "exchange": "NASDAQ", "industry": "移动SoC/无线"},
    {"code": "ADI", "name": "Analog Devices", "weight": 3.95, "exchange": "NASDAQ", "industry": "模拟/MCU"},
    {"code": "KLAC", "name": "KLA", "weight": 3.90, "exchange": "NASDAQ", "industry": "设备"},
    {"code": "TXN", "name": "Texas Instruments", "weight": 3.86, "exchange": "NASDAQ", "industry": "模拟/MCU"},
    {"code": "AMAT", "name": "Applied Materials", "weight": 3.84, "exchange": "NASDAQ", "industry": "设备"},
    {"code": "LRCX", "name": "Lam Research", "weight": 3.77, "exchange": "NASDAQ", "industry": "设备"},
    {"code": "MPWR", "name": "Monolithic Power", "weight": 3.20, "exchange": "NASDAQ", "industry": "电源管理"},
    {"code": "TER", "name": "Teradyne", "weight": 3.12, "exchange": "NASDAQ", "industry": "设备"},
    {"code": "SKHY", "name": "SK Hynix ADR", "weight": 3.06, "exchange": "NASDAQ", "industry": "存储"},
    {"code": "TSM", "name": "TSMC", "weight": 3.02, "exchange": "NYSE", "industry": "代工"},
    {"code": "NXPI", "name": "NXP", "weight": 2.98, "exchange": "NASDAQ", "industry": "模拟/MCU"},
    {"code": "ALAB", "name": "Astera Labs", "weight": 2.50, "exchange": "NASDAQ", "industry": "连接/高速接口"},
    {"code": "MCHP", "name": "Microchip", "weight": 2.08, "exchange": "NASDAQ", "industry": "模拟/MCU"},
    {"code": "CRDO", "name": "Credo", "weight": 1.63, "exchange": "NASDAQ", "industry": "连接/高速接口"},
]

# GPU/AI 核心（动量加分用）
GPU_AI_CORE = {"NVDA", "AMD", "AVGO", "TSM", "ALAB", "CRDO", "MRVL"}

CASH_TICKERS = {"XTSLA", "USD", "WFFUT", "IXTZ6", "CASH"}


def _industry_of(ticker: str, name: str = "") -> str:
    from core.filters.market_filter import get_market
    return get_market(ticker) or "半导体"


def fetch_soxx_holdings_live(top_n: int = 20) -> list[dict]:
    """从 iShares 拉取 SOXX 最新持仓。失败抛异常。"""
    df = pd.read_csv(SOXX_HOLDINGS_URL, skiprows=9)
    df.columns = [str(c).strip() for c in df.columns]
    if "Ticker" not in df.columns:
        raise ValueError("iShares CSV 缺少 Ticker 列")

    asset_col = "Asset Class" if "Asset Class" in df.columns else None
    if asset_col:
        df = df[df[asset_col].astype(str).str.lower() == "equity"]

    weight_col = "Weight (%)" if "Weight (%)" in df.columns else None
    if weight_col:
        df[weight_col] = pd.to_numeric(df[weight_col], errors="coerce")
        df = df.sort_values(weight_col, ascending=False)

    rows = []
    for _, r in df.iterrows():
        ticker = str(r.get("Ticker", "")).strip().upper().replace('"', "")
        if not ticker or ticker in CASH_TICKERS:
            continue
        name = str(r.get("Name", ticker)).strip().replace('"', "")
        exchange = str(r.get("Exchange", "")).strip().replace('"', "")
        weight = float(r[weight_col]) if weight_col and pd.notna(r.get(weight_col)) else 0.0
        rows.append({
            "code": ticker,
            "name": name.title() if name.isupper() else name,
            "weight": round(weight, 2),
            "exchange": exchange or "NASDAQ",
            "industry": _industry_of(ticker, name),
        })
        if len(rows) >= top_n:
            break

    if len(rows) < 5:
        raise ValueError(f"SOXX 持仓过少: {len(rows)}")
    return rows


def get_soxx_universe(top_n: int = 20, live: bool = True) -> list[dict]:
    """
    返回 [{"code","name","weight","exchange","industry"}, ...]
    live=True 时尝试拉最新持仓，失败回退快照。
    """
    if live:
        try:
            rows = fetch_soxx_holdings_live(top_n=top_n)
            log.info("SOXX 最新持仓已加载: %d 只 (iShares)", len(rows))
            return rows
        except Exception as e:
            log.warning("拉取 SOXX 最新持仓失败，使用内置快照: %s", e)

    return [dict(x) for x in SOXX_TOP20_FALLBACK[:top_n]]


def get_soxx_tickers(top_n: int = 20, live: bool = True) -> list[str]:
    return [s["code"] for s in get_soxx_universe(top_n=top_n, live=live)]


def refresh_pool_csv(path: Optional[str] = None, top_n: int = 20) -> str:
    """把 SOXX 前N写入 data/my_pool.csv（保留已有成本/持仓）。"""
    path = path or POOL_CSV
    os.makedirs(os.path.dirname(path), exist_ok=True)

    existing = {}
    if os.path.exists(path):
        try:
            old = pd.read_csv(path, dtype=str)
            for _, row in old.iterrows():
                code = str(row.get("代码", "")).strip().upper()
                if code:
                    existing[code] = row.to_dict()
        except Exception:
            pass

    universe = get_soxx_universe(top_n=top_n, live=True)
    rows = []
    for s in universe:
        prev = existing.get(s["code"], {})
        rows.append({
            "代码": s["code"],
            "名称": s["name"],
            "层级": prev.get("层级") or "核心",
            "成本价": prev.get("成本价") or "",
            "持仓量": prev.get("持仓量") or "",
            "买入日期": prev.get("买入日期") or "",
        })

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")

    snap = pd.DataFrame(universe)
    snap.to_csv(SNAPSHOT_CSV, index=False, encoding="utf-8-sig")
    log.info("已写入股票池 %s (%d只)", path, len(df))
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    refresh_pool_csv()
    for i, s in enumerate(get_soxx_universe(), 1):
        print(f"{i:2d}. {s['code']:<6} {s['name']:<28} {s['weight']:>5.2f}%  {s['industry']}")
