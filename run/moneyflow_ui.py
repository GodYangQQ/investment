#!/usr/bin/env python3
"""
板块资金流向 UI（方框热力图 · 分资金来源）
========================================
零依赖 Web 服务（Python 标准库），盘中数据来自东方财富。

资金来源映射（按单笔成交金额分档估算）：
  机构/主力 = 超大单 + 大单
  机构(纯)  = 超大单
  游资(近似)= 大单            ← 启发式，真实游资需看龙虎榜席位
  散户      = 中单 + 小单

交互：方框热力图（越流入越红/越流出越绿）；点击加入展示区；
悬浮看明细 + 该板块内主力流入/流出 Top3 个股。

用法:
    python run/moneyflow_ui.py                 # http://127.0.0.1:8770
    python run/moneyflow_ui.py --port 8888
    python run/moneyflow_ui.py --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

RUN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(RUN_DIR)
WATCH_FILE = os.path.join(ROOT_DIR, "data", "moneyflow_watch.json")
sys.path.insert(0, os.path.join(RUN_DIR, "..", "core"))

import requests  # noqa: E402

from cn_data import _em_get  # noqa: E402

_SEARCH_HOSTS = ["https://searchapi.eastmoney.com", "https://searchadapter.eastmoney.com"]
_SEARCH_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com/"}
_A_MKT = {"沪A", "深A", "京A", "科创板", "创业板"}


# ============ 本地磁盘缓存（跨重启保留） ============
_DC_PATH = os.path.join(ROOT_DIR, "data", "ui_cache.json")
_DC = None
_DC_LOCK = threading.Lock()


def _dc_load():
    global _DC
    if _DC is None:
        try:
            with open(_DC_PATH, encoding="utf-8") as f:
                _DC = json.load(f)
        except Exception:
            _DC = {}
    return _DC


def dc_get(key: str):
    with _DC_LOCK:
        node = _dc_load().get(key)
    if node and time.time() - node.get("ts", 0) < node.get("ttl", 3600):
        return node.get("v")
    return None


def dc_set(key: str, v, ttl: int = 3600):
    with _DC_LOCK:
        d = _dc_load()
        d[key] = {"ts": time.time(), "ttl": ttl, "v": v}
        try:
            os.makedirs(os.path.dirname(_DC_PATH), exist_ok=True)
            tmp = _DC_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, _DC_PATH)
        except Exception:
            pass


def search_stocks(q: str, n: int = 10) -> list[dict]:
    """股票搜索补全（东财 suggest）。"""
    q = str(q).strip()
    if not q:
        return []
    cv = dc_get("q:" + q)
    if cv is not None:
        return cv[:n]
    params = {"input": q, "type": "14", "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": n}
    for host in _SEARCH_HOSTS:
        try:
            r = requests.get(host + "/api/suggest/get", params=params, headers=_SEARCH_H, timeout=10)
            data = ((r.json() or {}).get("QuotationCodeTable", {}) or {}).get("Data") or []
            out = []
            for it in data:
                code = str(it.get("Code", ""))
                name = it.get("Name", "")
                if len(code) != 6 or not code.isdigit():
                    continue
                stn = it.get("SecurityTypeName", "") or ""
                # 仅保留 A 股
                if stn and not any(k in stn for k in ("沪A", "深A", "京A", "科创", "创业")):
                    continue
                out.append({"code": code, "name": name, "type": stn})
            if out:
                dc_set("q:" + q, out, ttl=30 * 24 * 3600)
                return out[:n]
        except Exception:
            continue
    return []

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

SECTOR_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3"}
FIELDS = "f12,f14,f3,f62,f184,f66,f72,f78,f84,f6,f10,f8"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _items(js):
    if js and js.get("data") and js["data"].get("diff") is not None:
        diff = js["data"]["diff"]
        return list(diff.values()) if isinstance(diff, dict) else diff
    return []


def fetch_market_total() -> dict:
    """全市场主力净流入 = 上证指数 + 深证成指 + 北证50 的聚合。"""
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           "&secids=1.000001,0.399001,0.899050&fields=f12,f14,f62,f66,f72,f78,f84")
    inst = hot = mid = small = 0.0
    for it in _items(_em_get(url)):
        inst += _num(it.get("f66")); hot += _num(it.get("f72"))
        mid += _num(it.get("f78")); small += _num(it.get("f84"))
    return {"main": inst + hot, "inst": inst, "hot": hot,
            "retail": mid + small, "mid": mid, "small": small}


def fetch_market_curve() -> list[dict]:
    """沪深两市主力净流入的今日分钟序列（累计）。"""
    def series(secid: str):
        url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?lmt=0&klt=1"
               f"&secid={secid}&fields1=f1,f2,f3,f7&fields2=f51,f52")
        out = []
        for k in ((_em_get(url) or {}).get("data", {}) or {}).get("klines", []) or []:
            p = str(k).split(",")
            try:
                out.append((p[0], float(p[1])))
            except (IndexError, ValueError):
                pass
        return out
    sh, sz = series("1.000001"), series("0.399001")
    n = min(len(sh), len(sz))
    return [{"t": sh[i][0][11:16], "v": sh[i][1] + sz[i][1]} for i in range(n)]


# ============ 主力行为评估（按需计算 + 缓存，只算"已添加"的） ============
_BEH: dict[str, dict] = {}
_BEH_LOCK = threading.Lock()
_BEH_TTL = 600  # 秒


def classify_behavior(pos_high, trend5, trend20, flow5, improve):
    """两级：大方向(抄底/诱低/诱多/逃顶/中性) + 具体行为。"""
    deep = pos_high <= -30
    high = pos_high >= -8
    # ① 抄底（低位吸筹，看多）
    if deep and (improve > 0.5 or flow5 > 0):
        sub = "挖坑吸筹" if trend20 > 8 else ("下跌吸筹(左侧)" if trend20 < -5 else "横盘吸筹")
        return "抄底", sub, "bull", 80
    # ② 逃顶（顶部派发，看空）
    if high and flow5 < -1:
        return "逃顶", ("高位放量出货" if trend20 > 0 else "阴跌出货"), "bear", 82
    # ③ 诱多（拉高派发，看空）
    if (high or trend20 > 8) and flow5 < 0:
        return "诱多", ("冲高回落诱多" if trend5 < 0 else "放量滞涨诱多"), "bear", 74
    # ④ 诱低（制造下跌洗盘，看多）
    if (not high) and trend20 < -3 and (flow5 > -1 or improve > 2):
        return "诱低", ("破位诱空" if trend5 < trend20 else "急跌吸筹"), "bull", 66
    # ⑤ 中性态
    if high and flow5 > 0 and trend20 > 0:
        return "中性", "主力拉升", "bull", 60
    if trend20 > 0 and flow5 <= 0:
        return "中性", "主力洗盘", "neutral", 48
    if deep and flow5 < -3:
        return "中性", "主力仍离场", "bear", 45
    return "中性", "无明显信号", "neutral", 30


def board_secid(code: str) -> str:
    return "90." + str(code)


def stock_secid(code: str) -> str:
    return _secid(str(code))


def _mkt(code: str) -> str:
    c = str(code)
    return "sh" if c.startswith(("6", "9")) else ("bj" if c.startswith(("4", "8")) else "sz")


def _board_series_ak(name: str):
    """板块兜底：日K取收盘序列 + 板块资金流取主力净占比。"""
    import akshare as ak
    end = datetime.now().strftime("%Y%m%d")
    idx, ratio = [], []
    try:
        k = ak.stock_board_industry_hist_em(symbol=name, start_date="20241201", end_date=end,
                                            period="日k", adjust="")
        if k is not None and not k.empty:
            idx = [float(v) for v in k["收盘"]]
    except Exception:
        pass
    try:
        f = ak.stock_sector_fund_flow_hist(symbol=name)
        if f is not None and not f.empty:
            ratio = [float(v) for v in f["主力净流入-净占比"]]
    except Exception:
        pass
    n = min(len(idx), len(ratio))
    return idx[-n:], ratio[-n:]


def _flow_series(secid: str, name: str = ""):
    """返回 (idx收盘序列, ratio主力净占比序列)。个股走 akshare，板块走东财日K+akshare兜底。"""
    if secid.startswith(("1.", "0.")):
        import akshare as ak
        code = secid.split(".", 1)[1]
        df = ak.stock_individual_fund_flow(stock=code, market=_mkt(code))
        if df is None or df.empty:
            return [], []
        idx = [float(v) for v in df["收盘价"]]
        ratio = [float(v) for v in df["主力净流入-净占比"]]
        return idx, ratio
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=250&klt=101"
           f"&secid={secid}&fields1=f1,f2,f3,f7&fields2=f51,f52,f57,f62,f63")
    kl = ((_em_get(url) or {}).get("data", {}) or {}).get("klines") or []
    idx, ratio = [], []
    for k in kl:
        p = str(k).split(",")
        try:
            ratio.append(float(p[2])); idx.append(float(p[3]))
        except (IndexError, ValueError):
            continue
    if len(idx) < 30 and name:
        ix, rt = _board_series_ak(name)
        if len(ix) >= 30:
            return ix, rt
    return idx, ratio


def compute_behavior(secid: str, name: str = "") -> dict | None:
    try:
        idx, ratio = _flow_series(secid, name)
    except Exception:
        return None
    if len(idx) < 30:
        return None
    c, hi, lo = idx[-1], max(idx), min(idx)
    pos_high = (c / hi - 1) * 100 if hi else 0
    pos_low = (c / lo - 1) * 100 if lo else 0
    trend20 = (c / idx[-21] - 1) * 100 if len(idx) >= 21 else 0
    trend5 = (c / idx[-6] - 1) * 100 if len(idx) >= 6 else 0
    flow5 = sum(ratio[-5:]) / 5
    prev5 = sum(ratio[-10:-5]) / 5 if len(ratio) >= 10 else flow5
    improve = flow5 - prev5
    bigdir, sub, direction, score = classify_behavior(pos_high, trend5, trend20, flow5, improve)
    return {"bigdir": bigdir, "label": sub, "dir": direction, "score": score,
            "pos_high": round(pos_high, 1), "pos_low": round(pos_low, 1),
            "trend5": round(trend5, 1), "trend20": round(trend20, 1),
            "flow5": round(flow5, 2), "improve": round(improve, 2)}


def behavior_of(secid: str, allow_compute: bool = True, name: str = ""):
    with _BEH_LOCK:
        node = _BEH.get(secid)
    if node and time.time() - node["ts"] < _BEH_TTL:
        return node["data"]
    dv = dc_get("beh:" + secid)
    if dv is not None:
        with _BEH_LOCK:
            _BEH[secid] = {"ts": time.time(), "data": dv}
        return dv
    if not allow_compute:
        return node["data"] if node else None
    data = compute_behavior(secid, name)
    with _BEH_LOCK:
        _BEH[secid] = {"ts": time.time(), "data": data}
    if data is not None:
        dc_set("beh:" + secid, data, ttl=_BEH_TTL)
    return data


def behaviors(items: list) -> dict:
    """items: [(secid, name), ...]"""
    from concurrent.futures import ThreadPoolExecutor
    items = [(s, n) for s, n in items if s]
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda t: behavior_of(t[0], True, t[1]), items))
    return dict(zip([s for s, _ in items], res))


_HIST: dict[str, dict] = {}
_HIST_LOCK = threading.Lock()
_HIST_TTL = 6 * 3600  # 秒


def fetch_history(h_type: str, key: str) -> list[dict]:
    """近3年周线收盘。h_type: industry / concept / stock。key: 板块名 或 股票代码。"""
    ck = f"{h_type}:{key}"
    with _HIST_LOCK:
        node = _HIST.get(ck)
        if node and time.time() - node["ts"] < _HIST_TTL:
            return node["data"]
    dv = dc_get("hist:" + ck)
    if dv is not None:
        with _HIST_LOCK:
            _HIST[ck] = {"ts": time.time(), "data": dv}
        return dv
    data = []
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        if h_type == "stock":
            df = ak.stock_zh_a_hist(symbol=key, period="weekly",
                                    start_date="20200101", end_date=end, adjust="qfq")
        elif h_type == "concept":
            df = ak.stock_board_concept_hist_em(symbol=key, start_date="20200101",
                                                end_date=end, period="周k", adjust="")
        else:
            df = ak.stock_board_industry_hist_em(symbol=key, start_date="20200101",
                                                 end_date=end, period="周k", adjust="")
        if df is not None and not df.empty:
            for _, r in df.tail(160).iterrows():
                data.append({"t": str(r["日期"])[:10], "c": float(r["收盘"])})
    except Exception as e:
        logging.warning("history fail %s: %s", key, str(e)[:60])
    with _HIST_LOCK:
        _HIST[ck] = {"ts": time.time(), "data": data}
    if data:
        dc_set("hist:" + ck, data, ttl=_HIST_TTL)
    return data


def _fetch_sector_dir(fs: str, po: int, pz: int) -> list[dict]:
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?fid=f62&po={po}&pz={pz}&pn=1"
           f"&np=1&fltt=2&invt=2&fs={fs}&fields={FIELDS}")
    rows = []
    for it in _items(_em_get(url)):
        inst = _num(it.get("f66")); hot = _num(it.get("f72"))
        mid = _num(it.get("f78")); small = _num(it.get("f84"))
        rows.append({
            "code": it.get("f12"), "name": it.get("f14"), "pct": _num(it.get("f3")),
            "main": inst + hot, "inst": inst, "hot": hot,
            "retail": mid + small, "mid": mid, "small": small,
            "amount": _num(it.get("f6")), "vr": _num(it.get("f10")), "turnover": _num(it.get("f8")),
        })
    return rows


# 市场结构类概念（长期霸榜、非题材），过滤掉
EXCLUDE_SECTORS = {
    "融资融券", "富时罗素", "MSCI中国", "标准普尔", "深股通", "沪股通",
    "转债标的", "AH股", "AB股", "破净股", "基金重仓", "GDR",
    "深成500", "大盘股", "HS300_", "上证50_", "中证500", "中证100",
    "沪深300", "上证180_", "中证1000", "创业板综", "深证100R", "上证380",
    "中盘股", "小盘股", "微盘股", "大盘蓝筹", "超大盘", "高价股", "低价股",
}


def fetch_sectors(kind: str = "industry", limit: int = 30) -> list[dict]:
    """同时取净流入 Top(limit) 与净流出 Top(limit)，避免只看到流入。"""
    fs = SECTOR_FS.get(kind, SECTOR_FS["industry"])
    need = limit + len(EXCLUDE_SECTORS)
    inflow = [r for r in _fetch_sector_dir(fs, 1, need) if r["name"] not in EXCLUDE_SECTORS][:limit]
    outflow = [r for r in _fetch_sector_dir(fs, 0, need) if r["name"] not in EXCLUDE_SECTORS][:limit]
    merged: dict[str, dict] = {}
    for r in inflow + outflow:
        merged[r["code"]] = r
    return list(merged.values())


def load_watch() -> dict:
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {"sectors": list(d.get("sectors", [])), "stocks": list(d.get("stocks", []))}
    except Exception:
        return {"sectors": [], "stocks": []}


def save_watch(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(WATCH_FILE), exist_ok=True)
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump({"sectors": list(d.get("sectors", [])),
                       "stocks": list(d.get("stocks", [])),
                       "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _secid(code: str) -> str:
    c = str(code).strip()
    if c.startswith("6"):
        return "1." + c          # 上交所
    return "0." + c              # 深交所 / 北交所


# ============ 个股回购（后台加载 + 缓存） ============
_REP_LOCK = threading.Lock()
_REP = {"df": None, "loading": False}
_REP_CACHE = os.path.join(ROOT_DIR, "data", "repurchase_cache.csv")


def _rep_load_worker():
    import pandas as pd
    df = None
    try:
        if os.path.exists(_REP_CACHE):
            if datetime.fromtimestamp(os.path.getmtime(_REP_CACHE)).strftime("%Y%m%d") == datetime.now().strftime("%Y%m%d"):
                df = pd.read_csv(_REP_CACHE, dtype=str)
    except Exception:
        df = None
    if df is None:
        try:
            import akshare as ak
            df = ak.stock_repurchase_em()
            df.to_csv(_REP_CACHE, index=False, encoding="utf-8-sig")
        except Exception:
            df = None
    with _REP_LOCK:
        _REP["df"] = df
        _REP["loading"] = False


def ensure_repurchase():
    with _REP_LOCK:
        if _REP["df"] is not None or _REP["loading"]:
            return
        _REP["loading"] = True
    threading.Thread(target=_rep_load_worker, daemon=True).start()


def repurchase_of(code: str):
    ensure_repurchase()
    with _REP_LOCK:
        df = _REP["df"]
    if df is None:
        return None
    c = str(code).zfill(6)
    try:
        sub = df[df["股票代码"].astype(str).str.zfill(6) == c]
    except Exception:
        return None
    if sub.empty:
        return []
    out = []

    def f(r, k):
        try:
            v = r.get(k)
            return None if v in (None, "", "nan") else float(v)
        except (TypeError, ValueError):
            return None
    for _, r in sub.head(5).iterrows():
        out.append({
            "plan_amt": f(r, "计划回购金额区间-上限"),
            "done_amt": f(r, "已回购金额"),
            "plan_price": str(r.get("计划回购价格区间", "") or ""),
            "progress": str(r.get("实施进度", "") or ""),
            "date": str(r.get("最新公告日期", "") or "")[:10],
            "ratio": f(r, "占公告前一日总股本比例-上限"),
        })
    return out


def changes_of(code: str) -> dict:
    """股东/高管增减持（同花顺）。返回最近若干条。"""
    ck = "chg:" + str(code)
    cv = dc_get(ck)
    if cv is not None:
        return cv
    out = {"management": [], "shareholder": []}
    try:
        import akshare as ak
        import pandas as pd
        m = ak.stock_management_change_ths(symbol=str(code))
        if m is not None and not m.empty:
            m = m.copy()
            m["_d"] = pd.to_datetime(m["变动日期"], errors="coerce")
            m = m.sort_values("_d", ascending=False)
            for _, r in m.head(3).iterrows():
                out["management"].append({
                    "date": str(r.get("变动日期", ""))[:10], "who": str(r.get("变动人", "")),
                    "rel": str(r.get("与公司高管关系", "")), "chg": str(r.get("变动数量", "")),
                    "price": str(r.get("交易均价", "")), "way": str(r.get("股份变动途径", "")),
                })
    except Exception:
        pass
    try:
        import akshare as ak
        import pandas as pd
        s = ak.stock_shareholder_change_ths(symbol=str(code))
        if s is not None and not s.empty:
            s = s.copy()
            s["_d"] = pd.to_datetime(s["公告日期"], errors="coerce")
            s = s.sort_values("_d", ascending=False)
            for _, r in s.head(3).iterrows():
                out["shareholder"].append({
                    "date": str(r.get("公告日期", ""))[:10], "who": str(r.get("变动股东", "")),
                    "chg": str(r.get("变动数量", "")), "price": str(r.get("交易均价", "")),
                    "way": str(r.get("变动途径", "")),
                })
    except Exception:
        pass
    dc_set(ck, out, ttl=12 * 3600)
    return out


def daily_flow(code: str, n: int = 5) -> list:
    """近 n 日每天的主力净流入（仅净额）。"""
    ck = "df5:" + str(code)
    cv = dc_get(ck)
    if cv is not None:
        return cv
    out = []
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow(stock=str(code), market=_mkt(code))
        if df is not None and not df.empty:
            for _, r in df.tail(n).iterrows():
                out.append({"d": str(r["日期"])[5:], "v": float(r["主力净流入-净额"])})
    except Exception:
        out = []
    if out:
        dc_set(ck, out, ttl=3600)
    return out


def fetch_stock(code: str) -> dict:
    """个股资金流（含资金来源四档）。"""
    code = str(code).strip()
    if not code.isdigit() or len(code) != 6:
        return {"error": "请输入 6 位股票代码"}
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={_secid(code)}&fields=f12,f14,f2,f3,f62,f184,f66,f72,f78,f84,f6,f10,f8")
    items = _items(_em_get(url))
    if not items:
        return {"error": f"未找到 {code}"}
    it = items[0]
    inst = _num(it.get("f66")); hot = _num(it.get("f72"))
    mid = _num(it.get("f78")); small = _num(it.get("f84"))
    try:
        beh = behavior_of(stock_secid(code))
    except Exception:
        beh = None
    try:
        rep = repurchase_of(code)
    except Exception:
        rep = None
    try:
        chg = changes_of(code)
    except Exception:
        chg = None
    return {
        "code": it.get("f12"), "name": it.get("f14"),
        "price": _num(it.get("f2")), "pct": _num(it.get("f3")),
        "main": inst + hot, "inst": inst, "hot": hot,
        "retail": mid + small, "mid": mid, "small": small,
        "main_pct": _num(it.get("f184")),
        "amount": _num(it.get("f6")), "vr": _num(it.get("f10")), "turnover": _num(it.get("f8")),
        "beh": beh, "repurchase": rep, "changes": chg,
        "daily5": daily_flow(code),
    }


def fetch_board_stocks(board_code: str, n: int = 3) -> dict:
    """板块成分股主力净流入：返回 inflow/outflow 各前 n。"""
    def one(desc: bool):
        po = 1 if desc else 0
        url = (f"https://push2.eastmoney.com/api/qt/clist/get?fid=f62&po={po}&pz={n}&pn=1"
               f"&np=1&fltt=2&invt=2&fs=b:{board_code}&fields=f12,f14,f3,f62")
        out = []
        for it in _items(_em_get(url))[:n]:
            out.append({"code": it.get("f12"), "name": it.get("f14"),
                        "pct": _num(it.get("f3")), "net": _num(it.get("f62"))})
        return out
    return {"inflow": one(True), "outflow": one(False)}


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股板块资金流向 · 方框热力图</title>
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#2b333d;--txt:#e8edf3;--muted:#94a0ad;
--up:#ff4d4f;--down:#26a69a;--accent:#4d9fff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.55 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:9}
h1{font-size:17px;margin:0;font-weight:700}
.tag{font-size:13px;color:var(--muted)}
.sp{flex:1}
button{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 13px;cursor:pointer;font-size:13px}
button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.legend{padding:9px 20px;color:var(--muted);font-size:13px;border-bottom:1px solid var(--line)}
.legend b{color:var(--txt)}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;margin-right:6px}
.secttl{padding:12px 20px 6px;color:var(--muted);font-size:13px;display:flex;align-items:center;gap:12px}
.secttl .clr{cursor:pointer;color:var(--accent);font-size:13px}
.grid{display:flex;flex-wrap:wrap;gap:10px;padding:10px 20px 28px}
.tile{width:132px;height:74px;border-radius:10px;border:1px solid rgba(255,255,255,.10);
padding:9px 11px;cursor:pointer;display:flex;flex-direction:column;justify-content:space-between;
transition:transform .08s, box-shadow .08s;position:relative;overflow:hidden}
.tile:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.55)}
.tile .nm{font-size:14px;font-weight:700;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.9);line-height:1.2;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile .vl{font-size:16px;font-weight:800;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.9);font-variant-numeric:tabular-nums}
.tile .pc{font-size:12px;color:#eef2f7;text-shadow:0 1px 3px rgba(0,0,0,.9)}
.tile.sel{outline:2px solid #fff}
.tbeh{position:absolute;top:5px;right:6px;z-index:2}
.btag{display:inline-block;font-size:11px;font-weight:700;padding:1px 7px;border-radius:9px;line-height:1.5;white-space:nowrap}
.gtag{display:inline-block;font-size:10px;font-weight:700;padding:0 5px;border-radius:8px;margin-left:6px;line-height:1.6;vertical-align:middle}
.gtag.bull{background:#d6282c;color:#fff;text-shadow:none}
.gtag.bear{background:#14786e;color:#fff;text-shadow:none}
.bg-bull{background:#d6282c;color:#fff;text-shadow:none}
.bg-bear{background:#14786e;color:#fff;text-shadow:none}
.bg-neutral{background:#3a434d;color:#e6edf3;text-shadow:none}
.selgrid{display:flex;flex-wrap:wrap;gap:12px;padding:6px 20px 16px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.mkt{padding:10px 20px;font-size:15px;border-bottom:1px solid var(--line);background:#0b0f14}
.mkt b{font-size:18px;font-weight:800;margin:0 2px;font-variant-numeric:tabular-nums}
.mkt .seg{margin-right:18px}
.helpbox{padding:12px 20px;background:#0b0f14;border-bottom:1px solid var(--line);font-size:13px;line-height:1.8;color:var(--txt)}
.helpbox code{background:#161b22;padding:1px 6px;border-radius:4px;color:#ffd27a}
.spark{padding:4px 20px 8px;background:#0b0f14;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.spark .cap{color:var(--muted);font-size:12px}
.srow{padding:2px 20px 8px;display:flex;align-items:center;gap:10px}
.cin{background:#0b0f14;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:7px 12px;font-size:14px;width:150px}
.cin:focus{outline:none;border-color:var(--accent)}
.sbox{position:relative}
.sug{position:absolute;top:100%;left:0;width:250px;background:#0b0f14;border:1px solid var(--line);
border-radius:8px;margin-top:4px;z-index:50;display:none;max-height:330px;overflow:auto;box-shadow:0 10px 30px rgba(0,0,0,.6)}
.sug .it{padding:8px 12px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;font-size:13px}
.sug .it:hover{background:#1b2129}
.sug .it .c{color:var(--muted)}
.sug .none{padding:9px 12px;color:var(--muted);font-size:12px}
.box{width:250px;border-radius:12px;border:1px solid rgba(255,255,255,.12);padding:12px 14px;position:relative;overflow:hidden}
.box,.box .bnm,.box .bpct,.box .bmain{color:#fff !important;text-shadow:0 1px 3px rgba(0,0,0,.9)}
.box .top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.box .bnm{font-size:16px;font-weight:800}
.box .bpct{font-size:13px;font-weight:600}
.box .bmain{font-size:22px;font-weight:900;font-variant-numeric:tabular-nums;margin:2px 0 8px}
.row{display:flex;align-items:center;gap:8px;font-size:13px;margin:4px 0}
.row .lab{width:96px;color:#f0f4f8;text-shadow:0 1px 3px rgba(0,0,0,.9)}
.row .val{width:72px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.9)}
.row .bwrap{flex:1;height:7px;background:rgba(0,0,0,.35);border-radius:4px;overflow:hidden}
.row .bfill{height:100%}
.box .x{position:absolute;top:8px;right:11px;cursor:pointer;color:#fff;font-size:14px;opacity:.75}
.box .x:hover{opacity:1}
.tip{position:fixed;pointer-events:none;z-index:99;background:#0b0f14;border:1px solid var(--line);
border-radius:10px;padding:12px 14px;font-size:14px;box-shadow:0 12px 34px rgba(0,0,0,.7);display:none;min-width:440px;max-width:600px}
.tip .scols{display:flex;gap:20px}
.tip .scol{flex:1;min-width:0}
.tip .tt{font-weight:800;font-size:16px;margin-bottom:8px;color:#fff}
.tip .tt .pct{font-size:13px;font-weight:600}
.tip .tr{display:flex;justify-content:space-between;gap:18px;font-variant-numeric:tabular-nums;margin:3px 0}
.tip .tr b{font-weight:700}
.tip .div{border-top:1px solid var(--line);margin:9px 0}
.tip .sh{font-size:12px;color:var(--muted);margin:3px 0 4px}
.tip .stk{display:flex;justify-content:space-between;gap:12px;font-size:13.5px;margin:3px 0}
.tip .stk .sn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px}
.tip .load{color:var(--muted);font-size:12px}
.tipchart{margin-top:9px;border-top:1px solid var(--line);padding-top:8px}
.tipchart svg{display:block}
.up{color:var(--up)}.down{color:var(--down)}
</style>
</head>
<body>
<header>
  <h1>A股板块资金流向 · 方框热力图</h1>
  <span class="tag" id="ts"></span>
  <span class="sp"></span>
  <button id="bInd" class="on">行业</button>
  <button id="bCon">概念</button>
  <button id="bAuto" class="on">自动刷新</button>
  <button id="bRefresh">刷新</button>
  <button id="bHelp">口径说明 ?</button>
</header>
<div class="helpbox" id="help" style="display:none">
  <b>为什么"主力 ≈ −散户"？为什么"主力+散户=0"？</b><br>
  资金流向<b>不是"钱进/出市场"</b>，而是按<b>每笔成交的主动方单量</b>分档记账：
  <br>　· 买入方的单量档位 → 记为该档"<span class="up">流入</span>"
  <br>　· 卖出方的单量档位 → 记为该档"<span class="down">流出</span>"
  <br>一买一卖金额相等、符号相反 → <b>所有档加总恒 = 0</b>。
  <br><br>
  所以：<br>
  　<code>主力 = 机构(超大单) + 游资(大单)</code>（主力<b>含</b>游资，别相加）<br>
  　<code>散户 = 中单 + 小单</code><br>
  　<code>主力 + 散户 ≈ 0</code>（恒等式，不是"大盘流入"）
  <br><br>
  <b>"大盘资金流入"（东财/同花顺显示的 -266 之类）= 主力净流入 = 超大单 + 大单</b>，
  它随时间(分钟级)和口径(分单门槛)波动，和"主力+散户"无关。
  <br><br>
  它衡量的是<b>主动买卖的方向与单量结构（情绪/行为）</b>，<b>不是真实资金进出股市</b>。
  <br><br>
  <b>常见误读（重点）</b><br>
  　❌ "散户买 = 主力卖" → 这是<b>记账恒等</b>，<b>不是因果</b>；对手盘身份未知。<br>
  　❌ "主力净流出 = 主力看空" → 可能是调仓/换手/获利了结/拆单吸筹/ETF申赎/被动挂单承接。<br>
  　❌ 看单日就下结论 → 净额噪音极大。<br>
  　✅ 正确读法：看<b>持续性</b>、<b>位置</b>(低位vs高位)、<b>价量配合</b>(放量出货/缩量洗盘)、<b>龙虎榜</b>确认；个股比大盘更有意义。
  <br><br>
  <b>主力行为标签后的数字（如 66分）</b>：是该行为的<b>规则强度/置信分（0-100）</b>，
  <b>不是概率、不是预期涨幅</b>，仅为分类优先级（逃顶82 / 抄底80 / 诱多74 / 诱低66 / 拉升60 / 洗盘48 / 离场45 / 无信号30）。
</div>
<div class="legend">
  <b>颜色</b>：越红=主力净流入越多，越绿=净流出越多　
  <b>资金来源</b>（东财按单笔金额分档估算）：
  <span class="pill" style="background:#3a2a2a;color:#ff9a9a">主力 = 机构(超大单) + 游资(大单)</span>
  <span class="pill" style="background:#22303a;color:#8fd6ff">散户 = 中单 + 小单</span>
  <span class="pill" style="background:#3a3222;color:#ffd27a">主力 ≈ −散户（全市场净额≈0）</span>
  　点方框加入展示区 · 悬浮看明细 + 板块内流入/流出 Top10
  <br>主力行为评估（大方向 → 细分）：
  <span class="btag bg-bull">抄底</span><span class="tag">横盘/挖坑/下跌吸筹</span>
  <span class="btag bg-bull">诱低</span><span class="tag">破位诱空/急跌吸筹</span>
  <span class="btag bg-bear">诱多</span><span class="tag">放量滞涨/冲高回落</span>
  <span class="btag bg-bear">逃顶</span><span class="tag">高位出货/阴跌出货</span>
  <span class="tag">｜依据：位置 + 5/20日趋势 + 近5日主力净占比 + 改善（后台随盘更新）</span>
</div>
<div class="mkt" id="mkt">沪深两市主力净流入：加载中…</div>
<div class="spark" id="spark"></div>
<div class="secttl">展示区（点击下方板块加入，最多 8 个）<span class="clr" id="selclear">清空</span></div>
<div class="selgrid" id="selgrid"><div class="empty">点击任意板块方框，添加到这里看详细</div></div>
<div class="secttl">个股（输入股票代码，如 600812 / 300308）</div>
<div class="srow">
  <div class="sbox">
    <input id="codeIn" class="cin" placeholder="输入代码或名称，如 中远 / 600812" autocomplete="off"/>
    <div class="sug" id="sug"></div>
  </div>
  <button id="addStock">添加</button>
  <span id="stockMsg" class="tag"></span>
</div>
<div class="selgrid" id="stockgrid"><div class="empty">输入股票代码后点击添加，查看该股资金流（分资金来源）</div></div>
<div class="secttl">全部板块 <span id="cnt" class="tag"></span></div>
<div class="grid" id="grid"></div>
<div class="tip" id="tip"></div>
<script>
let kind="industry", auto=true, timer=null, data=[], selected=[], stocks=[], stockCache={}, behMap={}, hoverReq=0, watchLoaded=false;
const $=s=>document.querySelector(s);
const YI=v=>{const x=(v||0)/1e8;return (x>0?"+":"")+x.toFixed(2)+"亿"};
const AMT=v=>(v==null||isNaN(v))?"—":(v/1e8).toFixed(2)+"亿";
const NUM2=v=>(v==null||isNaN(v)||v===0)?"—":Number(v).toFixed(2);
function volHTML(r){
  if(r.amount==null && r.vr==null) return "";
  return `<div class="tr"><span>成交额 / 量比</span><b>${AMT(r.amount)} · ${NUM2(r.vr)}</b>${r.turnover?` <small class="tag">换手 ${NUM2(r.turnover)}%</small>`:""}</div>`;
}
function daily5HTML(r){
  const a=(r&&r.daily5)||[]; if(!a.length) return "";
  const max=Math.max(1,...a.map(x=>Math.abs(x.v)));
  const bw=30,gap=12,base=34,H=50,W=a.length*(bw+gap)-gap+2;
  let rects="",lab="";
  a.forEach((x,i)=>{
    const bh=Math.max(2,Math.abs(x.v)/max*(base-6));
    const col=x.v>=0?'#d6282c':'#14786e';
    const y=x.v>=0?(base-bh):base;
    const cx=i*(bw+gap);
    rects+=`<rect x="${cx}" y="${y.toFixed(1)}" width="${bw}" height="${bh.toFixed(1)}" rx="2" fill="${col}"/>`;
    lab+=`<text x="${cx+bw/2}" y="${H-1}" text-anchor="middle" font-size="9" fill="#94a0ad">${x.d}</text>`;
  });
  const vals=a.map(x=>`<span class="${x.v>=0?'up':'down'}">${x.v>=0?'+':''}${(x.v/1e8).toFixed(1)}</span>`).join("&nbsp;&nbsp;");
  return `<div class="tipchart"><div class="sh">近5日主力净流入（亿，左→右=早→新）</div>
    <svg width="${W}" height="${H}">${rects}${lab}</svg>
    <div class="cap" style="font-size:11px;margin-top:2px">${vals}</div></div>`;
}
const CLS=v=>v>0?"up":v<0?"down":"";
const GT=b=>b?'<span class="gtag bull">利好</span>':'<span class="gtag bear">利空</span>';
function repHTML(r){
  const rep=r&&r.repurchase;
  if(rep===undefined||rep===null) return "";
  if(!rep.length) return `<div class="tr"><span>股东回购</span><small class="tag">近一年无回购记录</small></div>`;
  const x=rep[0], yi=v=>(v==null?"—":(v/1e8).toFixed(2)+"亿");
  return `<div class="tr"><span>股东回购</span><b>已回购 ${yi(x.done_amt)} / 计划≤${yi(x.plan_amt)}</b>${GT(true)}</div>
    <div class="tr"><span></span><small class="tag">${x.progress||''} · 价格上限${x.plan_price||'—'} · 占流通${x.ratio==null?'—':x.ratio.toFixed(2)+'%'} · ${x.date||''}</small></div>`;
}
function chgDir(t){ t=String(t); return t.indexOf("增持")>=0?"up":(t.indexOf("减持")>=0?"down":""); }
function chgHTML(r){
  const c=r&&r.changes; if(c===undefined||c===null) return "";
  const m=c.management||[], s=c.shareholder||[];
  if(!m.length && !s.length) return "";
  const tagOf=t=>{const d=chgDir(t); return d?GT(d==="up"):"";};
  let h="";
  for(const x of m) h+=`<div class="tr"><span></span><small class="tag">高管 ${x.date} ${x.who}(${x.rel}) <b class="${chgDir(x.chg)}">${x.chg}</b>${tagOf(x.chg)} @${x.price} · ${x.way}</small></div>`;
  for(const x of s) h+=`<div class="tr"><span></span><small class="tag">股东 ${x.date} ${x.who} <b class="${chgDir(x.chg)}">${x.chg}</b>${tagOf(x.chg)} · ${x.way}</small></div>`;
  return `<div class="tr"><span>增减持</span></div>`+h;
}
function shareholderHTML(r){ return repHTML(r)+chgHTML(r); }
function behOf(r){ return (r&&r.beh)||behMap[r&&r.code]||null; }
function behBadge(r,full){
  const b=behOf(r);
  if(!b) return "";
  if(!full && b.label==="无明显信号") return "";
  const short=(b.bigdir && b.bigdir!=="中性")?b.bigdir:b.label;
  const txt=full?`${b.bigdir}·${b.label} ${b.score}分`:short;
  return `<span class="btag bg-${b.dir||"neutral"}">${txt}</span>`;
}
let behReq=0;
async function loadBeh(){
  const pend=selected.filter(s=>behMap[s.code]===undefined);
  const codes=pend.map(s=>s.code);
  if(!codes.length) return;
  const req=++behReq;
  const q=pend.map(s=>"90."+s.code+"|"+s.name).join(",");
  try{
    const r=await fetch("/api/beh?secids="+encodeURIComponent(q));
    const j=await r.json(); const b=j.beh||{};
    let changed=false;
    for(const c of codes){ const v=b["90."+c]; if(v!==undefined){ behMap[c]=v; changed=true; } else { behMap[c]=null; } }
    if(changed && req===behReq){ renderGrid(); renderSel(); }
  }catch(e){}
}
let maxAbs=1;
function heat(v){
  const a=Math.min(1,Math.sqrt(Math.abs(v)/maxAbs));
  const al=0.14+0.80*a;
  return v>=0?`rgba(214,40,44,${al})`:`rgba(20,120,110,${al})`;
}
function tile(r){
  const el=document.createElement("div");
  el.className="tile"+(selected.some(s=>s.code===r.code)?" sel":"");
  el.style.background=heat(r.main);
  el.innerHTML=`<span class="tbeh">${behBadge(r,false)}</span><div class="nm">${r.name}</div>
    <div><span class="vl">${YI(r.main)}</span> <span class="pc">${r.pct>0?"+":""}${r.pct.toFixed(1)}%</span></div>`;
  el.onmouseenter=e=>showTip(e,r);
  el.onmousemove=moveTip;
  el.onmouseleave=hideTip;
  el.onclick=()=>toggle(r);
  return el;
}
function renderGrid(){
  const g=$("#grid"); g.innerHTML="";
  [...data].sort((a,b)=>b.main-a.main).forEach(r=>g.appendChild(tile(r)));
  $("#cnt").textContent=data.length+" 个";
}
function row(label,v){
  const a=Math.min(1,Math.abs(v)/Math.max(1,Math.abs(ctxMax())));
  const col=v>=0?"var(--up)":"var(--down)";
  return `<div class="row"><span class="lab">${label}</span><span class="val">${YI(v)}</span>
    <span class="bwrap"><span class="bfill" style="width:${(a*100).toFixed(0)}%;background:${col}"></span></span></div>`;
}
function ctxMax(){const s=selected[0]||data[0];if(!s)return 1;return Math.max(Math.abs(s.inst),Math.abs(s.hot),Math.abs(s.retail),1e8)}
function renderSel(){
  const g=$("#selgrid");
  if(!selected.length){g.innerHTML='<div class="empty">点击任意板块方框，添加到这里看详细</div>';return;}
  g.innerHTML="";
  for(const r of selected){
    const el=document.createElement("div");
    el.className="box"; el.style.background=heat(r.main);
    el.innerHTML=`<span class="x" title="移除">✕</span>
      <div class="top"><span class="bnm">${r.name}</span><span class="bpct">${r.pct>0?"+":""}${r.pct.toFixed(2)}%</span></div>
      <div class="bmain">${YI(r.main)}<small class="tag"> 主力</small></div>
      <div style="margin:-2px 0 7px">${behBadge(r,true)}</div>
      ${volHTML(r)}
      <div>${row("　机构(超大单)",r.inst)}${row("　游资(大单)",r.hot)}${row("散户(中+小)",r.retail)}${row("　中单",r.mid)}${row("　小单",r.small)}</div>`;
    g.appendChild(el);
    el.querySelector(".x").onclick=e=>{e.stopPropagation();toggle(r)};
    el.onmouseenter=e=>showTip(e,r); el.onmousemove=moveTip; el.onmouseleave=hideTip;
  }
}
function toggle(r){
  const i=selected.findIndex(s=>s.code===r.code);
  if(i>=0) selected.splice(i,1);
  else { selected.push(r); if(selected.length>8) selected.shift(); }
  renderGrid(); renderSel(); saveWatch(); loadBeh();
}
function saveWatch(){
  const body={sectors:selected.map(s=>s.code), stocks:stocks.map(s=>s.code)};
  fetch("/api/watch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).catch(()=>{});
}
async function loadWatch(){
  try{
    const r=await fetch("/api/watch"); const j=await r.json();
    selected=(j.sectors||[]).map(c=>data.find(d=>d.code===c)).filter(Boolean);
    renderGrid(); renderSel();
    for(const c of (j.stocks||[])){
      try{ const rr=await fetch("/api/stock?code="+encodeURIComponent(c)); const s=await rr.json();
        if(s && !s.error && !stocks.some(x=>x.code===s.code)) stocks.push(s);
      }catch(e){}
    }
    renderStocks(); renderGrid(); renderSel(); loadBeh();
    $("#ts").textContent+=" · 已恢复自选";
  }catch(e){}
}
function tipBody(r){
  const b=behOf(r);
  return `<div class="tt">${r.name} <span class="pct ${CLS(r.pct)}">${r.pct>0?"+":""}${r.pct.toFixed(2)}%</span> ${behBadge(r,true)}</div>
    ${b?`<div class="tr"><span>行为依据</span><small class="tag">距高${b.pos_high}% · 5日${b.trend5>0?"+":""}${b.trend5}% · 20日${b.trend20>0?"+":""}${b.trend20}% · 近5主力${b.flow5>0?"+":""}${b.flow5}% · 改善${b.improve>0?"+":""}${b.improve}</small>${b.dir&&b.dir!=="neutral"?GT(b.dir==="bull"):""}</div>`:""}
    <div class="tr"><span>主力净流入</span><b class="${CLS(r.main)}">${YI(r.main)}</b>${GT((r.main||0)>0)}</div>
    ${volHTML(r)}
    <div class="tr"><span>　├ 机构(超大单)</span><b class="${CLS(r.inst)}">${YI(r.inst)}</b></div>
    <div class="tr"><span>　└ 游资(大单)</span><b class="${CLS(r.hot)}">${YI(r.hot)}</b></div>
    <div class="tr"><span>散户(中+小)</span><b class="${CLS(r.retail)}">${YI(r.retail)}</b></div>
    <div class="tr"><span>　├ 中单</span><b class="${CLS(r.mid)}">${YI(r.mid)}</b></div>
    <div class="tr"><span>　└ 小单</span><b class="${CLS(r.small)}">${YI(r.small)}</b></div>`+shareholderHTML(r)+daily5HTML(r);
}
function stockHTML(s){
  if(!s) return '<div class="div"></div><div class="load">加载成分股…</div>';
  const col=(title,arr,cls,arrow)=>`<div class="scol"><div class="sh">${arrow} 主力净${title} Top10</div>`+
    arr.map(x=>`<div class="stk"><span class="sn">${x.name} ${x.pct>0?"+":""}${x.pct.toFixed(1)}%</span><b class="${cls}">${YI(x.net)}</b></div>`).join("")+`</div>`;
  return `<div class="div"></div><div class="scols">
    ${col("流入",s.inflow||[],"up","↑")}
    ${col("流出",s.outflow||[],"down","↓")}</div>`;
}
let histCache={};
function sparkSVG(pts){
  const w=300,h=64,pad=4,N=pts.length;
  const vals=pts.map(p=>p.c), min=Math.min(...vals), max=Math.max(...vals), rng=(max-min)||1;
  const X=i=>pad+i*(w-2*pad)/((N-1)||1);
  const Y=v=>h-pad-(v-min)/rng*(h-2*pad);
  const path=pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.c).toFixed(1)}`).join(' ');
  const chg=vals[N-1]/vals[0]-1, color=chg>=0?'#ff4d4f':'#26a69a';
  return `<svg width="${w}" height="${h}"><path d="${path}" fill="none" stroke="${color}" stroke-width="1.6"/></svg>
    <div class="cap" style="font-size:12px;color:var(--muted);margin-top:3px">
      近 ${N} 周：${pts[0].t} → ${pts[N-1].t}　<span class="${chg>=0?'up':'down'}">${chg>=0?'+':''}${(chg*100).toFixed(0)}%</span>　高${max.toFixed(1)} 低${min.toFixed(1)}</div>`;
}
async function loadTipChart(type,key){
  const ck=type+":"+key;
  let w=histCache[ck];
  if(w===undefined){
    try{ const r=await fetch("/api/history?type="+type+"&key="+encodeURIComponent(key)); const j=await r.json(); w=j.w||[]; }
    catch(e){ w=[]; }
    histCache[ck]=w;
  }
  const el=document.getElementById("tipchart");
  if(el && el.dataset.ck===ck) el.innerHTML = w.length? sparkSVG(w) : '<span class="load">无历史数据</span>';
}
function showTip(e,r,noSub){
  const t=$("#tip");
  const hist = noSub ? {type:"stock", key:(r.code||"")} : {type:kind, key:(r.name||"")};
  if(noSub){
    t.innerHTML=tipBody(r)+`<div class="tipchart" id="tipchart">周线加载中…</div>`;
    const el=document.getElementById("tipchart"); if(el) el.dataset.ck=hist.type+":"+hist.key;
    t.style.display="block"; moveTip(e); loadTipChart(hist.type,hist.key); return;
  }
  const cached=stockCache[r.code];
  t.innerHTML=tipBody(r)+`<div id="tipstk">${stockHTML(cached)}</div>`+`<div class="tipchart" id="tipchart">周线加载中…</div>`;
  const el=document.getElementById("tipchart"); if(el) el.dataset.ck=hist.type+":"+hist.key;
  t.style.display="block"; moveTip(e);
  if(!cached){
    const req=++hoverReq;
    fetch("/api/stocks?board="+r.code).then(x=>x.json()).then(j=>{
      stockCache[r.code]=j;
      const s=document.getElementById("tipstk");
      if(req===hoverReq && s) s.innerHTML=stockHTML(j);
    }).catch(()=>{});
  }
  loadTipChart(hist.type,hist.key);
}
function stockCard(s){
  const el=document.createElement("div");
  el.className="box"; el.style.background=heat(s.main);
  el.innerHTML=`<span class="x" title="移除">✕</span>
    <div class="top"><span class="bnm">${s.name} <small class="tag">${s.code}</small></span>
    <span class="bpct">${(s.price||0).toFixed(2)} ${s.pct>0?"+":""}${s.pct.toFixed(2)}%</span></div>
    <div class="bmain">${YI(s.main)}<small class="tag"> 净流入</small></div>
    <div style="margin:-2px 0 6px">${behBadge(s,true)}</div>
    ${volHTML(s)}`;
  el.querySelector(".x").onclick=e=>{e.stopPropagation();stocks=stocks.filter(x=>x.code!==s.code);renderStocks();saveWatch()};
  el.onmouseenter=e=>showTip(e,s,true); el.onmousemove=moveTip; el.onmouseleave=hideTip;
  return el;
}
function renderStocks(){
  const g=$("#stockgrid");
  if(!stocks.length){g.innerHTML='<div class="empty">输入股票代码后点击添加，查看该股资金流（分资金来源）</div>';return;}
  g.innerHTML="";
  [...stocks].sort((a,b)=>b.main-a.main).forEach(s=>g.appendChild(stockCard(s)));
}
async function addStock(){
  const code=$("#codeIn").value.trim();
  hideSug();
  if(!code)return;
  $("#stockMsg").textContent="查询中…";
  try{
    const r=await fetch("/api/stock?code="+encodeURIComponent(code));
    const j=await r.json();
    if(j.error){$("#stockMsg").textContent=j.error;return;}
    $("#stockMsg").textContent="";
    if(stocks.some(x=>x.code===j.code)) stocks=stocks.map(x=>x.code===j.code?j:x);
    else stocks.push(j);
    $("#codeIn").value=""; renderStocks(); saveWatch();
  }catch(e){$("#stockMsg").textContent="失败 "+e;}
}
async function refreshStocks(){
  if(!stocks.length)return;
  const out=[];
  for(const s of stocks){
    try{const r=await fetch("/api/stock?code="+s.code);const j=await r.json();out.push(j.error?s:j);}
    catch(e){out.push(s)}
  }
  stocks=out; renderStocks();
}
function moveTip(e){
  const t=$("#tip"); let x=e.clientX+16, y=e.clientY+14;
  const w=t.offsetWidth||320, h=t.offsetHeight||260;
  if(x+w>innerWidth)x=e.clientX-w-16; if(y+h>innerHeight)y=Math.max(8,e.clientY-h-14);
  t.style.left=x+"px"; t.style.top=y+"px";
}
function hideTip(){$("#tip").style.display="none"; hoverReq++}
let sugTimer=null, sugData=[];
function hideSug(){ const s=$("#sug"); if(s) s.style.display="none"; }
function renderSug(){
  const s=$("#sug");
  if(!sugData.length){ s.innerHTML='<div class="none">无匹配股票</div>'; s.style.display="block"; return; }
  s.innerHTML=sugData.map((x,i)=>`<div class="it" data-i="${i}"><span>${x.name}</span><span class="c">${x.code} ${x.type||""}</span></div>`).join("");
  s.style.display="block";
  s.querySelectorAll(".it").forEach(el=>el.onclick=()=>{
    const x=sugData[+el.dataset.i];
    $("#codeIn").value=x.code; hideSug(); addStock();
  });
}
function renderSpark(pts){
  const el=$("#spark");
  if(!pts||!pts.length){el.innerHTML='<span class="cap">今日无分时数据（休市/未开盘）</span>';return;}
  const w=300,h=48,pad=4,N=pts.length;
  const vals=pts.map(p=>p.v);
  const min=Math.min(...vals,0), max=Math.max(...vals,0), range=(max-min)||1;
  const X=i=>pad+i*(w-2*pad)/((N-1)||1);
  const Y=v=>h-pad-(v-min)/range*(h-2*pad);
  const path=pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.v).toFixed(1)}`).join(' ');
  const zy=Y(0), last=vals[N-1], color=last>=0?'#ff4d4f':'#26a69a';
  let iA=pts.findIndex(p=>p.t>='11:30'); if(iA<0)iA=N-1;
  const svg=`<svg width="${w}" height="${h}" style="display:block">
    <line x1="${pad}" y1="${zy.toFixed(1)}" x2="${w-pad}" y2="${zy.toFixed(1)}" stroke="#3a434d" stroke-dasharray="3 3"/>
    <path d="${path}" fill="none" stroke="${color}" stroke-width="1.6"/>
    <circle cx="${X(N-1).toFixed(1)}" cy="${Y(last).toFixed(1)}" r="2.6" fill="${color}"/></svg>`;
  const cap=`<span class="cap">今日曲线：开盘 ${YI(pts[0].v)}　上午收盘(11:30) <b class="${CLS(vals[iA])}">${YI(vals[iA])}</b>　最新(${pts[N-1].t}) <b class="${CLS(last)}">${YI(last)}</b>　（区间 ${YI(min)} ~ ${YI(max)}）</span>`;
  el.innerHTML=svg+cap;
}
async function loadCurve(){
  try{const r=await fetch("/api/market_curve");const j=await r.json();renderSpark(j.curve||[]);}catch(e){}
}
async function load(){
  try{
    const r=await fetch("/api/sectors?kind="+kind); const j=await r.json();
    data=j.rows||[];
    maxAbs=Math.max(1,...data.map(d=>Math.abs(d.main)));
    selected=selected.map(s=>data.find(d=>d.code===s.code)||s);
    $("#ts").textContent="更新 "+j.time;
    if(j.market){const m=j.market;
      $("#mkt").innerHTML=
        `<span class="seg">沪深两市主力净流入 <b class="${CLS(m.main)}">${YI(m.main)}</b>`+
        `<small class="tag">（其中 机构(超大单) <b class="${CLS(m.inst)}">${YI(m.inst)}</b> + 游资(大单) <b class="${CLS(m.hot)}">${YI(m.hot)}</b>）</small></span>`+
        `<span class="seg">散户(中+小) <b class="${CLS(m.retail)}">${YI(m.retail)}</b>`+
        `<small class="tag">（中单 ${YI(m.mid)} + 小单 ${YI(m.small)}）</small></span>`+
        `<span class="seg"><small class="tag">主力+散户 ${YI(m.main+m.retail)}≈0　截至 ${j.time}</small></span>`;
    }
    renderGrid(); renderSel(); loadBeh();
  }catch(e){$("#ts").textContent="加载失败 "+e;}
  loadCurve();
  if(!watchLoaded && data.length){watchLoaded=true; loadWatch();}
  else { refreshStocks(); }
}
$("#bInd").onclick=()=>{kind="industry";setTab();stockCache={};load()};
$("#bCon").onclick=()=>{kind="concept";setTab();stockCache={};load()};
function setTab(){$("#bInd").classList.toggle("on",kind==="industry");$("#bCon").classList.toggle("on",kind==="concept")}
$("#bRefresh").onclick=load;
$("#bHelp").onclick=()=>{const h=$("#help");h.style.display=h.style.display==="none"?"block":"none";};
$("#selclear").onclick=()=>{selected=[];renderGrid();renderSel();saveWatch()};
$("#addStock").onclick=addStock;
$("#codeIn").addEventListener("input",()=>{
  clearTimeout(sugTimer);
  const q=$("#codeIn").value.trim();
  if(!q){hideSug();return;}
  sugTimer=setTimeout(async()=>{
    try{ const r=await fetch("/api/search?q="+encodeURIComponent(q)); const j=await r.json();
      sugData=j.results||[]; renderSug();
    }catch(e){hideSug();}
  },200);
});
$("#codeIn").addEventListener("keydown",e=>{
  if(e.key==="Enter"){hideSug();addStock();}
  else if(e.key==="Escape"){hideSug();}
});
document.addEventListener("click",e=>{if(!e.target.closest(".sbox"))hideSug();});
$("#bAuto").onclick=()=>{auto=!auto;$("#bAuto").classList.toggle("on",auto);setup()};
function setup(){if(timer)clearInterval(timer); if(auto)timer=setInterval(load,10000)}
load(); setup(); renderStocks();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        # 浏览器提前断开(刷新/切换)会抛 BrokenPipe，属正常，静默处理
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/sectors":
            q = parse_qs(u.query)
            kind = q.get("kind", ["industry"])[0] or "industry"
            if kind not in SECTOR_FS:
                kind = "industry"
            try:
                rows = fetch_sectors(kind, 50)
            except Exception:
                rows = []
            try:
                market = fetch_market_total()
            except Exception:
                market = None
            self._send(json.dumps({"time": datetime.now().strftime("%H:%M:%S"),
                                   "kind": kind, "rows": rows, "market": market},
                                  ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/stock":
            q = parse_qs(u.query)
            code = (q.get("code", [""])[0] or "").strip()
            try:
                data = fetch_stock(code)
            except Exception as e:
                data = {"error": str(e)[:60]}
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/stocks":
            q = parse_qs(u.query)
            board = (q.get("board", [""])[0] or "").strip()
            data = {"inflow": [], "outflow": []}
            if board:
                try:
                    data = fetch_board_stocks(board, 10)
                except Exception:
                    pass
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/market_curve":
            try:
                curve = fetch_market_curve()
            except Exception:
                curve = []
            self._send(json.dumps({"curve": curve}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/beh":
            q = parse_qs(u.query)
            raw = [s for s in (q.get("secids", [""])[0] or "").split(",") if s]
            items = []
            for s in raw:
                sid, nm = (s.split("|", 1) + [""])[:2] if "|" in s else (s, "")
                items.append((sid, nm))
            try:
                data = behaviors(items)
            except Exception:
                data = {}
            self._send(json.dumps({"beh": data}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/history":
            q = parse_qs(u.query)
            h_type = (q.get("type", ["industry"])[0] or "industry").strip()
            key = (q.get("key", [""])[0] or "").strip()
            try:
                w = fetch_history(h_type, key) if key else []
            except Exception:
                w = []
            self._send(json.dumps({"w": w}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/search":
            q = parse_qs(u.query)
            kw = (q.get("q", [""])[0] or "").strip()
            try:
                res = search_stocks(kw, 10)
            except Exception:
                res = []
            self._send(json.dumps({"results": res}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path == "/api/watch":
            self._send(json.dumps(load_watch(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif u.path in ("/", "/index.html"):
            self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/watch":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n) if n else b"{}"
                d = json.loads(body.decode("utf-8") or "{}")
                save_watch(d)
                self._send(b'{"ok":true}', "application/json")
            except Exception as e:
                self._send(json.dumps({"ok": False, "error": str(e)[:80]}).encode(),
                           "application/json", 400)
        else:
            self._send(b"not found", "text/plain", 404)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionResetError):
            return  # 浏览器正常断开，忽略
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description="板块资金流向 UI（方框热力图）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    ensure_repurchase()  # 后台预热回购数据
    srv = _Server((args.host, args.port), Handler)
    print(f"资金流向 UI 已启动: http://{args.host}:{args.port}   (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
