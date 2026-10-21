#!/usr/bin/env python3
"""
A股数据层（东方财富 + 腾讯）
==========================
- 概念板块列表 / 板块成分（东财）
- 实时行情 / 日K线（腾讯）

用于 A 股短线题材炒作 pipeline。

用法:
    from cn_data import get_concept_boards, resolve_board, get_board_constituents
    from cn_data import get_quote, get_kline
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}

_BOARD_CACHE: Optional[list[dict]] = None
_CONS_CACHE: dict[str, list[dict]] = {}
_QUOTE_CACHE: dict[str, dict] = {}


_EM_HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://push2his.eastmoney.com",
]


def _em_get(url: str, retries: int = 5) -> Optional[dict]:
    """带多主机轮换 + 重试的东财请求。"""
    last = None
    path = url.split(".com", 1)[-1] if ".com" in url else url
    for i in range(retries):
        host = _EM_HOSTS[i % len(_EM_HOSTS)]
        full = host + path
        try:
            r = requests.get(full, headers=_H, timeout=15)
            r.raise_for_status()
            js = r.json()
            if js and js.get("data"):
                return js
            last = "empty data"
        except Exception as e:
            last = e
        time.sleep(0.6 * (i + 1))
    log.warning("em get failed: %s", last)
    return None


def _market_prefix(code: str) -> str:
    code = str(code)
    if code.startswith(("60", "68", "90", "51", "58")):
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def get_concept_boards(force: bool = False) -> list[dict]:
    """东财全部概念板块：[{code, name, pct, main_net}]"""
    global _BOARD_CACHE
    if _BOARD_CACHE is not None and not force:
        return _BOARD_CACHE
    boards = []
    seen = set()
    for pn in range(1, 8):
        url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14,f3,f62")
        js = _em_get(url)
        if not js or not js.get("data") or not js["data"].get("diff"):
            break
        diff = js["data"]["diff"]
        items = list(diff.values()) if isinstance(diff, dict) else diff
        if not items:
            break
        for it in items:
            code = it.get("f12")
            if code and code not in seen:
                seen.add(code)
                boards.append({
                    "code": code, "name": it.get("f14"),
                    "pct": _num(it.get("f3")), "main_net": _num(it.get("f62")),
                })
        if len(items) < 100:
            break
        time.sleep(0.3)
    _BOARD_CACHE = boards
    return boards


def _num(v, default=None):
    try:
        if v in (None, "-", ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def resolve_board(keyword: str) -> Optional[dict]:
    """按关键词模糊匹配概念板块（优先精确/最短名）。"""
    boards = get_concept_boards()
    if not boards:
        return None
    kw = str(keyword).strip()
    exact = [b for b in boards if b["name"] == kw]
    if exact:
        return exact[0]
    contains = [b for b in boards if kw in str(b["name"])]
    if contains:
        contains.sort(key=lambda b: len(str(b["name"])))
        return contains[0]
    return None


def get_board_constituents(board_code: str, limit: int = 60) -> list[dict]:
    """板块成分（按涨幅倒排）。"""
    if board_code in _CONS_CACHE:
        return _CONS_CACHE[board_code][:limit]
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz={limit}&po=1&np=1"
           f"&fltt=2&invt=2&fid=f3&fs=b:{board_code}"
           "&fields=f12,f14,f2,f3,f8,f10,f62,f20,f21")
    js = _em_get(url)
    rows = []
    if js and js.get("data") and js["data"].get("diff"):
        diff = js["data"]["diff"]
        items = list(diff.values()) if isinstance(diff, dict) else diff
        for it in items:
            rows.append({
                "code": str(it.get("f12", "")), "name": it.get("f14", ""),
                "price": _num(it.get("f2")), "pct": _num(it.get("f3"), 0.0),
                "turnover": _num(it.get("f8"), 0.0), "vol_ratio": _num(it.get("f10"), 0.0),
                "main_net": _num(it.get("f62"), 0.0),
                "float_mv": _num(it.get("f21"), 0.0),
            })
    _CONS_CACHE[board_code] = rows
    return rows[:limit]


def get_quote(code: str) -> dict:
    """腾讯实时行情。"""
    code = str(code)
    if code in _QUOTE_CACHE:
        return _QUOTE_CACHE[code]
    sym = _market_prefix(code) + code
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={sym}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.encoding = "gbk"
        txt = r.text
        parts = txt.split('"')
        f = parts[1].split("~") if len(parts) > 1 else []
        if len(f) < 46:
            return {}
        q = {
            "code": f[2], "name": f[1],
            "price": _num(f[3], 0.0), "prev_close": _num(f[4], 0.0), "open": _num(f[5], 0.0),
            "volume": _num(f[6], 0.0), "pct_change": _num(f[32], 0.0),
            "turnover": _num(f[38], 0.0), "pe": _num(f[39], 0.0),
            "total_mv": (_num(f[45], 0.0) or 0.0) * 1e8,
            "circ_mv": (_num(f[44], 0.0) or 0.0) * 1e8,
        }
        _QUOTE_CACHE[code] = q
        return q
    except Exception as e:
        log.warning("quote failed %s: %s", code, e)
        return {}


def get_kline(code: str, days: int = 60) -> pd.DataFrame:
    """腾讯前复权日K。列: date, open, close, high, low, volume"""
    sym = _market_prefix(code) + str(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        data = r.json().get("data", {}).get(sym, {})
        klines = data.get("qfqday") or data.get("day") or []
        rows = []
        for k in klines:
            if len(k) >= 6:
                rows.append({"date": k[0], "open": float(k[1]), "close": float(k[2]),
                             "high": float(k[3]), "low": float(k[4]), "volume": float(k[5])})
        df = pd.DataFrame(rows)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception as e:
        log.warning("kline failed %s: %s", code, e)
        return pd.DataFrame()


def get_quotes_batch(codes: list[str]) -> dict[str, dict]:
    """腾讯批量行情（每批≤60）。返回 {code: quote}"""
    out = {}
    codes = [str(c) for c in codes if c]
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        syms = ",".join(_market_prefix(c) + c for c in batch)
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={syms}",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if '"' not in line:
                    continue
                f = line.split('"')[1].split("~")
                if len(f) < 46:
                    continue
                code = f[2]
                q = {
                    "code": code, "name": f[1],
                    "price": _num(f[3], 0.0), "prev_close": _num(f[4], 0.0),
                    "pct_change": _num(f[32], 0.0), "turnover": _num(f[38], 0.0),
                    "pe": _num(f[39], 0.0),
                    "total_mv": (_num(f[45], 0.0) or 0.0) * 1e8,
                }
                out[code] = q
                _QUOTE_CACHE[code] = q
        except Exception as e:
            log.warning("batch quote failed: %s", e)
        time.sleep(0.1)
    return out


def get_ths_concepts() -> list[dict]:
    """同花顺概念板块列表（akshare，可选）。"""
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_ths()
        return [{"code": str(r["code"]), "name": str(r["name"])} for _, r in df.iterrows()]
    except Exception as e:
        log.warning("THS concepts failed: %s", e)
        return []


def get_concept_cons_em(name: str) -> list[dict]:
    """东财概念成分股（akshare，可选增强）。返回 [{code,name}]"""
    try:
        import akshare as ak
        df = ak.stock_board_concept_cons_em(symbol=name)
        col_code = "代码" if "代码" in df.columns else df.columns[0]
        col_name = "名称" if "名称" in df.columns else df.columns[1]
        return [{"code": str(r[col_code]), "name": str(r[col_name])} for _, r in df.iterrows()]
    except Exception as e:
        log.warning("EM concept cons failed for %s: %s", name, e)
        return []


def is_excluded(code: str, name: str) -> bool:
    """排除 ST / 退市 / 停牌 / B股。"""
    name = str(name)
    if any(k in name for k in ["ST", "*ST", "退"]):
        return True
    code = str(code)
    if code.startswith(("2", "9")):  # B股
        return True
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    boards = get_concept_boards()
    print("概念板块数:", len(boards))
    b = resolve_board("生物疫苗")
    print("resolve 生物疫苗 ->", b)
    if b:
        for s in get_board_constituents(b["code"], 5):
            print("  ", s["code"], s["name"], s["pct"], "量比", s["vol_ratio"])
    print(get_quote("600812"))
