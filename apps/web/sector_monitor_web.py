#!/usr/bin/env python3
"""
A股 / 美股 板块划线看盘（Web 版）
=================================
把 apps/cli/soxx_monitor.py 的「多周期 + 手动画线」搬到浏览器，标的从单只 ETF
换成**板块**：

  · A股：东财行业板块 / 概念板块（按成交额排序，带量比、主力净额）
  · 美股：SPDR 11 大行业 + 半导体/科技 + 主题 ETF（按成交额排序）
  · 周期：分时 / 5分 / 15分 / 30分 / 60分 / 日 / 周 / 月
  · 均线：MA5 / MA17 / MA21 / MA45
  · 画线：水平线 / 趋势线；选中后可拖线体、拖端点；
          价格在线上方=支撑(绿)，下方=压力(红)；Delete 删除；Ctrl+Z/Y 撤销重做
  · 画线按 市场:标的|周期 保存到 data/sector_lines.json，与 Qt 版 schema 一致
    （{"type":"h","y":..} / {"type":"trend","x1":..,"y1":..,"x2":..,"y2":..}）

数据来源
  A股板块列表：东方财富 clist（多主机轮换 + 重试）→ 同花顺行业表兜底 → 本地缓存
  A股分时：    东财 kline(1/5/15/30/60) → trends2 兜底 → 本地缓存
  A股日线：    东财 kline(101/102/103) → 同花顺指数 → 东财资金流日线(仅收盘价) → 本地缓存
  A股分时：    东财 kline/trends → 同花顺板块分时（5/15/30/60 分，1 分实时合成） → 本地缓存
  美股列表：   新浪 gb_ 实时行情（批量）
  美股分时：   新浪 US_MinKService.getMinK
  美股日线：   新浪 US_MinKService.getDailyK → Nasdaq（core/us_data）

用法:
    python apps/web/sector_monitor_web.py                 # http://127.0.0.1:8771
    python apps/web/sector_monitor_web.py --port 8888
    python apps/web/sector_monitor_web.py --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RUN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(RUN_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")
DB_FILE = os.path.join(DATA_DIR, "sector_monitor.db")
LINES_FILE = os.path.join(DATA_DIR, "sector_lines.json")
WATCH_FILE = os.path.join(DATA_DIR, "watch.json")
UI_FILE = os.path.join(RUN_DIR, "sector_monitor_web.html")
GAME_PORT = 8123
OFFLINE = os.environ.get("SECTOR_OFFLINE", "").strip().lower() in ("1", "true", "yes")
GAME_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>回测游戏</title>
<style>html,body{margin:0;height:100%;background:#0e1116;overflow:hidden}iframe{width:100%;height:100%;border:0}</style>
</head><body><iframe src="http://__HOST__:8123/"></iframe></body></html>"""


def _ensure_game_server(port: int = GAME_PORT) -> None:
    """确保回测游戏服务在运行（独立进程，供 /game iframe 使用）。"""
    import socket
    import subprocess

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
    except Exception:
        pass
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(RUN_DIR, "backtest_game.py"), "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


sys.path.insert(0, ROOT_DIR)

try:
    import requests
except ImportError:  # pragma: no cover
    print("需要 requests：pip install requests", file=sys.stderr)
    raise

import pandas as pd

# ---------------- 常量 ----------------
UP = "#e03c3c"  # 涨 / 压力（与 Qt 版一致）
DOWN = "#1f9e8f"  # 跌 / 支撑

EM_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
EM_HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2his.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://83.push2.eastmoney.com",
]

SINA_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

QQ_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

# 常用宽基指数（搜索时总是列出来，不依赖外部接口）
CN_INDEX_LIST = [
    ("SH000001", "上证指数"),
    ("SZ399001", "深证成指"),
    ("SZ399006", "创业板指"),
    ("SH000300", "沪深300"),
    ("SH000905", "中证500"),
    ("SH000852", "中证1000"),
    ("SH000016", "上证50"),
    ("SH000688", "科创50"),
    ("SZ399005", "中小100"),
    ("SZ399101", "中小综指"),
]

SECTOR_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3"}
SECTOR_FIELDS = "f2,f3,f6,f8,f10,f12,f14,f62,f104,f105,f128"
MAX_SECTORS = 400
MAX_BARS = 3600  # 日线最多保留约 15 年

# 美股板块宇宙（ETF）。分组只影响前端展示，可自行增删。
# (代码, 中文备注)  —— 实时名称以新浪返回为准
US_GROUPS = [
    (
        "broad",
        "宽基行业",
        [
            ("XLK", "科技"),
            ("XLF", "金融"),
            ("XLV", "医疗"),
            ("XLY", "可选消费"),
            ("XLP", "必需消费"),
            ("XLE", "能源"),
            ("XLI", "工业"),
            ("XLB", "材料"),
            ("XLU", "公用事业"),
            ("XLRE", "房地产"),
            ("XLC", "通信服务"),
        ],
    ),
    (
        "semi",
        "半导体·科技",
        [
            ("SOXX", "半导体"),
            ("SMH", "半导体"),
            ("XSD", "半导体等权"),
            ("PSI", "半导体"),
            ("IGV", "软件"),
            ("VGT", "科技"),
            ("QQQ", "纳指100"),
            ("MAGS", "七巨头"),
        ],
    ),
    (
        "theme",
        "主题·其他",
        [
            ("XBI", "生物科技"),
            ("ITB", "住宅建筑"),
            ("KRE", "区域银行"),
            ("XOP", "油气开采"),
            ("TAN", "太阳能"),
            ("ICLN", "清洁能源"),
            ("GDX", "黄金矿业"),
            ("GLD", "黄金"),
            ("TLT", "长债"),
            ("SPY", "标普500"),
            ("IWM", "罗素2000"),
        ],
    ),
]
US_ALL = [(code, name) for _, _, items in US_GROUPS for code, name in items]

US_INDEX_CODES = ["SPY", "QQQ", "SOXX"]
CN_INDEX_SECIDS = "1.000001,0.399001,0.399006"

TF_RULES = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
    "1d": None,
    "1w": "W-FRI",
    "1M": "ME",
}
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_MEM: dict[str, dict] = {}
_MEM_LOCK = threading.Lock()
_LINES_LOCK = threading.Lock()


# ---------------- SQLite 持久层 ----------------
# K线是「只追加、反复读」的数据，落 SQLite 最划算；行情/列表是易失数据，
# 只做「快照」保存（用于秒开 + 断网兜底），不做历史表。
_DB: sqlite3.Connection | None = None
_DB_LOCK = threading.RLock()

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="smw")
_HOLD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hold")
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()
_BARS_LOCKS: dict[str, threading.Lock] = {}
_BARS_LOCKS_GUARD = threading.Lock()
_ACTIVE_LISTS: dict[str, float] = {}
_ACTIVE_BARS: dict[str, float] = {}
_BARS_ARGS: dict[str, tuple[str, str]] = {}


