"""板块之间的关系：共振（同向） / 对手（反向）。

指标口径：
    1. 取 `sector_monitor.db` 里的日线收盘价，算日收益。
    2. 市场因子 = 上证指数（美股用 SPY）。
    3. 「共振」用原始日收益相关：一起涨一起跌。
    4. 「对手」用超额收益相关（板块收益 − 指数收益）：
       原始相关几乎全为正（共同 beta），只有剔掉大盘后才会出现真正的反向轮动。
       这里减指数收益而不是减横截面均值，避免“残差和为 0”造出来的假负相关。
    5. 同一个板块的两套代码（东财 BK… / 同花顺 T88…，相关≈1）会自动去重。

用法：
    from core.scoring.sector_relation import sector_relations
    d = sector_relations("cn")            # {code: {"peers": [...], "rivals": [...]}}
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_FILE = ROOT / "data" / "sector_monitor.db"

WINDOW = 250          # 最近多少个交易日
MIN_PERIODS = 120
PEER_MIN = 0.45       # 共振门槛
RIVAL_MAX = -0.12     # 对手门槛（超额收益相关）
DUP_CORR = 0.995      # 视为“同一板块的两套代码”
CACHE_TTL = 1800      # 30 分钟

_CACHE: dict[tuple, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_NAMES: dict[str, tuple[float, dict[str, str]]] = {}

_MARKET_FACTOR = {"cn": "SH000001", "us": "SPY"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot_names(conn: sqlite3.Connection, market: str, kind: str) -> dict[str, str]:
    """板块代码 → 名称（来自列表快照；拿不到就退回代码本身）。"""
    try:
        row = conn.execute(
            "SELECT payload FROM snapshots WHERE key=?", (f"list:{market}:{kind}",)
        ).fetchone()
        if not row:
            return {}
        import json

        payload = json.loads(row["payload"])
        return {str(r.get("code")): str(r.get("name") or r.get("code")) for r in payload.get("rows") or []}
    except Exception:
        return {}


def _load_returns(market: str, codes: list[str]):
    """读日线 → 日收益 DataFrame（列 = 板块代码）。"""
    import pandas as pd

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT code, ts, c FROM bars WHERE market=? AND tf='1d'", (market,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    df = pd.DataFrame([(r["code"], r["ts"], r["c"]) for r in rows], columns=["code", "ts", "c"])
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts", "c"])
    px = df.pivot_table(index="ts", columns="code", values="c", aggfunc="last").sort_index()
    px = px[[c for c in codes if c in px.columns]]
    return px.pct_change()


def _ths_industry_names() -> dict[str, str]:
    """同花顺行业代码（881xxx）→ 名称；用于补齐快照里没有的板块（缓存 6h）。"""
    now = time.time()
    hit = _NAMES.get("industry")
    if hit and now - hit[0] < 6 * 3600:
        return hit[1]
    out: dict[str, str] = {}
    try:
        import akshare as ak

        df = ak.stock_board_industry_name_ths()
        for _, r in df.iterrows():
            code = str(r.get("code") or "").strip()
            if code:
                out["T" + code] = str(r.get("name") or code)
    except Exception:
        out = {}
    if out:
        _NAMES["industry"] = (now, out)
    return out


def _universe(conn: sqlite3.Connection, market: str, kind: str) -> dict[str, str]:
    """有足够历史的板块：优先用快照列表，否则用库里所有非个股代码。"""
    names = _snapshot_names(conn, market, kind)
    have = {
        str(r[0])
        for r in conn.execute(
            "SELECT code FROM bars WHERE market=? AND tf='1d' GROUP BY code HAVING COUNT(*)>?",
            (market, MIN_PERIODS),
        )
    }
    if names:
        out = {c: n for c, n in names.items() if c in have}
        if market == "cn" and kind != "concept":
            # 快照可能只有半页列表：用同花顺全量行业名补齐库里有历史的板块
            extra = _ths_industry_names()
            for c in have:
                if c not in out and c in extra:
                    out[c] = extra[c]
        if len(out) >= 5:
            return out
    # 兜底：排除个股（6 位纯数字）与指数
    out = {}
    for c in have:
        cc = str(c)
        if market == "cn" and (cc.isdigit() and len(cc) == 6):
            continue
        if market == "cn" and cc in _MARKET_FACTOR.values():
            continue
        out[cc] = cc
    return out


def _relations(market: str, kind: str, window: int, top: int) -> dict:
    import numpy as np
    import pandas as pd

    conn = _connect()
    try:
        names = _universe(conn, market, kind)
    finally:
        conn.close()
    if len(names) < 5:
        return {"market": market, "window": window, "items": {}, "note": "板块历史数据不足"}

    factor = _MARKET_FACTOR.get(market, "SPY")
    codes = list(dict.fromkeys(list(names) + [factor]))  # 因子可能已在板块里（如美股的 SPY）
    ret = _load_returns(market, codes)
    if ret is None or ret.empty:
        return {"market": market, "window": window, "items": {}, "note": "无本地日线"}
    ret = ret.tail(window + 1)
    if factor not in ret.columns:
        return {"market": market, "window": window, "items": {}, "note": f"缺少市场因子 {factor}"}

    mkt = ret[factor]
    excess = ret.sub(mkt, axis=0)
    corr_raw = ret.corr(min_periods=MIN_PERIODS)
    corr_ex = excess.corr(min_periods=MIN_PERIODS)

    cols = list(corr_raw.columns)
    items: dict[str, dict] = {}
    for a in cols:
        peers, rivals = [], []
        for b in cols:
            if b == a:
                continue
            raw = corr_raw.at[a, b]
            ex = corr_ex.at[a, b]
            if pd.isna(raw) or pd.isna(ex):
                continue
            # 同一板块的两套代码：跳过（否则永远是最强共振 + 最强对手）
            if raw >= DUP_CORR:
                continue
            peers.append((float(raw), float(ex), b))
            rivals.append((float(ex), float(raw), b))
        peers.sort(reverse=True)
        rivals.sort()
        items[a] = {
            "name": names.get(a, a),
            "peers": [
                {"code": b, "name": names.get(b, b), "corr": round(p, 3), "ex_corr": round(e, 3)}
                for p, e, b in peers[:top] if p >= PEER_MIN
            ],
            "rivals": [
                {"code": b, "name": names.get(b, b), "corr": round(e, 3), "raw_corr": round(r, 3)}
                for e, r, b in rivals[:top] if e <= RIVAL_MAX
            ],
        }
    return {
        "market": market,
        "kind": kind,
        "window": int(window),
        "asof": str(ret.index[-1].date()) if len(ret.index) else "",
        "count": len(items),
        "items": items,
    }


def sector_relations(market: str = "cn", kind: str = "industry", window: int = WINDOW, top: int = 3) -> dict:
    """板块的共振 / 对手榜（带 30 分钟内存缓存）。"""
    market = "us" if str(market).lower() == "us" else "cn"
    window = max(60, min(int(window or WINDOW), 750))
    top = max(1, min(int(top or 3), 5))
    key = (market, kind, window, top)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    data = _relations(market, kind, window, top)
    with _LOCK:
        _CACHE[key] = (now, data)
    return data
