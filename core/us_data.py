#!/usr/bin/env python3
"""
美股数据层
==========
主源: Nasdaq 公开 JSON（行情/K线/财报）
备用: yfinance（被限流时自动跳过）

用法:
    from us_data import fetch_realtime_quote, fetch_daily_kline, fetch_financial_data
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

try:
    import yfinance as yf
    _HAS_YFINANCE = True
except ImportError:
    yf = None
    _HAS_YFINANCE = False

_HAS_AKSHARE = _HAS_YFINANCE

INDEX_TICKERS = {"SOXX": "SOXX", "QQQ": "QQQ", "SPY": "SPY", "SMH": "SMH", "^SOX": "SOXX"}
TICKER_ALIASES = {"SKHY": ["SKHY", "000660.KS"]}
ETF_TICKERS = {
    "SOXX", "SOXL", "SOXS", "SMH", "QQQ", "TQQQ", "SQQQ", "SPY", "UPRO", "SPXU",
    "XLK", "VGT", "XSD", "PSI", "IGW", "FTXL",
    # 利率/债券 ETF（用于验证宏观）
    "TLT", "UBT", "TBT", "TMV", "IEF", "SHY", "TIP", "LQD", "HYG", "BND", "AGG",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

_HIST_CACHE: dict[str, pd.DataFrame] = {}
_QUOTE_CACHE: dict[str, dict] = {}
_FIN_CACHE: dict[str, Optional[dict]] = {}
_QUARTER_CACHE: dict[str, list] = {}


def _normalize_ticker(code: str) -> str:
    code = str(code).strip().upper()
    if code.startswith("$"):
        code = code[1:]
    if code == "^SOX":
        return "SOXX"
    return code


def _sf(v, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_num(text) -> Optional[float]:
    if text is None:
        return None
    s = str(text).strip()
    if s in ("", "--", "N/A", "n/a", "NA"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("$", "").replace(",", "").replace("%", "").replace("(", "").replace(")", "")
    try:
        val = float(s)
        return -val if neg else val
    except ValueError:
        return None


def _asset_class(ticker: str) -> str:
    return "etf" if ticker in ETF_TICKERS else "stocks"


def _nasdaq_get(url: str, retries: int = 2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"nasdaq get failed {url}: {last}")


def _nasdaq_quote_payload(ticker: str) -> dict:
    ticker = _normalize_ticker(ticker)
    if ticker in _QUOTE_CACHE:
        return _QUOTE_CACHE[ticker]
    ac = _asset_class(ticker)
    info = _nasdaq_get(f"https://api.nasdaq.com/api/quote/{ticker}/info?assetclass={ac}")
    summary = {}
    try:
        summary = _nasdaq_get(f"https://api.nasdaq.com/api/quote/{ticker}/summary?assetclass={ac}")
    except Exception:
        summary = {}
    payload = {"info": (info or {}).get("data") or {}, "summary": (summary or {}).get("data") or {}}
    _QUOTE_CACHE[ticker] = payload
    return payload


def fetch_realtime_quote(code: str) -> dict:
    ticker = _normalize_ticker(code)
    data = _nasdaq_quote_payload(ticker)
    info = data.get("info") or {}
    primary = info.get("primaryData") or {}
    summary_map = ((data.get("summary") or {}).get("summaryData") or {})

    def s(key):
        node = summary_map.get(key) or {}
        return _parse_num(node.get("value"))

    price = _parse_num(primary.get("lastSalePrice")) or 0.0
    prev = s("PreviousClose") or price
    pct_raw = primary.get("percentageChange") or ""
    pct = _parse_num(pct_raw) if pct_raw else ((price / prev - 1) * 100 if prev else 0.0)
    mcap = s("MarketCap") or 0.0
    volume = _parse_num(str(primary.get("volume") or summary_map.get("ShareVolume", {}).get("value"))) or 0.0
    avg_vol = s("AverageVolume") or 0.0
    hi_lo = (summary_map.get("TodayHighLow") or {}).get("value") or ""
    high = low = None
    if "/" in str(hi_lo):
        parts = str(hi_lo).split("/")
        high = _parse_num(parts[0])
        low = _parse_num(parts[1]) if len(parts) > 1 else None

    shares = mcap / price if price else 0.0
    turnover = (volume / shares * 100) if shares else 0.0
    target = s("OneYrTarget") or 0.0
    name = info.get("companyName") or ticker
    # PE: TTM NI from cache later; leave 0 here, filled if financials available
    pe = 0.0
    pb = 0.0
    try:
        fin = fetch_financial_data(ticker)
        if fin:
            ni = fin.get("归母净利润")
            equity = None
            # approximate TTM NI as 4*quarter if only one quarter stored as quarterly
            if ni and mcap:
                ttm_ni = ni * 4 if abs(ni) < mcap else ni
                if ttm_ni:
                    pe = mcap / ttm_ni
            bvps = fin.get("每股净资产")
            if bvps:
                pb = price / bvps
            if fin.get("净资产收益率(ROE)") is None:
                pass
    except Exception:
        fin = None

    amplitude = 0.0
    if high and low and prev:
        amplitude = (high - low) / prev * 100

    return {
        "code": ticker,
        "name": name,
        "price": round(price, 4),
        "prev_close": round(prev, 4),
        "open": round(price, 4),
        "volume": volume or 0.0,
        "buy_volume": 0.0,
        "sell_volume": 0.0,
        "pct_change": round(pct or 0.0, 2),
        "high": round(high or price, 4),
        "low": round(low or price, 4),
        "pe_ttm": round(pe, 2),
        "turnover": round(turnover, 4),
        "total_mv_yi": round(mcap / 1e8, 2),
        "market_cap": mcap,
        "pb": round(pb, 2),
        "振幅": round(amplitude, 2),
        "exchange": info.get("exchange") or (summary_map.get("Exchange") or {}).get("value") or "",
        "industry": (summary_map.get("Industry") or {}).get("value") or "",
        "sector": (summary_map.get("Sector") or {}).get("value") or "",
        "target_mean": target,
        "recommendation": "",
        "analysts": 0,
        "currency": "USD",
        "avg_volume": avg_vol,
    }


def fetch_daily_kline(code: str, days: int = 120) -> pd.DataFrame:
    ticker = _normalize_ticker(code)
    cache_key = f"{ticker}:{days}"
    if cache_key in _HIST_CACHE:
        return _HIST_CACHE[cache_key].copy()

    end = datetime.utcnow().date()
    start = end - timedelta(days=max(int(days * 2.2) + 40, 120))
    ac = _asset_class(ticker)
    url = (
        f"https://api.nasdaq.com/api/quote/{ticker}/historical"
        f"?assetclass={ac}&fromdate={start}&todate={end}&limit={max(days + 50, 300)}"
    )
    js = _nasdaq_get(url)
    rows = (((js or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    if not rows:
        raise ValueError(f"No K-line data for {code}")

    recs = []
    for row in rows:
        dt = pd.to_datetime(row.get("date"), errors="coerce")
        if pd.isna(dt):
            continue
        recs.append({
            "date": dt.normalize(),
            "open": _parse_num(row.get("open")),
            "high": _parse_num(row.get("high")),
            "low": _parse_num(row.get("low")),
            "close": _parse_num(row.get("close")),
            "volume": _parse_num(row.get("volume")) or 0.0,
        })
    df = pd.DataFrame(recs).dropna(subset=["close"])
    df = df.sort_values("date").reset_index(drop=True)
    if days and len(df) > days:
        df = df.tail(days).reset_index(drop=True)
    if df.empty:
        raise ValueError(f"No K-line data for {code}")
    _HIST_CACHE[cache_key] = df
    return df.copy()


def fetch_batch_quotes(codes: list[str]) -> dict[str, dict]:
    out = {}
    for c in codes:
        try:
            out[_normalize_ticker(c)] = fetch_realtime_quote(c)
        except Exception as e:
            log.warning("quote failed %s: %s", c, e)
    return out


def _table_to_maps(table: dict) -> tuple[list[str], list[dict]]:
    headers = table.get("headers") or {}
    dates = []
    for k in ["value2", "value3", "value4", "value5"]:
        dates.append(str(headers.get(k) or ""))
    maps = [dict() for _ in dates]
    for row in table.get("rows") or []:
        name = str(row.get("value1") or "").strip()
        if not name:
            continue
        for i, key in enumerate(["value2", "value3", "value4", "value5"]):
            maps[i][name] = _parse_num(row.get(key))
    return dates, maps


def _row_get(m: dict, names: list[str]) -> Optional[float]:
    for n in names:
        if n in m and m[n] is not None:
            return m[n]
    lower = {k.lower(): v for k, v in m.items()}
    for n in names:
        if n.lower() in lower and lower[n.lower()] is not None:
            return lower[n.lower()]
    return None


def fetch_financial_data(code: str) -> Optional[dict]:
    ticker = _normalize_ticker(code)
    if ticker in _FIN_CACHE:
        return _FIN_CACHE[ticker]
    try:
        js = _nasdaq_get(f"https://api.nasdaq.com/api/company/{ticker}/financials?frequency=2")
    except Exception as e:
        log.warning("财务数据获取失败 %s: %s", code, e)
        return None
    data = (js or {}).get("data") or {}
    if not data:
        _FIN_CACHE[ticker] = None
        return None

    inc_dates, inc_maps = _table_to_maps(data.get("incomeStatementTable") or {})
    bs_dates, bs_maps = _table_to_maps(data.get("balanceSheetTable") or {})
    cf_dates, cf_maps = _table_to_maps(data.get("cashFlowTable") or {})
    ratio_dates, ratio_maps = _table_to_maps(data.get("financialRatiosTable") or {})
    if not inc_maps:
        return None

    inc = inc_maps[0]
    bs = bs_maps[0] if bs_maps else {}
    cf = cf_maps[0] if cf_maps else {}
    ratios = ratio_maps[0] if ratio_maps else {}

    # Nasdaq 财报数字为千美元
    scale = 1000.0
    rev = _row_get(inc, ["Total Revenue"])
    gp = _row_get(inc, ["Gross Profit"])
    ni = _row_get(inc, ["Net Income", "Net Income Applicable to Common Shareholders", "Net Income-Cont. Operations"])
    cogs = _row_get(inc, ["Cost of Revenue"])
    ocf = _row_get(cf, ["Net Cash Flow-Operating"])
    equity = _row_get(bs, ["Total Equity"])
    assets = _row_get(bs, ["Total Assets"])
    liab = _row_get(bs, ["Total Liabilities"])
    inventory = _row_get(bs, ["Inventory"])

    rev_yoy = ni_yoy = None
    if len(inc_maps) >= 4:
        prev_rev = _row_get(inc_maps[3], ["Total Revenue"])
        prev_ni = _row_get(inc_maps[3], ["Net Income", "Net Income Applicable to Common Shareholders"])
        if rev and prev_rev:
            rev_yoy = (rev / prev_rev - 1) * 100
        if ni is not None and prev_ni:
            ni_yoy = (ni / prev_ni - 1) * 100

    gm = _parse_num(str(ratios.get("Gross Margin"))) if ratios.get("Gross Margin") is not None else None
    if gm is None and gp is not None and rev:
        gm = gp / rev * 100
    net_margin = _parse_num(str(ratios.get("Profit Margin"))) if ratios.get("Profit Margin") is not None else None
    if net_margin is None and ni is not None and rev:
        net_margin = ni / rev * 100
    roe = _parse_num(str(ratios.get("After Tax ROE"))) if ratios.get("After Tax ROE") is not None else None
    if roe is None and ni is not None and equity:
        roe = ni * 4 / equity * 100
    debt_ratio = (liab / assets * 100) if liab is not None and assets else None

    bvps = None
    try:
        q = _nasdaq_quote_payload(ticker)
        price = _parse_num(((q.get("info") or {}).get("primaryData") or {}).get("lastSalePrice")) or 0
        mcap = _parse_num((((q.get("summary") or {}).get("summaryData") or {}).get("MarketCap") or {}).get("value")) or 0
        shares = mcap / price if price else 0
        if equity and shares:
            bvps = (equity * scale) / shares
        eps = (ni * scale) / shares if ni is not None and shares else None
    except Exception:
        shares = 0
        eps = None

    result = {
        "归母净利润": (ni * scale) if ni is not None else None,
        "营业总收入": (rev * scale) if rev is not None else None,
        "营业成本": (cogs * scale) if cogs is not None else None,
        "扣非净利润": (ni * scale) if ni is not None else None,
        "经营现金流量净额": (ocf * scale) if ocf is not None else None,
        "基本每股收益": eps,
        "每股净资产": bvps,
        "净资产收益率(ROE)": roe,
        "毛利率": gm,
        "销售净利率": net_margin,
        "资产负债率": debt_ratio,
        "商誉": None,
        "营业总收入同比": rev_yoy,
        "净利润同比增长率": ni_yoy,
        "存货": (inventory * scale) if inventory is not None else None,
        "source": "nasdaq",
        "report_date": inc_dates[0] if inc_dates else "",
    }
    _FIN_CACHE[ticker] = result
    return result


def fetch_quarterly_table(code: str, n: int = 8) -> list[dict]:
    ticker = _normalize_ticker(code)
    if ticker in _QUARTER_CACHE:
        return _QUARTER_CACHE[ticker]
    try:
        js = _nasdaq_get(f"https://api.nasdaq.com/api/company/{ticker}/financials?frequency=2")
    except Exception:
        return []
    data = (js or {}).get("data") or {}
    inc_dates, inc_maps = _table_to_maps(data.get("incomeStatementTable") or {})
    bs_dates, bs_maps = _table_to_maps(data.get("balanceSheetTable") or {})
    cf_dates, cf_maps = _table_to_maps(data.get("cashFlowTable") or {})
    ratio_dates, ratio_maps = _table_to_maps(data.get("financialRatiosTable") or {})
    scale = 1000.0
    rows = []
    count = min(n, len(inc_maps))
    for i in range(count):
        inc = inc_maps[i]
        bs = bs_maps[i] if i < len(bs_maps) else {}
        cf = cf_maps[i] if i < len(cf_maps) else {}
        ratios = ratio_maps[i] if i < len(ratio_maps) else {}
        rev = _row_get(inc, ["Total Revenue"])
        ni = _row_get(inc, ["Net Income", "Net Income Applicable to Common Shareholders"])
        gp = _row_get(inc, ["Gross Profit"])
        cogs = _row_get(inc, ["Cost of Revenue"])
        ocf = _row_get(cf, ["Net Cash Flow-Operating"])
        equity = _row_get(bs, ["Total Equity"])
        inventory = _row_get(bs, ["Inventory"])
        gm = _parse_num(str(ratios.get("Gross Margin"))) if ratios.get("Gross Margin") is not None else None
        if gm is None and gp is not None and rev:
            gm = gp / rev * 100
        roe = _parse_num(str(ratios.get("After Tax ROE"))) if ratios.get("After Tax ROE") is not None else None
        if roe is None and ni is not None and equity:
            roe = ni * 4 / equity * 100
        inv_turn = None
        if cogs and inventory and inventory > 0:
            inv_turn = (cogs * 4) / inventory
        rev_yoy = ni_yoy = None
        if i + 4 <= len(inc_maps):  # same quarter last year not available (only 4 cols)
            pass
        if i + 3 < len(inc_maps) and i == 0:
            # 4 columns only: YoY vs value5 which is 3 quarters back, not perfect
            pass
        if len(inc_maps) >= 4 and i == 0:
            prev_rev = _row_get(inc_maps[3], ["Total Revenue"])
            prev_ni = _row_get(inc_maps[3], ["Net Income", "Net Income Applicable to Common Shareholders"])
            if rev and prev_rev:
                rev_yoy = (rev / prev_rev - 1) * 100
            if ni is not None and prev_ni:
                ni_yoy = (ni / prev_ni - 1) * 100
        rows.append({
            "report_date": inc_dates[i] if i < len(inc_dates) else "",
            "report_name": inc_dates[i] if i < len(inc_dates) else "",
            "revenue": (rev * scale) if rev is not None else None,
            "net_profit": (ni * scale) if ni is not None else None,
            "gross_margin": gm,
            "roe": roe,
            "inventory_turnover": inv_turn,
            "inventory_days": (365 / inv_turn) if inv_turn else None,
            "cf_ratio": (ocf / ni) if ocf is not None and ni else None,
            "eps": None,
            "revenue_yoy": rev_yoy,
            "profit_yoy": ni_yoy,
        })
    _QUARTER_CACHE[ticker] = rows
    return rows


def fetch_index_quote(symbol: str = "SOXX") -> dict:
    ticker = INDEX_TICKERS.get(symbol.upper(), symbol)
    df = fetch_daily_kline(ticker, days=260)
    quote = fetch_realtime_quote(ticker)
    closes = df["close"].tolist()
    last_close = closes[-1] if closes else quote["price"]
    ma60 = float(np.mean(closes[-60:])) if len(closes) >= 60 else None
    ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else None
    if ma60 and last_close > ma60:
        position_cap, status, max_stocks = 100, f"[强势] {ticker}>MA60", "5-8只"
    elif ma200 and last_close > ma200:
        position_cap, status, max_stocks = 50, f"[震荡] MA60>{ticker}>MA200", "2-3只"
    elif ma200:
        position_cap, status, max_stocks = 30, f"[弱势] {ticker}<MA200", "1-2只（仅最高分）"
    else:
        position_cap, status, max_stocks = 80, "[!] 数据不足", "4-6只"
    return {
        "symbol": ticker,
        "idx_price": quote["price"],
        "idx_pct": quote["pct_change"],
        "ma60": round(ma60, 2) if ma60 else None,
        "ma200": round(ma200, 2) if ma200 else None,
        "last_close": round(last_close, 2),
        "status": status,
        "position_cap": position_cap,
        "max_stocks": max_stocks,
        "name": quote.get("name", ticker),
    }


def fmt_market_cap(mcap_or_yi) -> str:
    v = float(mcap_or_yi or 0)
    usd = v * 1e8 if v < 1e7 else v
    if usd >= 1e12:
        return f"{usd / 1e12:.2f}万亿美元"
    if usd >= 1e8:
        return f"{usd / 1e8:.1f}亿美元"
    if usd >= 1e6:
        return f"{usd / 1e6:.1f}百万美元"
    return f"{usd:.0f}美元"


if __name__ == "__main__":
    q = fetch_realtime_quote("NVDA")
    print(q["name"], q["price"], q["pct_change"], q["pe_ttm"], fmt_market_cap(q["market_cap"]))
    print(fetch_daily_kline("NVDA", 5).tail())