def db() -> sqlite3.Connection:
    global _DB
    if _DB is not None:
        return _DB
    with _DB_LOCK:
        if _DB is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=20)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bars(
                    market TEXT NOT NULL, code TEXT NOT NULL, tf TEXT NOT NULL,
                    ts TEXT NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,
                    PRIMARY KEY(market, code, tf, ts)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, ts REAL NOT NULL, src TEXT
                );
                CREATE TABLE IF NOT EXISTS snapshots(
                    key TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lines(
                    key TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watch(
                    id TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cache(
                    key TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL
                );
                """
            )
            conn.commit()
            _DB = conn
    return _DB


def db_bars_get(market: str, code: str, tf: str, limit: int = 5000) -> list[dict]:
    try:
        with _DB_LOCK:
            cur = db().execute(
                "SELECT ts,o,h,l,c,v FROM bars WHERE market=? AND code=? AND tf=? "
                "ORDER BY ts DESC LIMIT ?",
                (market, code, tf, limit),
            )
            rows = cur.fetchall()
        return [
            {"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in reversed(rows)
        ]
    except Exception:
        return []


def db_bars_put(market: str, code: str, tf: str, bars: list[dict], src: str) -> None:
    if not bars:
        return
    try:
        with _DB_LOCK:
            conn = db()
            conn.executemany(
                "INSERT OR REPLACE INTO bars(market,code,tf,ts,o,h,l,c,v) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(market, code, tf, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars],
            )
            if tf == "1m":
                # 分时只保留最后一个交易日：写新数据时把更早的分钟线删掉
                last_day = max(str(b["t"])[:10] for b in bars)
                conn.execute(
                    "DELETE FROM bars WHERE market=? AND code=? AND tf='1m' AND substr(ts,1,10) < ?",
                    (market, code, last_day),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,ts,src) VALUES(?,?,?)",
                (f"bars:{market}:{code}:{tf}", time.time(), src or ""),
            )
            conn.commit()
    except Exception:
        pass


def db_bars_replace(market: str, code: str, tf: str, bars: list[dict], src: str) -> None:
    """全量刷新：先清掉该代码旧数据，避免旧数据源残留造成跳变。"""
    if not bars:
        return
    try:
        with _DB_LOCK:
            conn = db()
            conn.execute("DELETE FROM bars WHERE market=? AND code=? AND tf=?", (market, code, tf))
            conn.executemany(
                "INSERT OR REPLACE INTO bars(market,code,tf,ts,o,h,l,c,v) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(market, code, tf, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars],
            )
            if tf == "1m":
                # 分时只保留最后一个交易日：写新数据时把更早的分钟线删掉
                last_day = max(str(b["t"])[:10] for b in bars)
                conn.execute(
                    "DELETE FROM bars WHERE market=? AND code=? AND tf='1m' AND substr(ts,1,10) < ?",
                    (market, code, last_day),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,ts,src) VALUES(?,?,?)",
                (f"bars:{market}:{code}:{tf}", time.time(), src or ""),
            )
            conn.commit()
    except Exception:
        pass


def db_meta_get(key: str):
    try:
        with _DB_LOCK:
            row = db().execute("SELECT ts,src FROM meta WHERE key=?", (key,)).fetchone()
        return {"ts": row[0], "src": row[1]} if row else None
    except Exception:
        return None


def db_snapshot_get(key: str):
    try:
        with _DB_LOCK:
            row = db().execute("SELECT ts,payload FROM snapshots WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"ts": row[0], "payload": json.loads(row[1])}
    except Exception:
        return None


def db_snapshot_put(key: str, payload: dict) -> None:
    try:
        with _DB_LOCK:
            conn = db()
            conn.execute(
                "INSERT OR REPLACE INTO snapshots(key,ts,payload) VALUES(?,?,?)",
                (key, time.time(), json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()
    except Exception:
        pass


def db_prune() -> None:
    """分时数据只保留最近 25 天，避免库无限增长。"""
    cut = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
    try:
        with _DB_LOCK:
            conn = db()
            conn.execute("DELETE FROM bars WHERE tf LIKE '%m' AND ts < ?", (cut,))
            conn.execute("DELETE FROM cache WHERE ts < ?", (time.time() - 7 * 86400,))
            conn.commit()
    except Exception:
        pass


def _submit_once(key: str, fn):
    """同一 key 同时只跑一个抓取（single-flight），返回 Future 或 None。"""
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return None
        _INFLIGHT.add(key)

    def _run():
        try:
            fn()
        except Exception:
            pass
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)

    return _POOL.submit(_run)


def _spawn_once(key: str, fn) -> bool:
    return _submit_once(key, fn) is not None


def _in_session(market: str) -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    if market == "cn":
        return 915 <= hm <= 1135 or 1255 <= hm <= 1505
    return hm >= 2100 or hm <= 605


def _list_ttl(market: str) -> float:
    return 5.0 if _in_session(market) else 30.0


def _bar_ttl(tf: str) -> float:
    """缓存时间：分时（1m）盘中要跟得紧，其他分钟 15s，日线 5 分钟。"""
    if tf == "1m":
        return 5.0
    return 15.0 if tf.endswith("m") else 300.0


def _refresh_tail(tf: str) -> int:
    """过期刷新只补最近 N 根；历史已在库里，不用全量重拉。"""
    return 5 if tf == "1d" else 30


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    idx = pd.to_datetime([b["t"] for b in bars])
    return pd.DataFrame(
        {
            "open": [b["o"] for b in bars],
            "high": [b["h"] for b in bars],
            "low": [b["l"] for b in bars],
            "close": [b["c"] for b in bars],
            "volume": [b["v"] for b in bars],
        },
        index=idx,
    )


def _resample_rows(bars: list[dict], tf: str) -> list[dict]:
    if not bars:
        return []
    try:
        df = _resample_tf(_bars_to_df(bars), tf)
        return _rows(df, False)
    except Exception:
        return []


# ---------------- 小工具 ----------------
def _num(v, default: float | None = 0.0):
    try:
        if v in (None, "", "-", "--"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _items(js) -> list:
    if not js:
        return []
    diff = (js.get("data") or {}).get("diff")
    if diff is None:
        return []
    return list(diff.values()) if isinstance(diff, dict) else diff


def _mem_get(key: str, ttl: float):
    with _MEM_LOCK:
        node = _MEM.get(key)
    if node and time.time() - node["ts"] < ttl:
        return node["v"]
    return None


def _mem_set(key: str, v):
    with _MEM_LOCK:
        _MEM[key] = {"ts": time.time(), "v": v}


# ---------------- 东财请求（多主机 + 重试） ----------------
_EM_HIST_DOWN_UNTIL = 0.0  # 东财历史接口(push2his)被限流/不可达时熔断 60s


def em_hist_get(path: str, retries: int = 1, timeout: float = 2.5):
    """K线/分时/资金流共用：失败后 60s 内直接跳过，避免每次点板块都等超时。"""
    global _EM_HIST_DOWN_UNTIL
    if time.time() < _EM_HIST_DOWN_UNTIL:
        return None
    js = em_get(path, retries=retries, timeout=timeout)
    _EM_HIST_DOWN_UNTIL = 0.0 if js is not None else time.time() + 60
    return js


def em_get(path: str, retries: int = 3, timeout: float = 5):
    for i in range(retries):
        host = EM_HOSTS[i % len(EM_HOSTS)]
        try:
            r = requests.get(host + path, headers=EM_H, timeout=timeout)
            if r.status_code == 200 and r.text.lstrip().startswith("{"):
                js = r.json()
                if js.get("data") is not None:
                    return js
        except Exception:
            pass
        time.sleep(0.35)
    return None


# ---------------- A股板块列表 ----------------
THS_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://q.10jqka.com.cn/",
}
# 同花顺板块指数分时：period 31=5分 41=15分 51=30分 61=1分
THS_YEAR_URL = "https://d.10jqka.com.cn/v4/line/bk_{code}/{period}/{year}.js"
THS_LAST_URL = "https://d.10jqka.com.cn/v4/line/bk_{code}/{period}/last.js"
THS_TIME_URL = "https://d.10jqka.com.cn/v6/time/bk_{code}/last.js"
_THS_V: dict[str, float] = {"ts": 0.0}
_THS_V["v"] = ""


def _ths_headers() -> dict:
    """同花顺要求 v=... 的 cookie，否则 502 概率很高；6 小时算一次就够了。"""
    now = time.time()
    if not _THS_V.get("v") or now - float(_THS_V.get("ts") or 0) > 6 * 3600:
        try:
            import py_mini_racer
            from akshare.datasets import get_ths_js

            js = py_mini_racer.MiniRacer()
            with open(get_ths_js("ths.js"), encoding="utf-8") as f:
                js.eval(f.read())
            _THS_V["v"] = js.call("v")
        except Exception:
            _THS_V["v"] = ""
        _THS_V["ts"] = now
    h = dict(THS_H)
    if _THS_V.get("v"):
        h["Cookie"] = "v=" + str(_THS_V["v"])
    return h


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&nbsp;", " ").strip()


_THS_LIST_PAGE = "https://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{p}/ajax/1/"
_THS_LIST_MAX_PAGE = 8


def fetch_cn_sectors_ths() -> list[dict]:
    """同花顺行业一览（逐页翻到底，通常 90 个）。东财不可用时兜底。"""
    rows, seen = [], set()
    for p in range(1, _THS_LIST_MAX_PAGE + 1):
        url = "https://q.10jqka.com.cn/thshy/" if p == 1 else _THS_LIST_PAGE.format(p=p)
        try:
            r = requests.get(url, headers=_ths_headers(), timeout=12)
            r.encoding = "gbk"
            html = r.text
        except Exception:
            break
        new = 0
        for row in _parse_ths_industry(html):
            if row["code"] in seen:
                continue
            seen.add(row["code"])
            rows.append(row)
            new += 1
        if not new:
            break
    return rows


# ---------------- 腾讯板块行情（东财不可用时的兜底，概念板块尤其需要） ----------------
_QQ_RANK = (
    "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
    "?board_type={bt}&sort_type=price&direct=down&offset={off}&count=200"
)
_QQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}


def _ths_concept_code_map() -> dict[str, str]:
    """同花顺概念名 → 代码（881xxx 之外的 3xxxxx），用来把腾讯的概念对齐到同花顺口径。"""
    hit = _mem_get("ths_concept_codes", 6 * 3600)
    if hit is not None:
        return hit
    out: dict[str, str] = {}
    try:
        import contextlib
        import io

        import akshare as ak

        with contextlib.redirect_stderr(io.StringIO()):
            df = ak.stock_board_concept_name_ths()
        for _, r in df.iterrows():
            code = str(r.get("code") or "").strip()
            name = str(r.get("name") or "").strip()
            if code and name:
                out[_norm_board_name(name)] = "T" + code
    except Exception:
        out = {}
    if out:
        _mem_set("ths_concept_codes", out)
    return out


def fetch_cn_sectors_qq(kind: str = "concept") -> list[dict]:
    """腾讯板块行情（概念 805 个 / 行业 31 个），带涨跌幅、成交额、量比、领涨股。"""
    cache_key = f"qq_sectors:{kind}"
    hit = _mem_get(cache_key, 120)
    if hit is not None:
        return hit
    bt = "gn" if kind == "concept" else "hy"
    code_map = _ths_concept_code_map() if kind == "concept" else {}
    rows, seen = [], set()
    for off in range(0, 2000, 200):
        try:
            r = requests.get(
                _QQ_RANK.format(bt=bt, off=off), headers=_QQ_HEADERS, timeout=15
            )
            data = (r.json() or {}).get("data") or {}
        except Exception:
            break
        items = data.get("rank_list") or []
        for it in items:
            name = str(it.get("name") or "").strip()
            qcode = str(it.get("code") or "").strip()
            if not name or not qcode:
                continue
            code = code_map.get(_norm_board_name(name)) or qcode
            if code in seen:
                continue
            seen.add(code)
            lzg = it.get("lzg") or {}
            rows.append(
                {
                    "market": "cn",
                    "code": code,
                    "name": name,
                    "price": _num(it.get("zxj"), 0.0),
                    "pct": _num(it.get("zdf"), 0.0),
                    # 腾讯 turnover 单位是万元
                    "amount": (_num(it.get("turnover"), 0.0) or 0.0) * 1e4,
                    "turnover": _num(it.get("hsl"), None),
                    "vr": _num(it.get("lb"), None),
                    "main": None,
                    "up": None,
                    "down": None,
                    "leader": str(lzg.get("name") or ""),
                    "group": kind,
                }
            )
        if len(items) < 200:
            break
    if rows:
        _mem_set(cache_key, rows)
    return rows


def _parse_ths_industry(html: str) -> list[dict]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 8:
            continue
        m = re.search(r"detail/code/(\d+)/[^>]*>([^<]+)<", tds[1])
        if not m:
            continue
        code, name = m.group(1), _strip_tags(m.group(2))
        pct = _num(_strip_tags(tds[2]), 0.0)
        amount = (_num(_strip_tags(tds[4]), 0.0) or 0.0) * 1e8
        main = (_num(_strip_tags(tds[5]), 0.0) or 0.0) * 1e8
        rows.append(
            {
                "market": "cn",
                "code": "T" + code,
                "name": name,
                "price": 0.0,
                "pct": pct,
                "amount": amount,
                "turnover": None,
                "vr": None,
                "main": main,
                "up": int(_num(_strip_tags(tds[6]), 0) or 0),
                "down": int(_num(_strip_tags(tds[7]), 0) or 0),
                "leader": _strip_tags(tds[9]) if len(tds) > 9 else "",
                "group": "industry",
            }
        )
    return rows


def fetch_cn_sectors(kind: str = "industry") -> tuple[list[dict], str]:
    cache_key = f"cn_sectors:{kind}"
    hit = _mem_get(cache_key, 5)
    if hit is not None:
        return hit
    fs = SECTOR_FS.get(kind, SECTOR_FS["industry"])
    url = (
        f"/api/qt/clist/get?pn=1&pz={MAX_SECTORS}&po=1&np=1&fltt=2&invt=2&fid=f6"
        f"&fs={fs}&fields={SECTOR_FIELDS}"
    )
    rows = []
    for it in _items(em_get(url)):
        code = str(it.get("f12") or "")
        if not code:
            continue
        rows.append(
            {
                "market": "cn",
                "code": code,
                "name": it.get("f14") or code,
                "price": _num(it.get("f2")),
                "pct": _num(it.get("f3")),
                "amount": _num(it.get("f6")),
                "turnover": _num(it.get("f8")),
                "vr": _num(it.get("f10"), None),
                "main": _num(it.get("f62"), None),
                "up": int(_num(it.get("f104"), 0) or 0),
                "down": int(_num(it.get("f105"), 0) or 0),
                "leader": it.get("f128") or "",
                "group": kind,
            }
        )
    src = "东方财富"
    if not rows and kind == "industry":
        rows = fetch_cn_sectors_ths()
        src = "同花顺"
    if not rows:
        # 东财挂掉且同花顺没有该类型（概念）时，用腾讯板块行情兜底
        rows = fetch_cn_sectors_qq(kind)
        src = "腾讯" + ("概念" if kind == "concept" else "行业")
    if rows:
        _mem_set(cache_key, (rows, src))
    return rows, src


def fetch_cn_indices() -> list[dict]:
    hit = _mem_get("cn_indices", 10)
    if hit is not None:
        return hit
    url = f"/api/qt/ulist.np/get?fltt=2&invt=2&secids={CN_INDEX_SECIDS}&fields=f2,f3,f6,f12,f14"
    out = []
    for it in _items(em_get(url)):
        raw = str(it.get("f12") or "").strip()
        code = ""
        if raw:
            raw = raw.zfill(6)
            code = ("SH" if raw.startswith("000") else "SZ") + raw
        out.append(
            {
                "code": code,
                "name": it.get("f14") or "",
                "price": _num(it.get("f2")),
                "pct": _num(it.get("f3")),
                "amount": _num(it.get("f6")),
            }
        )
    if not out:
        out = _sina_cn_indices()
    if out:
        _mem_set("cn_indices", out)
    return out


def _sina_cn_indices() -> list[dict]:
    """东财不通时的指数兜底（新浪）。amount 单位：万元 → 元。"""
    try:
        r = requests.get(
            "https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006", headers=SINA_H, timeout=8
        )
        r.encoding = "gbk"
        out = []
        for line in r.text.splitlines():
            m = re.match(r'var hq_str_s_(\w+)="(.*)";', line.strip())
            if not m:
                continue
            f = m.group(2).split(",")
            if len(f) < 6:
                continue
            sym = m.group(1)
            out.append(
                {
                    "code": _idx_code_norm(sym),
                    "name": f[0],
                    "price": _num(f[1]),
                    "pct": _num(f[3]),
                    "amount": (_num(f[5]) or 0) * 1e4,
                }
            )
        return out
    except Exception:
        return []


# ---------------- 美股行情（新浪批量） ----------------
def fetch_us_quotes(codes: list[str]) -> dict[str, dict]:
    cache_key = "us_quotes:" + ",".join(sorted(codes))
    hit = _mem_get(cache_key, 5)
    if hit is not None:
        return hit
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 20):
        chunk = codes[i : i + 20]
        q = ",".join("gb_" + c.lower() for c in chunk)
        try:
            r = requests.get("https://hq.sinajs.cn/list=" + q, headers=SINA_H, timeout=10)
            r.encoding = "gbk"
            for line in r.text.splitlines():
                m = re.match(r'var hq_str_gb_([A-Za-z0-9._-]+)="(.*)";', line.strip())
                if not m:
                    continue
                code, body = m.group(1).upper(), m.group(2)
                f = body.split(",")
                if len(f) < 11 or not f[0]:
                    continue
                price = _num(f[1], 0.0) or 0.0
                pct = _num(f[2], 0.0) or 0.0
                vol = _num(f[10], 0.0) or 0.0
                avg_vol = _num(f[11], 0.0) or 0.0
                vr = (
                    round(vol / avg_vol, 2)
                    if avg_vol > 0 and vol > 0 and 0.05 < vol / avg_vol < 40
                    else None
                )
                out[code] = {
                    "market": "us",
                    "code": code,
                    "name": f[0],
                    "price": price,
                    "pct": pct,
                    "amount": price * vol,
                    "volume": vol,
                    "vr": vr,
                    "turnover": None,
                    "main": None,
                    "up": 0,
                    "down": 0,
                    "leader": "",
                    "group": "",
                    "ts": f[3] if len(f) > 3 else "",
                }
        except Exception:
            continue
    if out:
        _mem_set(cache_key, out)
    return out


def fetch_us_sectors() -> tuple[list[dict], list[dict]]:
    codes = [c for c, _ in US_ALL] + [c for c in US_INDEX_CODES if c not in dict(US_ALL)]
    quotes = fetch_us_quotes(codes)
    rows = []
    for group, _gname, items in US_GROUPS:
        for code, hint in items:
            q = quotes.get(code)
            if not q:
                continue
            row = dict(q)
            row["group"] = group
            row["hint"] = hint
            if not row.get("name"):
                row["name"] = hint
            rows.append(row)
    indices = [quotes[c] for c in US_INDEX_CODES if c in quotes]
    return rows, indices


def _sector_fetch(market: str, kind: str) -> dict:
    """真正去拉一次列表/行情，并写入快照。失败时返回旧快照或空表。"""
    if market == "us":
        rows, indices = fetch_us_sectors()
        groups = [{"id": g, "name": n} for g, n, _ in US_GROUPS]
        src = "新浪美股"
    else:
        rows, src = fetch_cn_sectors(kind)
        indices = fetch_cn_indices()
        groups = [{"id": "industry", "name": "行业"}, {"id": "concept", "name": "概念"}]
        src = src or "东方财富"
    key = f"list:{market}:{kind}"
    if not rows:
        prev = db_snapshot_get(key)
        if prev and prev["payload"].get("rows"):
            return prev["payload"]
        return {
            "time": datetime.now().strftime("%H:%M:%S"),
            "market": market,
            "kind": kind,
            "rows": [],
            "indices": indices,
            "groups": groups,
            "source": src,
            "stale": True,
            "age": 0,
        }
    rows = sorted(rows, key=lambda r: -(r.get("amount") or 0))
    payload = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "market": market,
        "kind": kind,
        "rows": rows,
        "indices": indices,
        "groups": groups,
        "source": src,
    }
    db_snapshot_put(key, payload)
    return payload


def sector_payload(market: str, kind: str) -> dict:
    """SWR：永远先返回库里的快照（毫秒级），过期就丢后台刷新。"""
    key = f"list:{market}:{kind}"
    _ACTIVE_LISTS[key] = time.time()
    snap = db_snapshot_get(key)
    now = time.time()
    if snap:
        payload = dict(snap["payload"])
        age = now - snap["ts"]
        fresh = age < _list_ttl(market)
        payload["stale"] = not fresh
        payload["age"] = round(max(age, 0), 1)
        payload["stale_ts"] = 0 if fresh else snap["ts"]
        if not fresh:
            _spawn_once(key, lambda m=market, k=kind: _sector_fetch(m, k))
        return payload
    payload = _sector_fetch(market, kind)
    payload["stale"] = False
    payload["age"] = 0
    payload["stale_ts"] = 0
    return payload


# ---------------- MA21 斜率筛选 ----------------
def _ma21_from_closes(closes: list[float]):
    if not closes or len(closes) < 27:
        return None
    ma = [sum(closes[i - 20 : i + 1]) / 21.0 for i in range(20, len(closes))]
    if len(ma) < 6:
        return None
    slope = (ma[-1] - ma[-6]) / 5.0 / (ma[-1] or 1.0) * 100.0
    return {"slope": round(slope, 4), "ma21": round(ma[-1], 3), "last": round(closes[-1], 3)}


def _ma21_slope_cn(name: str, kind: str, code: str = ""):
    bars = []
    if code:
        try:
            bars = db_bars_get("cn", code, "1d")
        except Exception:
            bars = []
    if not bars or len(bars) < 27:
        try:
            df, used = _ths_daily_cached(name, kind, False)
        except Exception:
            df = None
        if df is not None and not df.empty:
            try:
                bars = [
                    {
                        "t": str(idx)[:10],
                        "o": float(r["open"]),
                        "h": float(r["high"]),
                        "l": float(r["low"]),
                        "c": float(r["close"]),
                        "v": float(r["volume"]),
                    }
                    for idx, r in df.iterrows()
                ]
            except Exception:
                bars = []
            if code and bars:
                db_bars_put("cn", code, "1d", bars, "同花顺")
    if not bars:
        return None
    return _ma21_from_closes([float(b["c"]) for b in bars])


def _ma21_slope_us(code: str):
    try:
        bars, src = us_bars(code, "1d")
    except Exception:
        bars = []
    if not bars:
        return None
    return _ma21_from_closes([float(b["c"]) for b in bars])


# 后台渐进扫描 MA21：同花顺并发敏感（信号量 2），不能一次性阻塞算 80 个
_MA_RESULTS: dict[tuple, dict] = {}
_MA_QUEUED: set = set()
_MA_FAIL: dict[tuple, float] = {}
_MA_LOCK = threading.Lock()
_MA_QUEUE: "queue.Queue" = queue.Queue()


def _ma_worker():
    while True:
        market, kind, code, name = _MA_QUEUE.get()
        try:
            v = _ma21_slope_us(code) if market == "us" else _ma21_slope_cn(name, kind, code)
            if v:
                node = {"ts": time.time(), **v}
                with _MA_LOCK:
                    _MA_RESULTS[(market, code)] = node
                cache_put(f"ma21:{market}:{code}", node)
            else:
                with _MA_LOCK:
                    _MA_FAIL[(market, code)] = time.time() + 600
        except Exception:
            pass
        finally:
            with _MA_LOCK:
                _MA_QUEUED.discard((market, code))
            _MA_QUEUE.task_done()
            time.sleep(1.0)  # 限速，避免同花顺历史接口被封


for _i in range(1):
    threading.Thread(target=_ma_worker, daemon=True, name=f"ma-worker-{_i}").start()


def _ma_enqueue(market: str, kind: str, code: str, name: str) -> bool:
    with _MA_LOCK:
        if (market, code) in _MA_QUEUED:
            return False
        _MA_QUEUED.add((market, code))
    _MA_QUEUE.put((market, kind, code, name))
    return True


def sector_ma_payload(
    market: str = "cn", kind: str = "industry", limit: int = 30, top: int = 80
) -> dict:
    """按 MA21 斜率筛选：先返回已算好的，缺的后台排队，前端轮询补齐。"""
    key = f"ma21:{market}:{kind}:{limit}"
    hit = _mem_get(key, 3)
    if hit is not None:
        return hit
    base = sector_payload(market, kind)
    rows = sorted(base.get("rows") or [], key=lambda r: -(r.get("amount") or 0))[: max(top, limit)]
    now = time.time()
    results = []
    pending = 0
    for r in rows:
        code = r.get("code", "")
        node = _MA_RESULTS.get((market, code))
        if node is None:
            node = cache_get(f"ma21:{market}:{code}", 1800)
            if node:
                _MA_RESULTS[(market, code)] = node
        if node and now - node.get("ts", 0) < 1800:
            results.append(
                {**r, "slope": node["slope"], "ma21": node["ma21"], "last": node["last"]}
            )
            continue
        # 先用 DB 已有日线秒算
        if market == "cn":
            try:
                bars = db_bars_get("cn", code, "1d")
            except Exception:
                bars = []
            if bars and len(bars) >= 27:
                v = _ma21_from_closes([float(b["c"]) for b in bars])
                if v:
                    node = {"ts": time.time(), **v}
                    _MA_RESULTS[(market, code)] = node
                    cache_put(f"ma21:{market}:{code}", node)
                    results.append({**r, **v})
                    continue
        if _MA_FAIL.get((market, code), 0) > now:
            continue
        pending += 1
        _ma_enqueue(market, kind, code, r.get("name", ""))
    up = sorted([r for r in results if r.get("slope", 0) > 0], key=lambda x: -x["slope"])[:limit]
    down = sorted([r for r in results if r.get("slope", 0) < 0], key=lambda x: x["slope"])[:limit]
    payload = {
        "market": market,
        "kind": kind,
        "up": up,
        "down": down,
        "scanned": len(results),
        "pending": pending,
        "need": len(rows),
        "time": datetime.now().strftime("%H:%M:%S"),
        "source": base.get("source"),
    }
    _mem_set(key, payload)
    return payload


# ---------------- K 线：A股 ----------------
def _rows(df: pd.DataFrame, intraday: bool) -> list[dict]:
    out = []
    fmt = "%Y-%m-%d %H:%M:%S" if intraday else "%Y-%m-%d"
    for idx, r in df.iterrows():
        try:
            out.append(
                {
                    "t": pd.Timestamp(idx).strftime(fmt),
                    "o": round(float(r["open"]), 4),
                    "h": round(float(r["high"]), 4),
                    "l": round(float(r["low"]), 4),
                    "c": round(float(r["close"]), 4),
                    "v": round(float(r["volume"]), 2),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


def _resample_tf(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """日线 → 周/月线。兼容 pandas 2.0 的 "M" 与 2.2+ 的 "ME"。"""
    rule = TF_RULES.get(tf)
    if not rule:
        return df
    try:
        return df.resample(rule).agg(AGG).dropna()
    except ValueError:
        return df.resample(rule.replace("ME", "M")).agg(AGG).dropna()


def _resample_intraday(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """按自然日分组重采样，避免 11:30→13:00 被当成连续区间。"""
    if minutes <= 1 or df.empty:
        return df
    parts = []
    for _, g in df.groupby(df.index.date):
        rs = g.resample(f"{minutes}min").agg(AGG).dropna()
        if not rs.empty:
            parts.append(rs)
    return pd.concat(parts) if parts else df.iloc[0:0]


def _em_kline(secid: str, klt: int, lmt: int = 800):
    url = (
        f"/api/qt/stock/kline/get?secid={secid}&klt={klt}&fqt=1&lmt={lmt}&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
    )
    js = em_hist_get(url)
    kl = ((js or {}).get("data") or {}).get("klines") or []
    rows = []
    for k in kl:
        p = str(k).split(",")
        if len(p) < 6:
            continue
        try:
            rows.append(
                {
                    "date": pd.to_datetime(p[0]),
                    "open": float(p[1]),
                    "close": float(p[2]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                    "volume": float(p[5]),
                }
            )
        except (ValueError, TypeError):
            continue
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _em_trends(secid: str, ndays: int = 5):
    url = (
        f"/api/qt/stock/trends2/get?secid={secid}&ndays={ndays}&iscr=0"
        "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
    )
    js = em_hist_get(url)
    tr = ((js or {}).get("data") or {}).get("trends") or []
    rows = []
    for k in tr:
        p = str(k).split(",")
        if len(p) < 6:
            continue
        try:
            rows.append(
                {
                    "date": pd.to_datetime(p[0]),
                    "open": float(p[1]),
                    "close": float(p[2]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                    "volume": float(p[5]),
                }
            )
        except (ValueError, TypeError):
            continue
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _em_flow_daily(secid: str, lmt: int = 900):
    """资金流日线：可拿到日期 + 板块指数收盘（无成交量，兜底用）。"""
    url = (
        "/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101&secid=%s"
        "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
        % (lmt, secid)
    )
    js = em_hist_get(url)
    kl = ((js or {}).get("data") or {}).get("klines") or []
    rows = []
    for k in kl:
        p = str(k).split(",")
        if len(p) < 12:
            continue
        try:
            c = float(p[11])
        except (ValueError, TypeError):
            continue
        if c <= 0:
            continue
        rows.append(
            {
                "date": pd.to_datetime(p[0]),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 0.0,
            }
        )
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


_THS_MEM: dict[tuple, tuple] = {}
_THS_FAIL: dict[tuple, float] = {}
_THS_ALIAS: dict[tuple, str] = {}
_THS_NAMES: dict[str, tuple[float, list[str]]] = {}
_THS_NAMES_FAIL: dict[str, float] = {}
_THS_SEM = threading.Semaphore(2)
_THS_FAIL_TTL = 40.0


def _norm_board_name(n: str) -> str:
    n = re.sub(r"[（(].*?[)）]", "", str(n or ""))
    n = re.sub(r"(Ⅱ|Ⅲ|Ⅰ|II|III|2|3)$", "", n)
    return n.replace(" ", "").strip()


def _ths_board_names(kind: str) -> list[str]:
    """同花顺板块名列表（用来把东财名映射过去），成功缓存 6h，失败只缓存 1 分钟。"""
    now = time.time()
    node = _THS_NAMES.get(kind)
    if node and now - node[0] < 6 * 3600:
        return node[1]
    if _THS_NAMES_FAIL.get(kind, 0) > now:
        return []
    names: list[str] = []
    try:
        import contextlib
        import io
        import akshare as ak

        with contextlib.redirect_stderr(io.StringIO()):
            df = (
                ak.stock_board_concept_name_ths()
                if kind == "concept"
                else ak.stock_board_industry_name_ths()
            )
        if df is not None and len(df):
            col = next(
                (c for c in ("name", "板块名称", "概念名称") if c in df.columns), df.columns[1]
            )
            names = [str(x).strip() for x in df[col].tolist() if str(x).strip()]
    except Exception:
        names = []
    if names:
        _THS_NAMES[kind] = (time.time(), names)
    else:
        _THS_NAMES_FAIL[kind] = now + 60
    return names


def _ths_candidates(name: str, kind: str) -> list[str]:
    """同花顺没有「通信」这类东财名：按同名 → 前缀 → 包含，找最接近的板块。"""
    key = (name, kind)
    if key in _THS_ALIAS:
        return [_THS_ALIAS[key]]
    target = _norm_board_name(name)
    if not target:
        return []
    exact, prefix, fuzzy = [], [], []
    for n in _ths_board_names(kind):
        nn = _norm_board_name(n)
        if nn == target:
            exact.append(n)
        elif nn.startswith(target) or target.startswith(nn):
            prefix.append((abs(len(nn) - len(target)), n))
        elif len(target) >= 2 and (target in nn or nn in target):
            fuzzy.append((abs(len(nn) - len(target)) + 1, n))
    prefix.sort()
    fuzzy.sort()
    return (exact + [n for _, n in prefix] + [n for _, n in fuzzy])[:3]


def _ths_try(nm: str, kind: str):
    import contextlib
    import io
    import akshare as ak

    end = datetime.now().strftime("%Y%m%d")
    try:
        with _THS_SEM, contextlib.redirect_stderr(io.StringIO()):
            if kind == "concept":
                df = ak.stock_board_concept_index_ths(
                    symbol=nm, start_date="20150101", end_date=end
                )
            else:
                df = ak.stock_board_industry_index_ths(
                    symbol=nm, start_date="20150101", end_date=end
                )
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    cols = list(df.columns)

    def pick(*names):
        for n in names:
            if n in cols:
                return n
        return None

    cd = pick("日期", "date")
    co = pick("开盘价", "开盘")
    ch = pick("最高价", "最高")
    cl = pick("最低价", "最低")
    cc = pick("收盘价", "收盘")
    cv = pick("成交量", "成交额")
    if not all([cd, co, ch, cl, cc]):
        return None
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append(
                {
                    "date": pd.to_datetime(r[cd]),
                    "open": float(r[co]),
                    "high": float(r[ch]),
                    "low": float(r[cl]),
                    "close": float(r[cc]),
                    "volume": float(r[cv]) if cv else 0.0,
                }
            )
        except (TypeError, ValueError):
            continue
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _ths_daily(name: str, kind: str):
    """返回 (日线 DataFrame, 实际使用的同花顺板块名)。"""
    cands = [name]
    base = _norm_board_name(name)
    if base and base != name:
        cands.append(base)
    for nm in cands:
        df = _ths_try(nm, kind)
        if df is not None:
            return df, nm
    for nm in _ths_candidates(name, kind):  # 同花顺里没有同名板块时的近似
        if nm in cands:
            continue
        df = _ths_try(nm, kind)
        if df is not None:
            _THS_ALIAS[(name, kind)] = nm
            return df, nm
    return None, name


def _ths_daily_cached(name: str, kind: str, retry: bool = False):
    """按 (名称, 类型) 缓存整段日线；失败只记 40s，用户重点一次会立即重试。"""
    k = (name, kind)
    now = time.time()
    node = _THS_MEM.get(k)
    if node and now - node[0] < 6 * 3600:
        return node[1], node[2]
    if not retry and _THS_FAIL.get(k, 0) > now:
        return None, name
    # 预取路径（retry=True）给足时间：整段日线要按年逐段抓，实测 ~10s+，
    # 用 9s 超时会把每次都判死；前端交互路径仍保留 9s 快速失败。
    df, used = _ths_daily_guarded(name, kind, timeout=40.0 if retry else 9.0)
    if df is None or df.empty:
        _THS_FAIL[k] = now + _THS_FAIL_TTL
        return None, name
    _THS_FAIL.pop(k, None)
    _THS_MEM[k] = (now, df, used)
    return df, used


def _ths_daily_guarded(name: str, kind: str, timeout: float = 9.0):
    from concurrent.futures import ThreadPoolExecutor

    if not name:
        return None, name
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_ths_daily, name, kind)
        return fut.result(timeout=timeout)
    except Exception:
        return None, name
    finally:
        ex.shutdown(wait=False)


# ---------------- 同花顺板块分时（日线以下） ----------------
_THS_INTRADAY_MEM: dict[tuple, tuple] = {}
_THS_INTRADAY_FAIL: dict[tuple, float] = {}
_THS_INTRADAY_TTL = 1800.0  # 年内整段历史缓存 30 分钟（尾部每次用 last.js + 当日分时增量）
_THS_DEEP_FAIL: dict[tuple, float] = {}
_THS_INTRADAY_MAX = 24  # 限制内存里的 DataFrame 数量（约 20~30MB 上限）
_THS_PERIOD = {"5m": "31", "15m": "41", "30m": "51", "60m": "51"}
_THS_FALLBACK = {"15m": ("5m", 3), "30m": ("15m", 2), "60m": ("30m", 2)}


def _ths_jsonp(url: str, tries: int = 3):
    """同花顺接口经常随机 502/504，重试几次基本都能拿到。"""
    for i in range(tries):
        try:
            with _THS_SEM:  # 同花顺对并发很敏感，最多 2 个同时打
                r = requests.get(url, headers=_ths_headers(), timeout=12)
            if r.status_code == 200:
                m = re.search(r"\((\{.*\})\)\s*;?\s*$", r.text, re.S)
                if m:
                    return json.loads(m.group(1))
        except Exception:
            pass
        time.sleep(0.12 + 0.35 * i)
    return None


def _ths_line_df(data: str):
    """解析 data="202610210935,open,high,low,close,volume,amount,...;..."。"""
    rows = []
    for seg in (data or "").split(";"):
        p = seg.split(",")
        if len(p) < 6:
            continue
        t = p[0].strip()
        try:
            ts = pd.to_datetime(t, format="%Y%m%d%H%M" if len(t) > 8 else "%Y%m%d")
            rows.append(
                {
                    "date": ts,
                    "open": float(p[1]),
                    "high": float(p[2]),
                    "low": float(p[3]),
                    "close": float(p[4]),
                    "volume": float(p[5] or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _ths_mem_put(key: tuple, df: pd.DataFrame) -> None:
    _THS_INTRADAY_MEM[key] = (time.time(), df)
    if len(_THS_INTRADAY_MEM) > _THS_INTRADAY_MAX:
        old = min(_THS_INTRADAY_MEM.items(), key=lambda kv: kv[1][0])[0]
        _THS_INTRADAY_MEM.pop(old, None)


def _agg_bars(df: pd.DataFrame, n: int):
    """同周期直接按自然日每 n 根合并（分时标签是区间结束时间，合并后时间取最后一根）。"""
    if n <= 1 or df is None or df.empty:
        return df
    parts = []
    for _, g in df.groupby(df.index.date):
        g = g.sort_index()
        for i in range(0, len(g), n):
            c = g.iloc[i : i + n]
            parts.append(
                {
                    "date": c.index[-1],
                    "open": float(c["open"].iloc[0]),
                    "high": float(c["high"].max()),
                    "low": float(c["low"].min()),
                    "close": float(c["close"].iloc[-1]),
                    "volume": float(c["volume"].sum()),
                }
            )
    if not parts:
        return df.iloc[0:0]
    return pd.DataFrame(parts).set_index("date")[["open", "high", "low", "close", "volume"]]


def _ths_time_df(code6: str):
    """当日分时（1 分钟）：v6/time 一次给全天，比按年拉 1 分钟 K 线便宜得多。"""
    d = _ths_jsonp(THS_TIME_URL.format(code=code6))
    if d:
        node = next(iter(d.values()), None) or {}
        date = str(node.get("date") or "")
        rows = []
        for seg in (node.get("data") or "").split(";"):
            p = seg.split(",")
            if len(p) < 2:
                continue
            try:
                ts = pd.to_datetime(date + p[0].strip(), format="%Y%m%d%H%M")
                px = float(p[1])
                vol = float(p[4]) if len(p) > 4 and p[4] not in ("", "-") else 0.0
            except (TypeError, ValueError):
                continue
            rows.append({"date": ts, "open": px, "high": px, "low": px, "close": px, "volume": vol})
        if rows:
            return (
                pd.DataFrame(rows)
                .set_index("date")
                .sort_index()[["open", "high", "low", "close", "volume"]]
            )
    d2 = _ths_jsonp(THS_LAST_URL.format(code=code6, period="61"))
    return _ths_line_df(((d2 or {}).get("data") or ""))


def _agg_live(df1: pd.DataFrame, minutes: int):
    """把当日 1 分钟价格合成 5/15/30/60 分，按 09:30 / 13:00 分段对齐。"""
    if df1 is None or df1.empty:
        return None
    groups: dict[pd.Timestamp, list] = {}
    for ts, r in df1.iterrows():
        hm = ts.hour * 60 + ts.minute
        if hm >= 13 * 60:
            start = ts.replace(hour=13, minute=0, second=0, microsecond=0)
        else:
            start = ts.replace(hour=9, minute=30, second=0, microsecond=0)
        m = int((ts - start).total_seconds() // 60)
        b = max(1, (m + minutes - 1) // minutes)
        end = start + pd.Timedelta(minutes=b * minutes)
        o, h, l, c, v = groups.get(end, (r["open"], r["high"], r["low"], r["close"], r["volume"]))
        groups[end] = (o, max(h, r["high"]), min(l, r["low"]), r["close"], v + r["volume"])
    if not groups:
        return None
    rows = [
        {"date": k, "open": a, "high": b, "low": c, "close": d, "volume": e}
        for k, (a, b, c, d, e) in groups.items()
    ]
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _ths_intraday_df(code6: str, tf: str, retry: bool = False):
    """同花顺板块：5/15/30 分拿年内整段，60 分由 30 分合并；1 分拿当日分时。"""
    live1 = _ths_time_df(code6)  # 当日 1 分钟（实时）
    if tf == "1m":
        return live1
    period = _THS_PERIOD.get(tf)
    if not period:
        return None
    key = (code6, period)
    now = time.time()
    if not retry and _THS_INTRADAY_FAIL.get(key, 0) > now:
        return None
    node = _THS_INTRADAY_MEM.get(key)
    deep = node[1] if (node and now - node[0] < _THS_INTRADAY_TTL) else None
    year = datetime.now().year
    # 整段历史只在冷启动/过期时拉一次；失败 30~60s 内不再重试，但尾部/当日分时照常更新
    short = None
    if deep is None and (retry or _THS_DEEP_FAIL.get(key, 0) <= now):
        d = _ths_jsonp(THS_YEAR_URL.format(code=code6, period=period, year=year), tries=4)
        got = _ths_line_df(((d or {}).get("data") or ""))
        if got is None or len(got) < 200:  # 年初 / 当年接口失败，用上一年补
            d2 = _ths_jsonp(THS_YEAR_URL.format(code=code6, period=period, year=year - 1))
            prev = _ths_line_df(((d2 or {}).get("data") or ""))
            if prev is not None and not prev.empty:
                got = pd.concat([prev, got]) if got is not None and not got.empty else prev
                got = got[~got.index.duplicated(keep="last")].sort_index()
        if got is not None and len(got) >= 200:
            deep = got
            _ths_mem_put(key, deep)
            _THS_DEEP_FAIL.pop(key, None)
        else:
            # 只拿到很短一截（接口限流 / 上一年文件也缺）：先拿来画，但不写长缓存，
            # 30 秒后再补历史，避免 30 分钟都只有几十根。
            short = got if (got is not None and not got.empty) else None
            _THS_DEEP_FAIL[key] = now + 30
    base = deep if deep is not None else short
    # 尾部增量：last.js 只有 140 根，但足够把最近几天/当天补上
    d = _ths_jsonp(THS_LAST_URL.format(code=code6, period=period))
    tail = _ths_line_df(((d or {}).get("data") or ""))
    if tail is not None and not tail.empty:
        if base is None or base.empty:
            base = tail
        else:
            base = pd.concat([base, tail])
            base = base[~base.index.duplicated(keep="last")].sort_index()
    # 60 分没有独立接口：先把 30 分历史合成 60 分，再接当日的实时 60 分
    if tf == "60m" and base is not None and not base.empty:
        base = _agg_bars(base, 2)
    # 同花顺历史接口有时会晚一天：用当日实时 1 分钟补上今天（含未收盘的那根）
    today = _agg_live(live1, int(tf[:-1]))
    if today is not None and not today.empty:
        if base is None or base.empty:
            base = today
        else:
            base = pd.concat([base, today])
            base = base[~base.index.duplicated(keep="last")].sort_index()
    if base is None or base.empty:
        _THS_INTRADAY_FAIL[key] = now + _THS_FAIL_TTL
        fb = _THS_FALLBACK.get(tf)
        if fb:
            sub = _ths_intraday_df(code6, fb[0], retry)
            if sub is not None and not sub.empty:
                return _agg_bars(sub, fb[1])
        return None
    _THS_INTRADAY_FAIL.pop(key, None)
    return base


def cn_bars(
    code: str,
    tf: str,
    name: str = "",
    kind: str = "industry",
    retry: bool = False,
    tail: int | None = None,
) -> tuple[list[dict], str]:
    secid = "90." + code
    is_ths_code = str(code).startswith("T")
    if tf.endswith("m"):
        if is_ths_code:
            df = _ths_intraday_df(str(code)[1:], tf, retry)
            if df is not None and not df.empty:
                label = {
                    "1m": "同花顺分时",
                    "5m": "同花顺5分",
                    "15m": "同花顺15分",
                    "30m": "同花顺30分",
                    "60m": "同花顺60分",
                }.get(tf, "同花顺分时")
                if tail:
                    df = df.tail(tail)
                return _rows(df.tail(700), True), label
            return [], ""
        klt = int(tf[:-1])
        df = _em_kline(secid, klt, tail or 800)
        if df is not None and not df.empty:
            if tf == "1m":
                df = _clip_last_day(df)
            return _rows(df.tail(700), True), "东财K线"
        df = _em_trends(secid, 5)
        if df is not None and not df.empty:
            if klt <= 1:
                last_day = df.index.date.max()
                df = df[df.index.date == last_day]
            else:
                df = _resample_intraday(df, klt)
            if tail:
                df = df.tail(tail)
            return _rows(df.tail(700), True), "东财分时"
        return [], ""

    klt = {"1d": 101, "1w": 102, "1M": 103}.get(tf, 101)
    if not is_ths_code:
        df = _em_kline(secid, klt, tail or MAX_BARS)
        if df is not None and not df.empty:
            return _rows(df.tail(MAX_BARS), False), "东财K线"
    df, used = _ths_daily_cached(name, kind, retry)
    src = "同花顺日线" + ("" if (not used or used == name) else f"（{used}）")
    if df is None or df.empty:
        if is_ths_code:
            return [], ""
        df = _em_flow_daily(secid, tail or MAX_BARS)
        src = "东财资金流(收盘)"
    if tf == "1d":
        ak_rows = _ak_board_bars(name, kind)
        if ak_rows and (df is None or df.empty or len(ak_rows) > len(df)):
            return ak_rows, "akshare板块日线"
    if df is not None and not df.empty:
        df = _resample_tf(df, tf)
        if tail:
            df = df.tail(tail)
        return _rows(df.tail(MAX_BARS), False), src
    return [], ""


# ---------------- K 线：美股 ----------------
def _sina_jsonp(url: str):
    try:
        r = requests.get(url, headers=SINA_H, timeout=15)
        txt = r.text
        m = re.search(r"\((\[.*\])\)\s*;?\s*$", txt, re.S)
        if not m:
            m = re.search(r"\((\[.*\])\)", txt, re.S)
        if not m:
            return []
        return json.loads(m.group(1))
    except Exception:
        return []


def _sina_us_daily(symbol: str):
    url = (
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20x=/"
        f"US_MinKService.getDailyK?symbol={symbol}&___qn=3"
    )
    raw = _sina_jsonp(url)
    rows = []
    for r in raw:
        try:
            d = pd.to_datetime(str(r.get("d"))[:10])
            o, h, l, c = (float(r[k]) for k in ("o", "h", "l", "c"))
            v = float(r.get("v") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if c <= 0:
            continue
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _sina_us_min(symbol: str, k: int):
    url = (
        "https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/x/"
        f"US_MinKService.getMinK?symbol={symbol}&type={k}"
    )
    raw = _sina_jsonp(url)
    rows = []
    for r in raw:
        try:
            d = pd.to_datetime(str(r.get("d")))
            o, h, l, c = (float(r[key]) for key in ("o", "h", "l", "c"))
            v = float(r.get("v") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _nasdaq_daily(symbol: str):
    try:
        from core.datasets.us_data import fetch_daily_kline

        d = fetch_daily_kline(symbol, 900)
        d["date"] = pd.to_datetime(d["date"])
        return (
            d.set_index("date")[["open", "high", "low", "close", "volume"]]
            .sort_index()
            .astype(float)
        )
    except Exception:
        return None


_SPLIT_RATIOS = (
    0.1,
    1 / 9,
    1 / 8,
    1 / 7,
    1 / 6,
    0.2,
    0.25,
    1 / 3,
    0.5,
    2 / 3,
    0.75,
    1.5,
    4 / 3,
    2.0,
    3.0,
    4.0,
    5.0,
    6.0,
    7.0,
    8.0,
    9.0,
    10.0,
)


def _match_split(r: float) -> float | None:
    if not r or r <= 0:
        return None
    for cand in _SPLIT_RATIOS:
        if abs(r - cand) / cand <= 0.03:
            return cand
    return None


def _adjust_splits(df):
    """把未复权价里的拆股/合股跳变拉平（新浪/Nasdaq 美股常见）。"""
    if df is None or len(df) < 2:
        return df
    df = df.copy()
    o = df["open"].tolist()
    h = df["high"].tolist()
    lo = df["low"].tolist()
    c = df["close"].tolist()
    v = df["volume"].tolist() if "volume" in df else None
    n = len(df)
    factors = [1.0] * n
    cum = 1.0
    for i in range(n - 1, 0, -1):
        factors[i] = cum
        cand = _match_split(c[i] / c[i - 1]) if c[i - 1] else None
        if cand and cand != 1.0:
            ok = True
            for arr in (o, h, lo):
                if arr[i - 1]:
                    rr = arr[i] / arr[i - 1]
                    if abs(rr - cand) / cand > 0.05:
                        ok = False
                        break
            if ok:
                cum *= cand
    factors[0] = cum
    df["open"] = [x * f for x, f in zip(o, factors)]
    df["high"] = [x * f for x, f in zip(h, factors)]
    df["low"] = [x * f for x, f in zip(lo, factors)]
    df["close"] = [x * f for x, f in zip(c, factors)]
    if v is not None:
        df["volume"] = [x / f for x, f in zip(v, factors)]
    return df


def _has_bad_jumps(df) -> bool:
    if df is None or len(df) < 2:
        return False
    c = df["close"].tolist()
    for i in range(1, len(c)):
        if c[i - 1] and c[i]:
            r = c[i] / c[i - 1]
            if r < 0.55 or r > 1.8:
                return True
    return False


def us_bars(code: str, tf: str, tail: int | None = None) -> tuple[list[dict], str]:
    if tf.endswith("m"):
        k = int(tf[:-1])
        base_k = 1 if k <= 1 else (5 if k <= 5 else 15)
        df = _sina_us_min(code, base_k)
        if df is not None and not df.empty:
            if k <= 1:
                last_day = df.index.date.max()
                df = df[df.index.date == last_day]
            elif k > base_k:
                df = _resample_intraday(df, k)
            if tail:
                df = df.tail(tail)
            return _rows(df.tail(700), True), "新浪分时"

    # 新浪历史更长（约10年+），优先；拆股修正后若仍有异常跳变，再回退 Nasdaq
    df = _sina_us_daily(code)
    src = "新浪日线"
    if df is None or df.empty:
        df = _nasdaq_daily(code)
        src = "Nasdaq日线"
    if df is not None and not df.empty:
        df = _adjust_splits(df)
        if _has_bad_jumps(df):
            nd = _nasdaq_daily(code)
            if nd is not None and not nd.empty:
                nd = _adjust_splits(nd)
                if not _has_bad_jumps(nd):
                    df, src = nd, "Nasdaq日线"
        df = _resample_tf(df, tf)
        if tail:
            df = df.tail(tail)
        return _rows(df.tail(MAX_BARS), False), src
    return [], ""


# ---------------- K 线：A股个股 / 指数 ----------------
def _cn_prefix(code: str) -> str:
    code = str(code or "")
    if code.startswith(("60", "68", "90", "51", "58", "11", "50")):
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def _cn_symbol(code: str) -> str:
    """'002463' / 'SZ002463' / 'sz002463' -> 'sz002463'"""
    c = str(code or "").strip().upper().replace(".", "")
    if c[:2] in ("SH", "SZ", "BJ"):
        return c[:2].lower() + c[2:]
    return _cn_prefix(c) + c


def _cn_code_norm(code: str) -> str:
    """统一成 SH600000 / SZ002463，用于缓存 key、画线 key、自选。"""
    s = _cn_symbol(code)
    return s[:2].upper() + s[2:]


def _idx_code_norm(code: str) -> str:
    """000001 -> SH000001，399001 -> SZ399001"""
    c = str(code or "").strip().upper().replace(".", "")
    if c[:2] in ("SH", "SZ", "BJ"):
        return c
    return ("SH" if c.startswith("000") else "SZ") + c


def _qq_kline(symbol: str, count: int = 800) -> pd.DataFrame | None:
    """腾讯前复权日线（个股）/ 日线（指数）。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
    try:
        js = requests.get(url, headers=QQ_H, timeout=12).json()
        d = (js.get("data") or {}).get(symbol) or {}
        arr = d.get("qfqday") or d.get("day") or []
    except Exception:
        return None
    rows = []
    for a in arr:
        try:
            dt = pd.to_datetime(str(a[0])[:10])
            o, c, h, low = float(a[1]), float(a[2]), float(a[3]), float(a[4])
            v = float(a[5] or 0) * 100  # 腾讯是「手」，统一成「股」
        except (TypeError, ValueError, IndexError):
            continue
        if c <= 0:
            continue
        rows.append({"date": dt, "open": o, "high": h, "low": low, "close": c, "volume": v})
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _qq_minute(symbol: str) -> pd.DataFrame | None:
    """腾讯当日 1 分钟分时（指数/个股都支持）。"""
    try:
        js = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
            params={"code": symbol},
            headers=QQ_H,
            timeout=12,
        ).json()
        d = ((js.get("data") or {}).get(symbol) or {}).get("data") or {}
        arr = d.get("data") or []
        day = str(d.get("date") or "").strip()
    except Exception:
        return None
    if not arr:
        return None
    if len(day) != 8:
        day = datetime.now().strftime("%Y%m%d")
    rows, prev_vol = [], 0.0
    for line in arr:
        f = str(line).split()
        if len(f) < 2:
            continue
        try:
            hhmm, price = f[0], float(f[1])
            cum = float(f[2]) if len(f) > 2 else 0.0
        except (TypeError, ValueError):
            continue
        try:
            dt = pd.to_datetime(day + hhmm.zfill(4), format="%Y%m%d%H%M")
        except (TypeError, ValueError):
            continue
        dv = max(cum - prev_vol, 0.0)  # 腾讯给的是累计成交量
        prev_vol = cum
        rows.append(
            {
                "date": dt,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": dv * 100,
            }
        )
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _sina_cn_kline(symbol: str, scale: int, datalen: int = 1023) -> pd.DataFrame | None:
    """新浪分钟/日线（个股、指数通用）；scale: 1/5/15/30/60/240。"""
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale={scale}&ma=no&datalen={datalen}"
    )
    try:
        r = requests.get(url, headers=SINA_H, timeout=15)
        raw = r.json()
    except Exception:
        return None
    rows = []
    for it in raw if isinstance(raw, list) else []:
        try:
            dt = pd.to_datetime(str(it.get("day")))
            o, h, low, c = (float(it[k]) for k in ("open", "high", "low", "close"))
            v = float(it.get("volume") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if c <= 0:
            continue
        rows.append({"date": dt, "open": o, "high": h, "low": low, "close": c, "volume": v})
    if not rows:
        return None
    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def _ak_stock_bars(code: str) -> list[dict]:
    """akshare 新浪 A股日线（15年+），用于腾讯/新浪失败时兜底。"""
    try:
        import akshare as ak

        code = re.sub(r"^(SH|SZ|BJ)", "", str(code).strip().upper())
        # 北交所（43/83/87/92 开头）新浪也能取，别错当成深市
        if code.startswith(("4", "8", "92")):
            prefix = "bj"
        elif code.startswith(("60", "68", "90")):
            prefix = "sh"
        else:
            prefix = "sz"
        df = ak.stock_zh_a_daily(
            symbol=prefix + code,
            start_date="20100101",
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df is None or df.empty:
            return []
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        df = df.sort_index()
        cols = ["open", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in cols):
            return []
        return _rows(df[cols].tail(MAX_BARS), False)
    except Exception:
        return []


def _ak_board_bars(name: str, kind: str) -> list[dict]:
    """akshare 东财行业/概念板块日线（15年+）。"""
    try:
        import akshare as ak

        nm = re.sub(r"[（(].*?[）)]", "", str(name or "")).strip()
        if not nm:
            return []
        fn = (
            ak.stock_board_industry_hist_em
            if kind == "industry"
            else ak.stock_board_concept_hist_em
        )
        df = fn(
            symbol=nm,
            start_date="20100101",
            end_date=datetime.now().strftime("%Y%m%d"),
            period="日k",
            adjust="",
        )
        if df is None or df.empty:
            return []
        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            }
        )
        if "date" not in df.columns:
            return []
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        cols = ["open", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in cols):
            return []
        return _rows(df[cols].tail(MAX_BARS), False)
    except Exception:
        return []


def _ak_index_bars(code: str) -> list[dict]:
    """akshare 新浪指数日线（15年+）。"""
    try:
        import akshare as ak

        code = re.sub(r"^(SH|SZ|BJ)", "", str(code).strip().upper())
        prefix = "sh" if code.startswith(("000", "88", "99")) else "sz"
        df = ak.stock_zh_index_daily(symbol=prefix + code)
        if df is None or df.empty:
            return []
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        df = df.sort_index()
        cols = ["open", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in cols):
            return []
        return _rows(df[cols].tail(MAX_BARS), False)
    except Exception:
        return []


def _clip_last_day(df):
    """分时只保留最后一个交易日（盘中就是今天，休市就是最近交易日）。"""
    try:
        if df is None or len(df) == 0:
            return df
        last = df.index.date.max()
        return df[df.index.date == last]
    except Exception:
        return df


def cn_stock_bars(code: str, tf: str, tail: int | None = None) -> tuple[list[dict], str]:
    """A股个股 / ETF 的K线：日线走腾讯前复权，分钟走新浪（1/5/15/30/60）。"""
    sym = _cn_symbol(code)
    if tf.endswith("m"):
        m = max(1, int(tf[:-1]))
        df = _sina_cn_kline(sym, m, tail or 1023)
        src = f"新浪{m}分"
        if df is None or df.empty:
            df = _qq_minute(sym)
            src = "腾讯分时"
        if df is None or df.empty:
            return [], ""
        if tf == "1m":
            df = _clip_last_day(df)
        if tail:
            df = df.tail(tail)
        return _rows(df.tail(700), True), src
    df = _qq_kline(sym, tail or MAX_BARS)
    src = "腾讯日线(前复权)"
    if df is None or df.empty:
        df = _sina_cn_kline(sym, 240, tail or MAX_BARS)
        src = "新浪日线"
    if df is None or df.empty or len(df) < 1000:
        ak_rows = _ak_stock_bars(code)
        if ak_rows:
            return ak_rows, "akshare日线"
    if df is None or df.empty:
        return [], ""
    if tail:
        df = df.tail(tail)
    return _rows(df.tail(MAX_BARS), False), src


def cn_index_bars(code: str, tf: str, tail: int | None = None) -> tuple[list[dict], str]:
    """指数K线：日线腾讯，分钟新浪（1 分钟兜底腾讯当日分时）。"""
    sym = _cn_symbol(_idx_code_norm(code).lower())
    if tf.endswith("m"):
        m = max(1, int(tf[:-1]))
        df = _sina_cn_kline(sym, m, tail or 1023)
        src = f"新浪指数{m}分"
        if df is None or df.empty:
            df = _qq_minute(sym)
            src = "腾讯指数分时"
        if df is None or df.empty:
            return [], ""
        if tf == "1m":
            df = _clip_last_day(df)
        if tail:
            df = df.tail(tail)
        return _rows(df.tail(700), True), src
    df = _qq_kline(sym, tail or MAX_BARS)
    src = "腾讯指数日线"
    if df is None or df.empty:
        df = _sina_cn_kline(sym, 240, tail or MAX_BARS)
        src = "新浪指数日线"
    if df is None or df.empty or len(df) < 1000:
        ak_rows = _ak_index_bars(_idx_code_norm(code))
        if ak_rows:
            return ak_rows, "akshare指数日线"
    if df is None or df.empty:
        return [], ""
    if tail:
        df = df.tail(tail)
    return _rows(df.tail(MAX_BARS), False), src


def _sb_type(typ: str) -> tuple[str, str] | None:
    """腾讯 smartbox 类型 -> (kind, 标签)"""
    t = (typ or "").strip().upper()
    if t == "ZS":
        return "index", "指数"
    if t.startswith("GP-A"):
        return "stock", "个股"
    if t in ("ETF", "LOF"):
        return "stock", "ETF"
    return None


def _unescape_u(s: str) -> str:
    """腾讯 smartbox 返回的名字是字面量 \\uXXXX，还原为中文。"""
    if not s or "\\u" not in s:
        return s
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


def board_movers(code: str, name: str = ""):
    """板块成分股：按涨幅取前5领涨、后5领跌。东财失败时用 akshare 兜底。"""
    rows = []
    url = (
        f"/api/qt/clist/get?pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3&fs=b:{code}"
        "&fields=f12,f14,f2,f3"
    )
    js = em_get(url)
    for it in _items(js) if js else []:
        c = str(it.get("f12") or "")
        if not c:
            continue
        rows.append(
            {
                "code": c,
                "name": it.get("f14") or c,
                "price": _num(it.get("f2")),
                "pct": _num(it.get("f3")),
            }
        )
    if not rows and name:
        try:
            import akshare as ak

            nm = re.sub(r"[（(].*?[）)]", "", str(name)).strip()
            df = None
            for fn in (ak.stock_board_industry_cons_em, ak.stock_board_concept_cons_em):
                try:
                    df = fn(symbol=nm)
                    if df is not None and not df.empty:
                        break
                except Exception:
                    df = None
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    c = str(r.get("代码") or "").strip()
                    if not c:
                        continue
                    rows.append(
                        {
                            "code": c,
                            "name": r.get("名称") or c,
                            "price": _num(r.get("最新价")),
                            "pct": _num(r.get("涨跌幅")),
                        }
                    )
                rows.sort(key=lambda x: x["pct"], reverse=True)
        except Exception:
            pass
    up = rows[:5]
    down = rows[-5:][::-1] if len(rows) > 5 else []
    return up, down


_US_COMMON = [
    ("NVDA", "英伟达"),
    ("AMD", "AMD"),
    ("AAPL", "苹果"),
    ("MSFT", "微软"),
    ("GOOGL", "谷歌"),
    ("AMZN", "亚马逊"),
    ("META", "Meta"),
    ("TSLA", "特斯拉"),
    ("AVGO", "博通"),
    ("TSM", "台积电"),
    ("MU", "美光"),
    ("INTC", "英特尔"),
    ("QCOM", "高通"),
    ("NFLX", "奈飞"),
    ("BABA", "阿里巴巴"),
    ("PDD", "拼多多"),
    ("SOXL", "半导体3倍做多"),
    ("SOXX", "半导体ETF"),
    ("SMH", "半导体ETF"),
]


def _search_us(q: str) -> list[dict]:
    """美股代码/名称搜索：内置常用 + my_pool + Yahoo 兜底。"""
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower()
    out: list[dict] = []
    seen: set[str] = set()

    def add(code: str, name: str) -> None:
        c = str(code or "").strip().upper()
        if not c or c in seen:
            return
        seen.add(c)
        out.append({"market": "us", "kind": "stock", "code": c, "name": name or c, "tag": "美股"})

    try:
        import csv

        with open(os.path.join(DATA_DIR, "my_pool.csv"), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                c = str(r.get("代码") or "").strip()
                n = str(r.get("名称") or c)
                if c and (ql in c.lower() or ql in n.lower()):
                    add(c, n)
    except Exception:
        pass
    for c, n in _US_COMMON:
        if ql in c.lower() or ql in n.lower():
            add(c, n)
    if q.isascii():
        try:
            r = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": 10, "newsCount": 0, "enableFuzzyQuery": "false"},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=8,
            )
            data = r.json() if r.ok else {}
            for x in data.get("quotes", []):
                if x.get("quoteType") not in ("EQUITY", "ETF"):
                    continue
                sym = str(x.get("symbol") or "").strip()
                add(sym, x.get("shortname") or x.get("longname") or sym)
                if len(out) >= 12:
                    break
        except Exception:
            pass
    return out[:12]


def search_symbols(q: str) -> list[dict]:
    """按名称/代码搜个股、指数（腾讯 smartbox + 内置常用指数）。"""
    q = (q or "").strip()
    if not q:
        return []
    key = "search:v2:" + q.lower()
    hit = _mem_get(key, 300)
    if hit is None:
        hit = cache_get(key, 6 * 3600)
        if hit is not None:
            _mem_set(key, hit)
    if hit is not None:
        return hit
    out: list[dict] = []
    seen: set[str] = set()

    def add(code: str, name: str, kind: str, tag: str) -> None:
        code = _idx_code_norm(code) if kind == "index" else _cn_code_norm(code)
        if not code or code in seen:
            return
        seen.add(code)
        out.append({"market": "cn", "kind": kind, "code": code, "name": name, "tag": tag})

    ql = q.lower()
    for code, name in CN_INDEX_LIST:
        if ql in name.lower() or ql in code.lower() or (q.isdigit() and q in code):
            add(code, name, "index", "指数")
    try:
        r = requests.get(
            "https://smartbox.gtimg.cn/s3/",
            params={"v": "2", "q": q, "t": "all"},
            headers=QQ_H,
            timeout=8,
        )
        r.encoding = "gbk"
        m = re.search(r'v_hint="(.*?)"', r.text)
        parts = m.group(1).split("^") if m else []
        for part in parts:
            f = part.split("~")
            if len(f) < 5:
                continue
            mk, code6, name, _py, typ = f[0], f[1], _unescape_u(f[2]), f[3], f[4]
            conv = _sb_type(typ)
            if not conv or mk not in ("sh", "sz", "bj"):
                continue
            kind, tag = conv
            add((mk + code6).upper(), name, kind, tag)
            if len(out) >= 14:
                break
    except Exception:
        pass
    have = {x["code"].upper() for x in out}
    for it in _search_us(q):
        if len(out) >= 14:
            break
        if it["code"].upper() in have:
            continue
        out.append(it)
        have.add(it["code"].upper())
    out = out[:14]
    if out:
        _mem_set(key, out)
        cache_put(key, out)
    return out


# ---------------- 汇总接口 ----------------
def _append_today_bar(
    bars: list,
    market: str,
    code: str,
    kind: str,
    tf: str,
    name: str = "",
    retry: bool = False,
) -> list:
    """A股个股/指数/板块：日线还没出今天时，用分钟数据合成一根当日 K。"""
    if not bars or market != "cn" or tf != "1d":
        return bars
    today = datetime.now().strftime("%Y-%m-%d")
    if str(bars[-1].get("t") or "")[:10] >= today:
        return bars
    try:
        if kind in ("stock", "index"):
            sym = _cn_symbol(_idx_code_norm(code).lower()) if kind == "index" else _cn_symbol(code)
            df = _sina_cn_kline(sym, 1, 300)
            if df is None or df.empty:
                df = _qq_minute(sym)
            if df is None or df.empty:
                return bars
            idx = pd.to_datetime(df.index)
            day = df[idx.strftime("%Y-%m-%d") == today]
            if day.empty:
                return bars
            bars.append(
                {
                    "t": today,
                    "o": round(float(day["open"].iloc[0]), 4),
                    "h": round(float(day["high"].max()), 4),
                    "l": round(float(day["low"].min()), 4),
                    "c": round(float(day["close"].iloc[-1]), 4),
                    "v": float(day["volume"].sum()),
                }
            )
        else:
            mb, _ = cn_bars(code, "1m", name, kind, retry, tail=300)
            tb = [b for b in (mb or []) if str(b.get("t") or "").startswith(today)]
            if not tb:
                return bars
            bars.append(
                {
                    "t": today,
                    "o": float(tb[0]["o"]),
                    "h": max(float(b["h"]) for b in tb),
                    "l": min(float(b["l"]) for b in tb),
                    "c": float(tb[-1]["c"]),
                    "v": sum(float(b.get("v") or 0) for b in tb),
                }
            )
    except Exception:
        pass
    return bars


def _bars_fetch(
    market: str,
    code: str,
    tf: str,
    name: str,
    kind: str,
    retry: bool = False,
    tail: int | None = None,
) -> tuple[list, str]:
    try:
        if market == "us":
            bars, src = us_bars(code, tf, tail=tail)
        elif kind == "stock":
            bars, src = cn_stock_bars(code, tf, tail=tail)
        elif kind == "index":
            bars, src = cn_index_bars(code, tf, tail=tail)
        else:
            bars, src = cn_bars(code, tf, name, kind, retry, tail=tail)
    except Exception:
        bars, src = [], ""
    if bars:
        if tail is None and market == "us":
            db_bars_replace(market, code, tf, bars, src)
        else:
            db_bars_put(market, code, tf, bars, src)
    before = len(bars)
    bars = _append_today_bar(bars, market, code, kind, tf, name, retry)
    if len(bars) > before:
        db_bars_put(market, code, tf, bars[-1:], src)
    return bars, src


def _prev_close(market: str, code: str, alt: str, day: str) -> float | None:
    """分时的参考价：该交易日之前的最后一个日线收盘（= 昨收）。"""
    try:
        for c in (code, alt):
            if not c:
                continue
            for b in reversed(db_bars_get(market, c, "1d") or []):
                if str(b.get("t"))[:10] < day:
                    return float(b["c"])
    except Exception:
        pass
    return None


def _tail_day(bars: list[dict]) -> list[dict]:
    """分时只保留最后一个交易日。"""
    if not bars:
        return bars
    try:
        last = max(str(b.get("t") or "")[:10] for b in bars)
        return [b for b in bars if str(b.get("t") or "")[:10] == last]
    except Exception:
        return bars


def bars_payload(
    market: str, code: str, tf: str, name: str = "", kind: str = "industry", retry: bool = False
) -> dict:
    tf = tf if tf in TF_RULES else "1d"
    market = "us" if market == "us" else "cn"
    kind = kind if kind in ("concept", "stock", "index") else "industry"
    if market == "cn" and kind == "stock":
        code = _cn_code_norm(code)
    elif market == "cn" and kind == "index":
        code = _idx_code_norm(code)

    # 周/月线直接从库里的日线重采样：不发请求、毫秒级，切周期零等待。
    if tf in ("1w", "1M"):
        base = bars_payload(market, code, "1d", name, kind, retry)
        bars = _resample_rows(base.get("bars") or [], tf)
        return {
            "market": market,
            "code": code,
            "name": name,
            "tf": tf,
            "bars": bars,
            "count": len(bars),
            "derived": True,
            "src": (base.get("src") or "") + ("·周线" if tf == "1w" else "·月线"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "stale": base.get("stale", False),
            "age": base.get("age", 0),
        }

    bkey = f"bars:{market}:{code}:{tf}"
    _ACTIVE_BARS[bkey] = time.time()
    _BARS_ARGS[bkey] = (name, kind)
    bars = db_bars_get(market, code, tf)
    read_code = code
    if not bars and market == "cn" and kind in ("stock", "index"):
        bare = re.sub(r"^(SH|SZ|BJ)", "", code)
        if bare != code:
            bars = db_bars_get(market, bare, tf)
            if bars:
                read_code = bare
    if not OFFLINE:
        bars = _append_today_bar(bars, market, read_code, kind, tf, name, retry)
    meta = db_meta_get(bkey)
    age = time.time() - meta["ts"] if meta else 0.0
    fresh = bool(bars) and age < _bar_ttl(tf)
    if OFFLINE:
        return {
            "market": market,
            "code": code,
            "name": name,
            "tf": tf,
            "bars": _tail_day(bars) if tf == "1m" else bars,
            "src": (meta or {}).get("src", ""),
            "count": len(bars),
            "time": datetime.now().strftime("%H:%M:%S"),
            "stale": not fresh,
            "pending": False,
            "offline": True,
            "age": round(max(age, 0), 1),
            "fetched_at": (meta or {}).get("ts", 0),
        }
    if bars and not fresh:  # 有过期数据：先返回，后台只补最近 N 根
        _spawn_once(
            bkey,
            lambda: _bars_fetch(market, code, tf, name, kind, retry, tail=_refresh_tail(tf)),
        )
    pending = False
    if not bars:  # 首次访问：最多等 2.5s，超时就后台继续抓
        fut = _submit_once(bkey, lambda: _bars_fetch(market, code, tf, name, kind, retry))
        if fut is not None:
            try:
                fut.result(timeout=2.5)
            except Exception:
                pass
        else:
            deadline = time.time() + 2.0
            while time.time() < deadline and not db_bars_get(market, code, tf, 1):
                time.sleep(0.05)
        bars = db_bars_get(market, code, tf)
        meta = db_meta_get(bkey)
        age = time.time() - meta["ts"] if meta else 0.0
        fresh = bool(bars)
        pending = not bars
    shown = _tail_day(bars) if tf == "1m" else bars
    prev_close = None
    if tf == "1m" and shown:
        prev_close = _prev_close(market, code, read_code, str(shown[0]["t"])[:10])
    return {
        "market": market,
        "code": code,
        "name": name,
        "tf": tf,
        "prev_close": prev_close,
        "bars": shown,
        "src": (meta or {}).get("src", ""),
        "count": len(bars),
        "time": datetime.now().strftime("%H:%M:%S"),
        "stale": not fresh,
        "pending": pending,
        "age": round(max(age, 0), 1),
        "fetched_at": (meta or {}).get("ts", 0),
    }


# ---------------- 画线 / 自选（JSON 文件，便于 git 跟踪） ----------------
# 行情数据留在 SQLite（大、可重抓）；画线和自选是人手产出的小数据，
# 存成 data/sector_lines.json / data/watch.json，可以直接提交到仓库。
_MIGRATED = [False]


def _read_json_file(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file(path: str, data) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _migrate_lines_once() -> None:
    """老版本把画线存在 SQLite：首次启动时搬到 data/sector_lines.json。"""
    if _MIGRATED[0]:
        return
    _MIGRATED[0] = True
    if os.path.exists(LINES_FILE):
        return
    try:
        with _DB_LOCK:
            rows = db().execute("SELECT key,payload FROM lines").fetchall()
        if rows:
            _write_json_file(LINES_FILE, {k: json.loads(v) for k, v in rows})
    except Exception:
        pass


def load_lines() -> dict:
    _migrate_lines_once()
    d = _read_json_file(LINES_FILE, {})
    return d if isinstance(d, dict) else {}


def save_key_lines(key: str, lines: list) -> None:
    try:
        with _DB_LOCK:
            d = _read_json_file(LINES_FILE, {})
            if not isinstance(d, dict):
                d = {}
            if lines:
                d[str(key)] = lines
            else:
                d.pop(str(key), None)
            _write_json_file(LINES_FILE, d)
    except Exception:
        pass


def load_watch_db() -> list:
    d = _read_json_file(WATCH_FILE, None)
    if isinstance(d, list):
        return d
    # 老版本存在 SQLite：搬迁一次
    try:
        with _DB_LOCK:
            row = db().execute("SELECT payload FROM watch WHERE id='default'").fetchone()
        items = json.loads(row[0]) if row else []
        if items:
            _write_json_file(WATCH_FILE, items)
        return items
    except Exception:
        return []


def save_watch_db(items: list) -> None:
    try:
        with _DB_LOCK:
            _write_json_file(WATCH_FILE, list(items or []))
    except Exception:
        pass


def cache_get(key: str, ttl: float):
    try:
        with _DB_LOCK:
            row = db().execute("SELECT ts,payload FROM cache WHERE key=?", (str(key),)).fetchone()
        if row and time.time() - row[0] < ttl:
            return json.loads(row[1])
    except Exception:
        pass
    return None


def cache_put(key: str, obj) -> None:
    try:
        with _DB_LOCK:
            conn = db()
            conn.execute(
                "INSERT OR REPLACE INTO cache(key,ts,payload) VALUES(?,?,?)",
                (str(key), time.time(), json.dumps(obj, ensure_ascii=False)),
            )
            conn.commit()
    except Exception:
        pass


# ---------------- HTTP ----------------
def _cn_sector_of_stock(code: str) -> dict | None:
    """A股个股 → 所属行业板块（评分表里的「行业」→ 同花顺板块代码）。"""
    try:
        from core.scoring.company_score import load_scores

        df = load_scores()
        if df is None or df.empty or "行业" not in df.columns:
            return None
        c6 = re.sub(r"\D", "", str(code))[-6:]
        hit = df[df["代码"].astype(str).str[-6:] == c6]
        if hit.empty:
            return None
        ind = str(hit.iloc[0].get("行业") or "").strip()
        if not ind:
            return None
        try:
            from core.scoring.sector_relation import _ths_industry_names

            names = _ths_industry_names()     # {T881xxx: 名称}
        except Exception:
            names = {}
        norm = lambda s: str(s or "").replace("Ⅱ", "").replace("Ⅲ", "").strip()
        target = norm(ind)
        for sc, nm in names.items():
            n2 = norm(nm)
            if n2 == target or (target and (n2 in target or target in n2)):
                return {"market": "cn", "kind": "industry", "code": sc, "name": nm}
        return {"market": "cn", "kind": "industry", "code": "", "name": ind}
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 复用连接，省掉每次重连

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, encoding: str | None = None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept-Encoding")
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _json(self, obj, code: int = 200):
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        enc = None
        if len(raw) > 900 and self._accepts_gzip():
            raw = gzip.compress(raw, 5)
            enc = "gzip"
        self._send(raw, "application/json; charset=utf-8", code, enc)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/game":
            _ensure_game_server()
            host = (self.headers.get("Host") or "127.0.0.1").split(":")[0]
            html = GAME_HTML.replace("__HOST__", host).encode("utf-8")
            self._send(html, "text/html; charset=utf-8", 200)
            return
        if u.path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as f:
                    html = f.read()
            except FileNotFoundError:
                self._send(b"UI file missing", "text/plain; charset=utf-8", 500)
                return
            enc = None
            if self._accepts_gzip():
                html = gzip.compress(html, 6)
                enc = "gzip"
            self._send(html, "text/html; charset=utf-8", 200, enc)
            return
        if u.path == "/api/sectors":
            market = (q.get("market", ["cn"])[0] or "cn").strip()
            kind = (q.get("kind", ["industry"])[0] or "industry").strip()
            if kind not in SECTOR_FS:
                kind = "industry"
            try:
                self._json(sector_payload(market, kind))
            except Exception as e:
                self._json({"error": str(e)[:120]}, 500)
            return
        if u.path == "/api/sectors_ma":
            market = "us" if (q.get("market", ["cn"])[0] == "us") else "cn"
            kind = (q.get("kind", ["industry"])[0] or "industry").strip()
            if kind not in SECTOR_FS:
                kind = "industry"
            try:
                limit = int(q.get("limit", ["30"])[0] or 30)
            except Exception:
                limit = 30
            try:
                self._json(sector_ma_payload(market, kind, max(5, min(limit, 60))))
            except Exception as e:
                self._json({"up": [], "down": [], "error": str(e)[:120]})
            return
        if u.path == "/api/board_movers":
            code = (q.get("code", [""])[0] or "").strip()
            market = (q.get("market", ["cn"])[0] or "cn").strip()
            name = (q.get("name", [""])[0] or "").strip()
            if market != "cn" or not code:
                self._json({"up": [], "down": [], "note": "美股ETF无成分股"})
                return
            try:
                up, down = board_movers(code, name)
                self._json({"up": up, "down": down})
            except Exception as e:
                self._json({"up": [], "down": [], "error": str(e)[:120]})
            return
        if u.path == "/api/board_scores":
            code = (q.get("code", [""])[0] or "").strip()
            name = (q.get("name", [""])[0] or "").strip()
            market = (q.get("market", ["cn"])[0] or "cn").strip()
            try:
                top = int(q.get("top", ["20"])[0] or 20)
            except Exception:
                top = 20
            top = max(1, min(top, 50))
            if market != "cn":
                self._json({"items": [], "note": "美股暂不支持"})
                return
            try:
                from core.scoring.company_score import board_scores, scores_ready

                if not scores_ready():
                    self._json({"items": [], "pending": True, "note": "评分计算中"})
                    return
                codes = []
                items = board_scores(name=name, codes=codes, top=top)
                if not items and code:
                    try:
                        from core.datasets.cn_data import get_board_constituents

                        cons = get_board_constituents(code, 500)
                        codes = [str(c.get("code")) for c in cons if c.get("code")]
                        if codes:
                            items = board_scores(name=name, codes=codes, top=top)
                    except Exception:
                        pass
                self._json(
                    {"items": items, "basis": "constituents" if len(codes) >= 5 else "industry"}
                )
            except Exception as e:
                self._json({"items": [], "error": str(e)[:120]})
            return
        if u.path == "/api/news":
            try:
                limit = int(q.get("limit", ["300"])[0] or 300)
            except Exception:
                limit = 300
            cat = (q.get("category", [""])[0] or "").strip()
            kw = (q.get("q", [""])[0] or "").strip()
            try:
                from core.events.news_feed import state as news_state

                if (q.get("refresh", ["0"])[0] or "") == "1":
                    from core.events.news_feed import kick_refresh

                    kick_refresh(True)
                code = (q.get("code", [""])[0] or "").strip()
                try:
                    min_level = int(q.get("level", ["0"])[0] or 0)
                except Exception:
                    min_level = 0
                stock = (q.get("stock", [""])[0] or "").strip()
                watch = (q.get("watch", ["0"])[0] or "") == "1"
                self._json(news_state(limit=limit, category=cat, q=kw, code=code,
                                      stock=stock, min_level=min_level, watch=watch))
            except Exception as e:
                self._json({"items": [], "counts": {}, "error": str(e)[:160]})
            return
        if u.path == "/api/stock_industry":
            raw = (q.get("codes", [""])[0] or "").strip()
            codes = [c for c in raw.split(",") if c.strip()][:200]
            out = {}
            try:
                from core.scoring.company_score import load_scores

                df = load_scores()
                if df is not None and not df.empty and "行业" in df.columns:
                    idx = {}
                    for _, r in df.iterrows():
                        idx[str(r.get("代码")).zfill(6)] = str(r.get("行业") or "")
                    for c in codes:
                        c6 = re.sub(r"\D", "", c)[-6:].zfill(6)
                        ind = idx.get(c6)
                        if ind:
                            out[c] = ind
            except Exception:
                pass
            self._json({"items": out})
            return
        if u.path == "/api/stock_context":
            market = (q.get("market", ["us"])[0] or "us").strip().lower()
            code = (q.get("code", [""])[0] or "").strip()
            if not code:
                self._json({"sector": None, "etfs": [], "note": "缺少 code"})
                return
            try:
                from core.datasets.etf_holdings import (
                    US_BROAD,
                    etf_holders,
                    warm_us_etfs,
                )

                # 美股：后台补齐 11 个宽基行业 ETF 的持仓缓存，供反查使用
                if market == "us":
                    _HOLD_POOL.submit(warm_us_etfs)

                holders = etf_holders(code, market, limit=6)
                if market == "us":
                    names = dict(US_ALL)
                    names.update({c: c for c in US_INDEX_CODES})
                    for h in holders:
                        h["name"] = names.get(h["code"], h["code"])
                    broad = [h for h in holders if h["code"] in US_BROAD]
                    sector = None
                    if broad:
                        top = broad[0]
                        sector = {
                            "market": "us",
                            "kind": "industry",
                            "code": top["code"],
                            "name": US_BROAD[top["code"]],
                            "weight": top["weight"],
                        }
                else:
                    names = {str(w.get("code")): str(w.get("name") or w.get("code"))
                             for w in load_watch_db()}
                    for h in holders:
                        h["name"] = names.get(h["code"], h["code"])
                    sector = _cn_sector_of_stock(code)
                self._json({"sector": sector, "etfs": holders, "code": code, "market": market})
            except Exception as e:
                self._json({"sector": None, "etfs": [], "error": str(e)[:160]})
            return
        if u.path == "/api/sector_relations":
            market = (q.get("market", ["cn"])[0] or "cn").strip()
            kind = (q.get("kind", ["industry"])[0] or "industry").strip()
            if kind not in ("industry", "concept"):
                kind = "industry"
            try:
                top = int(q.get("top", ["3"])[0] or 3)
            except Exception:
                top = 3
            try:
                from core.scoring.sector_relation import sector_relations

                self._json(sector_relations(market, kind, top=max(1, min(top, 5))))
            except Exception as e:
                self._json({"items": {}, "error": str(e)[:160]})
            return
        if u.path == "/api/etf_holdings":
            market = (q.get("market", ["us"])[0] or "us").strip()
            code = (q.get("code", [""])[0] or "").strip()
            try:
                top = int(q.get("top", ["10"])[0] or 10)
            except Exception:
                top = 10
            top = max(1, min(top, 20))
            if not code:
                self._json({"items": [], "note": "缺少 code"})
                return
            try:
                from core.datasets.etf_holdings import etf_holdings

                # 网络源可能慢：限时 12s，超时先返回 pending，后台继续算并写缓存
                fut = _HOLD_POOL.submit(etf_holdings, market, code, top)
                try:
                    items = fut.result(timeout=12)
                except FuturesTimeoutError:
                    self._json({"items": [], "pending": True, "note": "持仓数据抓取中，稍后再试"})
                    return
                self._json({
                    "items": items,
                    "note": "" if items else "暂无持仓数据（数据源暂不可用）",
                })
            except Exception as e:
                self._json({"items": [], "error": str(e)[:120]})
            return
        if u.path == "/api/search":
            q = (q.get("q", [""])[0] or "").strip()
            try:
                self._json({"results": search_symbols(q)})
            except Exception as e:
                self._json({"results": [], "error": str(e)[:120]}, 500)
            return
        if u.path == "/api/bars":
            market = (q.get("market", ["cn"])[0] or "cn").strip()
            code = (q.get("code", [""])[0] or "").strip().upper()
            name = (q.get("name", [""])[0] or "").strip()
            tf = (q.get("tf", ["1d"])[0] or "1d").strip()
            kind = (q.get("kind", ["industry"])[0] or "industry").strip()
            retry = (q.get("retry", ["0"])[0] or "0") in ("1", "true", "yes")
            if not code:
                self._json({"error": "code required"}, 400)
                return
            try:
                self._json(bars_payload(market, code, tf, name, kind, retry))
            except Exception as e:
                self._json({"error": str(e)[:120], "bars": []}, 500)
            return
        if u.path == "/api/lines":
            self._json({"lines": load_lines()})
            return
        if u.path == "/api/watch":
            self._json({"items": load_watch_db()})
            return
        if u.path == "/api/stats":
            try:
                with _DB_LOCK:
                    conn = db()
                    nb = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
                    ns = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                    nc = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                nl = len(load_lines())
                size = os.path.getsize(DB_FILE) if os.path.exists(DB_FILE) else 0
                self._json(
                    {
                        "bars": nb,
                        "snapshots": ns,
                        "lines": nl,
                        "cache": nc,
                        "db_kb": round(size / 1024, 1),
                        "inflight": len(_INFLIGHT),
                        "active_bars": len(_ACTIVE_BARS),
                    }
                )
            except Exception as e:
                self._json({"error": str(e)[:120]}, 500)
            return
        if u.path == "/api/diag":
            urls = {
                "eastmoney_delay": "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3",
                "eastmoney": "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3",
                "tencent": "https://qt.gtimg.cn/q=sh600812",
                "ths": "https://q.10jqka.com.cn/thshy/",
                "sina": "https://hq.sinajs.cn/list=sh600812",
                "smartbox": "https://smartbox.gtimg.cn/s3/?v=2&q=%E5%8D%8A%E5%AF%BC%E4%BD%93&t=all",
                "nasdaq": "https://api.nasdaq.com/api/quote/SOXX/info?assetclass=etf",
            }
            hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

            def _probe(u):
                try:
                    r = requests.get(u, headers=hdr, timeout=4)
                    return {
                        "status": r.status_code,
                        "ok": r.status_code == 200 and len(r.text) > 20,
                    }
                except Exception as e:
                    return {"status": None, "ok": False, "error": str(e)[:80]}

            srcs = {}
            try:
                with ThreadPoolExecutor(max_workers=7) as ex:
                    for k, v in zip(urls, ex.map(_probe, urls.values())):
                        srcs[k] = v
            except Exception:
                pass
            counts = {}
            try:
                with _DB_LOCK:
                    conn = db()
                    for tb in ("bars", "snapshots", "lines", "watch", "cache"):
                        counts[tb] = conn.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0]
            except Exception:
                pass
            self._json({"db": counts, "sources": srcs, "time": datetime.now().strftime("%H:%M:%S")})
            return
        if u.path == "/api/ping":
            self._json({"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            return
        self._send(b"not found", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/watch":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n) if n else b"{}"
                d = json.loads(body.decode("utf-8") or "{}")
                items = d.get("items")
                if not isinstance(items, list):
                    self._json({"ok": False, "error": "items invalid"}, 400)
                    return
                clean = []
                for it in items[:200]:
                    if not isinstance(it, dict) or not it.get("code"):
                        continue
                    clean.append(
                        {
                            "market": "us" if it.get("market") == "us" else "cn",
                            "code": str(it.get("code"))[:16],
                            "name": str(it.get("name") or "")[:40],
                            "kind": str(it.get("kind") or "industry")[:16],
                            "tf": str(it.get("tf") or "1d")[:8],
                            "group": str(it.get("group") or "")[:16],
                            "w": int(it.get("w") or 0) or None,
                            "h": int(it.get("h") or 0) or None,
                        }
                    )
                old_stocks = {
                    re.sub(r"\D", "", str(x.get("code") or ""))[-6:].zfill(6)
                    for x in (load_watch_db() or [])
                    if x.get("market") != "us" and str(x.get("kind")) not in ("industry", "concept")
                }
                save_watch_db(clean)
                new_stocks = {
                    re.sub(r"\D", "", str(x.get("code") or ""))[-6:].zfill(6)
                    for x in clean
                    if x.get("market") != "us" and str(x.get("kind")) not in ("industry", "concept")
                }
                added = new_stocks - old_stocks
                if added:
                    # 新增订阅：后台立刻补这只票的公告/财报
                    def _warm_news():
                        try:
                            from core.events import news_feed

                            n = news_feed.refresh_watch_stocks(days=7)
                            print(f"[news] 新增自选补抓 {sorted(added)} → {n} 条", flush=True)
                        except Exception as e:  # noqa: BLE001
                            print(f"[news] 新增自选补抓失败: {e}", flush=True)
                    threading.Thread(target=_warm_news, daemon=True, name="watch-news").start()
                self._json({"ok": True, "n": len(clean), "news_added": sorted(added)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:120]}, 400)
            return
        if u.path != "/api/lines":
            self._send(b"not found", "text/plain", 404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n else b"{}"
            d = json.loads(body.decode("utf-8") or "{}")
            key = str(d.get("key") or "").strip()
            lines = d.get("lines")
            if not key or not isinstance(lines, list):
                self._json({"ok": False, "error": "key/lines invalid"}, 400)
                return
            clean = []
            for ln in lines[:200]:
                if not isinstance(ln, dict):
                    continue
                if ln.get("type") == "h":
                    clean.append({"type": "h", "y": round(float(ln.get("y") or 0), 4)})
                elif ln.get("type") == "trend":
                    clean.append(
                        {
                            "type": "trend",
                            "x1": str(ln.get("x1"))[:32],
                            "y1": round(float(ln.get("y1") or 0), 4),
                            "x2": str(ln.get("x2"))[:32],
                            "y2": round(float(ln.get("y2") or 0), 4),
                        }
                    )
                elif ln.get("type") == "v":
                    clean.append(
                        {
                            "type": "v",
                            "x": str(ln.get("x"))[:32],
                            "y1": round(float(ln.get("y1") or 0), 4),
                            "y2": round(float(ln.get("y2") or 0), 4),
                        }
                    )
            save_key_lines(key, clean)
            self._json({"ok": True, "n": len(clean)})
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:120]}, 400)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def _background_ticker() -> None:
    """保持「最近访问过的列表/图表」在后台新鲜，前端拿到的永远不是过期数据。"""
    while True:
        try:
            now = time.time()
            for key, seen in list(_ACTIVE_LISTS.items()):
                if now - seen > 900:
                    _ACTIVE_LISTS.pop(key, None)
                    continue
                parts = key.split(":")
                if len(parts) != 3:
                    continue
                market, kind = parts[1], parts[2]
                snap = db_snapshot_get(key)
                if not snap or now - snap["ts"] > _list_ttl(market):
                    _spawn_once(key, lambda m=market, k=kind: _sector_fetch(m, k))
            budget = 6
            for bkey, seen in list(_ACTIVE_BARS.items()):
                if budget <= 0:
                    break
                if now - seen > 600:
                    _ACTIVE_BARS.pop(bkey, None)
                    _BARS_ARGS.pop(bkey, None)
                    continue
                parts = bkey.split(":")
                if len(parts) != 4:
                    continue
                _, market, code, tf = parts
                meta = db_meta_get(bkey)
                ttl = _bar_ttl(tf)
                if tf.endswith("m") and not _in_session(market):
                    ttl = max(ttl, 120)
                if meta and now - meta["ts"] < ttl:
                    continue
                name, kind = _BARS_ARGS.get(bkey, ("", "industry"))
                if _spawn_once(
                    bkey, lambda m=market, c=code, t=tf, n=name, k=kind: _bars_fetch(m, c, t, n, k)
                ):
                    budget -= 1
            # 自选图表也保持后台新鲜（即使当前没在看）
            wbudget = 4
            for it in (load_watch_db() or [])[:20]:
                if wbudget <= 0:
                    break
                mkt = "us" if it.get("market") == "us" else "cn"
                code = it.get("code") or ""
                if not code:
                    continue
                kind = it.get("kind") or "industry"
                name = it.get("name") or ""
                for tf in dict.fromkeys([it.get("tf") or "1d", "1d"]):
                    bkey = f"bars:{mkt}:{code}:{tf}"
                    meta = db_meta_get(bkey)
                    ttl = 900 if tf == "1d" else _bar_ttl(tf)
                    if meta and now - meta["ts"] < ttl:
                        continue
                    if _spawn_once(
                        bkey, lambda m=mkt, c=code, t=tf, n=name, k=kind: _bars_fetch(m, c, t, n, k)
                    ):
                        wbudget -= 1
                    if wbudget <= 0:
                        break
        except Exception:
            pass
        time.sleep(1.5)


def _warmup() -> None:
    """启动预热：列表 + 成交额前 N 个板块的日线，首屏点击基本秒开。

    已有新鲜数据的直接跳过，所以重启服务不会重复打接口。
    """
    for m, k in (("cn", "industry"), ("us", "industry")):
        snap = db_snapshot_get(f"list:{m}:{k}")
        if snap and time.time() - snap["ts"] < _list_ttl(m):
            continue
        try:
            _sector_fetch(m, k)
        except Exception:
            pass
    try:
        snap = db_snapshot_get("list:cn:industry")
        rows = ((snap or {}).get("payload") or {}).get("rows") or []
        for r in rows[:12]:
            code = r.get("code") or ""
            if not code:
                continue
            bkey = f"bars:cn:{code}:1d"
            meta = db_meta_get(bkey)
            if meta and time.time() - meta["ts"] < 1800:
                continue
            _BARS_ARGS[bkey] = (r.get("name", ""), r.get("group", "industry"))
            _spawn_once(
                bkey,
                lambda c=code, n=r.get("name", ""), g=r.get("group", "industry"): _bars_fetch(
                    "cn", c, "1d", n, g
                ),
            )
            time.sleep(0.2)
        snap = db_snapshot_get("list:us:industry")
        rows = ((snap or {}).get("payload") or {}).get("rows") or []
        for r in rows[:8]:
            code = r.get("code") or ""
            if not code:
                continue
            bkey = f"bars:us:{code}:1d"
            meta = db_meta_get(bkey)
            if meta and time.time() - meta["ts"] < 1800:
                continue
            _BARS_ARGS[bkey] = (r.get("name", ""), "industry")
            _spawn_once(bkey, lambda c=code: _bars_fetch("us", c, "1d", "", "industry"))
            time.sleep(0.1)
    except Exception:
        pass
    # 预热 MA21：把 A股行业成交额前 60 个丢进后台队列
    try:
        base = sector_payload("cn", "industry")
        for r in sorted(base.get("rows") or [], key=lambda x: -(x.get("amount") or 0))[:60]:
            _ma_enqueue("cn", "industry", r.get("code", ""), r.get("name", ""))
    except Exception:
        pass


def _score_warmup() -> None:
    try:
        from core.scoring.company_score import load_scores

        load_scores()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="A股/美股板块划线看盘（Web）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--no-warm", action="store_true", help="关闭启动预热 / 后台刷新")
    ap.add_argument("--offline", action="store_true", help="只读本地库，不访问外部数据源")
    args = ap.parse_args()
    global OFFLINE
    if args.offline:
        OFFLINE = True
    os.makedirs(DATA_DIR, exist_ok=True)
    db()
    db_prune()
    _ensure_game_server()
    threading.Thread(target=_score_warmup, daemon=True, name="score-warmup").start()
    # 新闻常驻抓取：每 5 分钟一轮，前端只读库
    try:
        from core.events.news_feed import start_poller

        start_poller(300)
    except Exception as e:
        print(f"[news] 轮询启动失败: {e}", flush=True)
    if not args.no_warm:
        threading.Thread(target=_warmup, daemon=True, name="warmup").start()
        threading.Thread(target=_background_ticker, daemon=True, name="ticker").start()
    srv = _Server((args.host, args.port), Handler)
    print(f"板块划线看盘已启动: http://{args.host}:{args.port}   (Ctrl+C 退出)")
    print(f"数据库: {DB_FILE}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
