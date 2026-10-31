"""ETF 前 N 大持仓（A股 ETF / 美股 ETF）。

用法：
    from core.datasets.etf_holdings import etf_holdings
    etf_holdings("cn", "510300", top=10)
    etf_holdings("us", "XLK", top=10)

返回：
    [{"code": "NVDA", "name": "NVIDIA CORP", "weight": 14.98}, ...]
    （weight 单位 %，按权重降序；拿不到返回 []）

数据源（按顺序降级）：
    美股：SPDR/SSGA 官方 holdings.xlsx → SOXX 本地快照（iShares） → yfinance funds_data
    A股 ：akshare 基金持仓（东财，取最新一个季度）

结果缓存到 data/etf_holdings.json（12 小时），可随时删除重算。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_FILE = ROOT / "data" / "etf_holdings.json"
CACHE_TTL = 12 * 3600
_LOCK = threading.Lock()

_SSGA_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
    "etfs/us/holdings-daily-us-en-{sym}.xlsx"
)
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# SPDR 系列（SSGA 官方 xlsx 可直取）
_SPDR = {
    "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC",
    "XOP", "XME", "XRT", "XHB", "XSD", "KRE", "KBE", "ITB", "XBI", "XPH", "XAR",
    "GLD", "GDX", "SLV", "DIA",
}


def _cache_read() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cache_write(d: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(CACHE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def _rows(items: list[dict], top: int) -> list[dict]:
    out = []
    for it in items:
        code = str(it.get("code") or "").strip().upper()
        if not code:
            continue
        try:
            w = float(it.get("weight") or 0)
        except (TypeError, ValueError):
            w = 0.0
        out.append({"code": code, "name": str(it.get("name") or code).strip(), "weight": round(w, 2)})
    out.sort(key=lambda x: -x["weight"])
    return out[: max(1, top)]


# ---------------- 美股 ----------------
def _us_spdr(symbol: str) -> list[dict]:
    """SPDR / SSGA 官方每日持仓 xlsx。"""
    import pandas as pd
    import requests

    url = _SSGA_URL.format(sym=symbol.lower())
    r = requests.get(url, headers=_UA, timeout=20)
    if r.status_code != 200 or not r.content:
        return []
    from io import BytesIO

    df = pd.read_excel(BytesIO(r.content), header=None)
    header = None
    for i in range(min(20, len(df))):
        vals = [str(x).strip() for x in df.iloc[i].tolist()]
        if vals and vals[0].lower() == "name" and "ticker" in [v.lower() for v in vals]:
            header = i
            cols = [v.lower() for v in vals]
            break
    if header is None:
        return []
    ci = {c: i for i, c in enumerate(cols)}
    i_name, i_tk, i_w = ci.get("name", 0), ci.get("ticker", 1), ci.get("weight", 4)
    out = []
    for i in range(header + 1, len(df)):
        row = df.iloc[i].tolist()
        if len(row) <= max(i_name, i_tk, i_w):
            continue
        tk = str(row[i_tk]).strip()
        if not tk or tk.lower() in ("nan", "none", "-", "cash"):
            continue
        out.append({
            "code": tk,
            "name": str(row[i_name]).strip(),
            "weight": row[i_w],
        })
    return out


def _us_soxx() -> list[dict]:
    """SOXX：走项目自带的 iShares 快照（离线可用）。"""
    try:
        from core.datasets.universe import get_soxx_universe

        return get_soxx_universe(top_n=20, live=False)
    except Exception:
        return []


def _us_yfinance(symbol: str) -> list[dict]:
    try:
        import yfinance as yf

        th = yf.Ticker(symbol).funds_data.top_holdings
        if th is None or len(th) == 0:
            return []

        def pct(v) -> float:
            try:
                f = float(str(v).replace("%", "").replace(",", "").strip())
            except (TypeError, ValueError):
                return 0.0
            return round(f * 100, 2) if f <= 1 else round(f, 2)

        out = []
        for idx, r in th.iterrows():
            name = r.get("holdingName") if hasattr(r, "get") else None
            out.append({
                "code": str(idx).strip(),
                "name": str(name or idx).strip(),
                "weight": pct(r.get("holdingPercent") if hasattr(r, "get") else 0),
            })
        return out
    except Exception:
        return []


# ---------------- A股 ----------------
def _cn_akshare(code: str) -> list[dict]:
    """A股 ETF/LOF 持仓：东财基金持仓，取最新季度。"""
    import akshare as ak

    sym = re.sub(r"\D", "", str(code))[:6]
    if not sym:
        return []
    year = time.localtime().tm_year
    for y in (year, year - 1):
        try:
            df = ak.fund_portfolio_hold_em(symbol=sym, date=str(y))
        except Exception:
            continue
        if df is None or len(df) == 0 or "占净值比例" not in df.columns:
            continue
        df = df.copy()
        df["占净值比例"] = __import__("pandas").to_numeric(df["占净值比例"], errors="coerce")
        if "季度" in df.columns:
            last = str(df["季度"].iloc[-1])
            q = df[df["季度"].astype(str) == last]
            if len(q):
                df = q
        out = []
        for _, r in df.iterrows():
            c = str(r.get("股票代码") or "").strip().zfill(6)
            if not c or c == "000000":
                continue
            out.append({
                "code": c,
                "name": str(r.get("股票名称") or c).strip(),
                "weight": r.get("占净值比例"),
            })
        if out:
            return out
    return []


def etf_holdings(market: str, code: str, top: int = 10) -> list[dict]:
    """取 ETF 前 top 大持仓；带 12 小时文件缓存。"""
    market = "us" if str(market).lower() == "us" else "cn"
    code = str(code or "").strip()
    if not code:
        return []
    key = f"{market}:{code.upper()}"
    with _LOCK:
        cache = _cache_read()
    node = cache.get(key)
    if isinstance(node, dict) and node.get("items") is not None:
        ttl = CACHE_TTL if node["items"] else 300  # 空结果只缓存 5 分钟，等数据源恢复
        if time.time() - float(node.get("ts") or 0) < ttl:
            return node["items"][: max(1, top)]

    items: list[dict] = []
    if market == "us":
        sym = code.upper()
        if sym in _SPDR:
            items = _us_spdr(sym)
        if not items and sym == "SOXX":
            items = _us_soxx()
        if not items:
            items = _us_yfinance(sym)
    else:
        items = _cn_akshare(code)

    items = _rows(items, top)
    with _LOCK:
        cache = _cache_read()
        cache[key] = {"ts": time.time(), "items": items}
        _cache_write(cache)
    return items


# ---------------- 反查：哪些 ETF 持有这只股票 ----------------
# 11 个 SPDR 宽基行业 ETF → GICS 行业名（用来判断美股个股“所属板块”）
US_BROAD = {
    "XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLY": "可选消费",
    "XLP": "必需消费", "XLE": "能源", "XLI": "工业", "XLB": "材料",
    "XLU": "公用事业", "XLRE": "房地产", "XLC": "通信服务",
}

_WARM = {"running": False, "done": False}


def warm_us_etfs() -> None:
    """后台把 11 个宽基行业 ETF 的持仓补齐（只补缓存里没有的）。"""
    if _WARM["running"]:
        return
    _WARM["running"] = True
    try:
        with _LOCK:
            cache = _cache_read()
        for sym in US_BROAD:
            if f"us:{sym}" in cache:
                continue
            try:
                etf_holdings("us", sym, top=20)
            except Exception:
                pass
        _WARM["done"] = True
    finally:
        _WARM["running"] = False


def etf_holders(code: str, market: str = "us", limit: int = 5) -> list[dict]:
    """反查持有该股票的 ETF（读本地缓存，按权重降序）。"""
    want = str(code or "").strip().upper()
    if not want:
        return []
    market = "us" if str(market).lower() == "us" else "cn"
    with _LOCK:
        cache = _cache_read()
    out = []
    for key, node in cache.items():
        if not key.startswith(market + ":"):
            continue
        etf = key.split(":", 1)[1]
        items = (node or {}).get("items") or []
        for it in items:
            if str(it.get("code") or "").upper() != want:
                continue
            try:
                w = float(it.get("weight") or 0)
            except (TypeError, ValueError):
                w = 0.0
            out.append({"code": etf, "weight": round(w, 2)})
            break
    out.sort(key=lambda x: -x["weight"])
    return out[: max(1, limit)]
