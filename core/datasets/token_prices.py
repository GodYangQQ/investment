"""24/7 代币化指数价格 + 中国指数缓存。

数据源：DexScreener 公开 API（Robinhood Token / xStock / Ondo / bStocks）。
代币价格是最后成交价，可能相对 ETF 净值有溢价/折价。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DB_FILE = ROOT / "data" / "sector_monitor.db"
API = "https://api.dexscreener.com/latest/dex/tokens/"
UA = {"User-Agent": "Mozilla/5.0"}

TOKENS = [
    {
        "symbol": "SPY",
        "name": "标普500",
        "group": "指数",
        "chain": "solana",
        "address": "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W",
        "venue": "xStock",
    },
    {
        "symbol": "QQQ",
        "name": "纳指100",
        "group": "指数",
        "chain": "robinhood",
        "address": "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68",
        "venue": "Robinhood",
    },
    {
        "symbol": "SOXX",
        "name": "半导体",
        "group": "指数",
        "chain": "robinhood",
        "address": "0x75742c18bc1f1c5c5f448f4c9d9c6f66dafaaa38",
        "venue": "Robinhood",
    },
    {
        "symbol": "FXI",
        "name": "中国大盘",
        "group": "指数",
        "chain": "bsc",
        "address": "0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1",
        "venue": "Ondo",
    },
    {
        "symbol": "TQQQ",
        "name": "纳指3X多",
        "group": "做多",
        "chain": "solana",
        "address": "XsjQP3iMAaQ3kQScQKthQpx9ALRbjKAjQtHg6TFomoc",
        "venue": "xStock",
    },
    {
        "symbol": "SQQQ",
        "name": "纳指3X空",
        "group": "做空",
        "chain": "bsc",
        "address": "0x17515b68378d86c38f394c666e79907da05dcba9",
        "venue": "Ondo",
    },
    {
        "symbol": "SOXL",
        "name": "半导体3X多",
        "group": "做多",
        "chain": "bsc",
        "address": "0xd97d097a89113fa59b76c572e5b2eb647e8eefaf",
        "venue": "bStocks",
    },
    {
        "symbol": "SOXS",
        "name": "半导体3X空",
        "group": "做空",
        "chain": "ethereum",
        "address": "0x01eb2ee6ec05c304666210133b762088ff9965ac",
        "venue": "Ondo",
    },
]

CN_INDICES = [
    ("SH000001", "上证指数"),
    ("SZ399001", "深证成指"),
    ("SZ399006", "创业板指"),
    ("SH000300", "沪深300"),
    ("SH000905", "中证500"),
    ("SH000688", "科创50"),
    ("SH000016", "上证50"),
]

CACHE_TTL = 4.0
_LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "data": None, "error": ""}


def _cn_indices() -> list[dict]:
    rows: list[dict] = []
    if not DB_FILE.exists():
        return rows
    try:
        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=2)
        try:
            for code, name in CN_INDICES:
                r = conn.execute(
                    "SELECT ts, c FROM bars WHERE market='cn' AND code=? AND tf='1d' "
                    "ORDER BY ts DESC LIMIT 2",
                    (code,),
                ).fetchall()
                if not r:
                    continue
                price = float(r[0][1])
                prev = float(r[1][1]) if len(r) > 1 else price
                pct = (price / prev - 1) * 100 if prev else 0.0
                rows.append(
                    {
                        "symbol": code[2:] if code.startswith(("SH", "SZ")) else code,
                        "name": name,
                        "group": "中国指数",
                        "price": price,
                        "change": pct,
                        "date": r[0][0],
                        "venue": "本地日线",
                    }
                )
        finally:
            conn.close()
    except Exception:
        pass
    return rows


def _best_pair(pairs: list[dict], chain: str, address: str) -> dict | None:
    hit = []
    for p in pairs:
        base = p.get("baseToken") or {}
        if str(p.get("chainId") or "") != chain:
            continue
        if str(base.get("address") or "").lower() != address.lower():
            continue
        hit.append(p)
    if not hit:
        return None
    return max(hit, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def fetch_prices(force: bool = False) -> dict:
    now = time.time()
    with _LOCK:
        if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < CACHE_TTL:
            return _CACHE["data"]

    def _one(t: dict) -> dict | None:
        for attempt in range(3):
            try:
                r = requests.get(API + t["address"], headers=UA, timeout=10)
                r.raise_for_status()
                p = _best_pair((r.json() or {}).get("pairs") or [], t["chain"], t["address"])
                if not p:
                    return None
                h1 = (p.get("txns") or {}).get("h1") or {}
                return {
                    "symbol": t["symbol"],
                    "name": t["name"],
                    "group": t["group"],
                    "price": float(p.get("priceUsd") or 0),
                    "change": float((p.get("priceChange") or {}).get("h24") or 0),
                    "volume": float((p.get("volume") or {}).get("h24") or 0),
                    "txns1h": int(h1.get("buys") or 0) + int(h1.get("sells") or 0),
                    "liquidity": float((p.get("liquidity") or {}).get("usd") or 0),
                    "venue": t["venue"],
                    "chain": t["chain"],
                }
            except Exception:
                if attempt < 2:
                    time.sleep(0.3)
        return None

    items: list[dict] = []
    error = ""
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = list(ex.map(_one, TOKENS))
        items = [x for x in got if x]
    except Exception as e:
        error = str(e)[:120]

    items += _cn_indices()
    data = {
        "ok": bool(items),
        "updated": time.strftime("%H:%M:%S"),
        "items": items,
        "error": error,
    }
    with _LOCK:
        if items or _CACHE["data"] is None:
            _CACHE["data"] = data
            _CACHE["ts"] = now
            _CACHE["error"] = error
        else:
            data = dict(_CACHE["data"])
            data["error"] = error
    return data
