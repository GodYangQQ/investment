"""中国财经/政策日历：重要会议、LPR、经济数据。

数据源：
- 经济数据：Nasdaq 经济日历（country=China，含预期/前值）
- 固定大事件：全国两会、进博会、广交会、LPR 报价（按惯例日期）
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.events.us_calendar import _et_ts, _get_json

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CACHE_FILE = DATA_DIR / "cn_calendar_cache.json"
CACHE_TTL = 3600
CAL_VERSION = 2
BJ = ZoneInfo("Asia/Shanghai")

# Nasdaq 的中国数据很杂：只保留能确认口径的，并换成官方常见发布时间（北京时间）
NAME_MAP = {
    "CPI": ("CPI", "09:30"),
    "PPI": ("PPI", "09:30"),
    "Trade Balance (USD)": ("贸易帐（美元）", "11:00"),
    "FX Reserves (USD)": ("外汇储备（美元）", "16:00"),
    "RatingDog Services PMI": ("RatingDog 服务业 PMI", "09:45"),
    "RatingDog Manufacturing PMI": ("RatingDog 制造业 PMI", "09:45"),
}
# 以下要么是重复口径，要么单位不可靠，直接不要
DROP = {"Exports", "Imports", "Trade Balance", "China Loan Prime Rate 5Y"}

_CACHE: dict = {"ts": 0.0, "days": 0, "data": None}


def _bj_ts(d: date, hhmm: str) -> float:
    try:
        hh, mm = (hhmm.split(":") + ["0", "0"])[:2]
        return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=BJ).timestamp()
    except Exception:
        return time.time()


def _econ_day(d: str) -> list[dict]:
    try:
        rows = ((_get_json(f"https://api.nasdaq.com/api/calendar/economicevents?date={d}") or {})
                .get("data") or {}).get("rows") or []
    except Exception:
        return []
    out: dict[tuple, dict] = {}
    for x in rows:
        if x.get("country") != "China":
            continue
        raw = str(x.get("eventName") or "").strip()
        if raw in DROP or raw not in NAME_MAP:
            continue
        name, hhmm = NAME_MAP[raw]
        tm = str(x.get("gmt") or "").strip()
        # Nasdaq 的日期是美东日期：先换算成北京日期，再套官方发布时间
        try:
            bj_date = datetime.fromtimestamp(_et_ts(d, tm), BJ).date() if re.fullmatch(r"\d{1,2}:\d{2}", tm) else date.fromisoformat(d)
        except Exception:
            bj_date = date.fromisoformat(d)
        key = (bj_date.isoformat(), name)
        cons = str(x.get("consensus") or "").replace("&nbsp;", "").strip()
        prev = str(x.get("previous") or "").replace("&nbsp;", "").strip()
        if key in out:
            # 同一事件的多行（同比/环比）合并前值，避免看起来像重复
            if prev and prev not in out[key]["previous"]:
                out[key]["previous"] = (out[key]["previous"] + " / " + prev).strip(" /")
            if cons and not out[key]["consensus"]:
                out[key]["consensus"] = cons
            continue
        out[key] = {
            "date": bj_date.isoformat(), "time": hhmm, "ts": _bj_ts(bj_date, hhmm),
            "kind": "econ", "title": name, "importance": 3 if name in ("CPI", "PPI", "贸易帐（美元）") else 2,
            "consensus": cons, "previous": prev, "actual": "",
            "url": "https://www.nasdaq.com/market-activity/economic-calendar",
        }
    return list(out.values())


def _fixed_events(days: int) -> list[dict]:
    start = date.today()
    end = start + timedelta(days=days)
    out: list[dict] = []

    def add(d: date, title: str, hhmm: str = "09:00", detail: str = "", imp: int = 3, url: str = "") -> None:
        if start <= d <= end:
            out.append({
                "date": d.isoformat(), "time": hhmm, "ts": _bj_ts(d, hhmm),
                "kind": "policy", "title": title, "detail": detail,
                "importance": imp, "url": url or "http://www.gov.cn/",
            })

    for y in range(start.year, end.year + 1):
        # 全国两会：政协 3/3、人大 3/5
        add(date(y, 3, 3), "全国政协会议开幕", "15:00", "全国两会", 3,
            "http://www.cppcc.gov.cn/")
        add(date(y, 3, 5), "全国人大会议开幕", "09:00", "政府工作报告 / 预算 / 经济目标", 3,
            "http://www.npc.gov.cn/")
        # 中国国际进口博览会（上海）
        add(date(y, 11, 5), "中国国际进口博览会开幕", "09:00", "上海 · 通常 11/5-11/10", 2,
            "https://www.ciie.org/")
        # 广交会（广州）
        add(date(y, 4, 15), "广交会开幕（春季）", "09:00", "广州 · 通常 4/15-5/5", 2,
            "https://www.cantonfair.org.cn/")
        add(date(y, 10, 15), "广交会开幕（秋季）", "09:00", "广州 · 通常 10/15-11/4", 2,
            "https://www.cantonfair.org.cn/")

    # LPR 每月 20 日 09:15（遇周末/节假日以实际公布为准）
    d = start
    while d <= end:
        if d.day == 20:
            add(d, "LPR 报价", "09:15", "1 年期 / 5 年期以上", 2,
                "http://www.pbc.gov.cn/")
        d += timedelta(days=1)
    return out


def _fetch_days(days: int) -> list[dict]:
    today = date.today()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(max(1, days))]
    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_econ_day, d) for d in dates]
        for f in as_completed(futs):
            try:
                events += f.result() or []
            except Exception:
                continue
    seen = set()
    out = []
    for e in events:
        key = (e.get("date"), e.get("time"), e.get("title"), e.get("consensus"), e.get("previous"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


_WARMING = {"on": False}


def _build(days: int) -> dict:
    events = _fixed_events(days) + _fetch_days(days)
    events.sort(key=lambda x: (x.get("ts") or 0, x.get("kind") or ""))
    result = {
        "version": CAL_VERSION, "region": "cn", "events": events, "days": days,
        "updated": time.time(),
        "sources": {"econ": "api.nasdaq.com/calendar/economicevents",
                    "policy": "gov.cn / npc.gov.cn / pbc.gov.cn"},
    }
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return result


def cached_calendar(days: int = 30, allow_stale: bool = True) -> dict | None:
    days = max(1, int(days or 30))
    now = time.time()
    if _CACHE["data"] is not None and _CACHE["days"] >= days:
        if allow_stale or now - _CACHE["ts"] < CACHE_TTL:
            return _CACHE["data"]
    if CACHE_FILE.exists():
        try:
            d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if d.get("version") == CAL_VERSION and int(d.get("days") or 0) >= days:
                if allow_stale or now - CACHE_FILE.stat().st_mtime < CACHE_TTL:
                    _CACHE.update(ts=CACHE_FILE.stat().st_mtime, days=int(d["days"]), data=d)
                    return d
        except Exception:
            pass
    return None


def start_warm(days: int = 30) -> bool:
    if _WARMING["on"]:
        return False
    _WARMING["on"] = True

    def run():
        try:
            d = _build(days)
            _CACHE.update(ts=time.time(), days=days, data=d)
        except Exception:
            pass
        finally:
            _WARMING["on"] = False

    threading.Thread(target=run, daemon=True, name="cn-calendar").start()
    return True


def calendar_view(days: int = 30, refresh: bool = False) -> dict:
    days = max(1, min(int(days or 30), 120))
    c = cached_calendar(days, allow_stale=True)
    age = time.time() - float((c or {}).get("updated") or 0)
    need = refresh or c is None or age > CACHE_TTL
    if need:
        start_warm(days)
    if c is None:
        return {"version": CAL_VERSION, "region": "cn", "events": [], "days": days,
                "pending": True, "refreshing": True, "updated": 0}
    return {**c, "pending": False, "refreshing": need, "age": round(age, 1)}
