"""美国财经日历：FOMC、关键经济数据、自选美股财报日期。

数据源：
- FOMC 会议日期：美联储官网（官方一手）
- 经济数据日历：Nasdaq 经济日历（含预期/前值）
- 财报日期：Nasdaq 财报日历（盘前/盘后 + EPS 预期）
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

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CACHE_FILE = DATA_DIR / "us_calendar_cache.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
TIMEOUT = 8
CACHE_TTL = 3600

ET = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")

# 只留对美股有直接影响的数据（太多的地方性数据会淹没日历）
KEY_EVENTS = (
    "CPI", "PPI", "PCE", "GDP", "Nonfarm", "Payroll", "Unemployment",
    "Retail Sales", "ISM", "PMI", "Jobless Claims", "Interest Rate",
    "FOMC", "Consumer Confidence", "Durable Goods", "Housing Starts",
    "Existing Home", "New Home", "Trade Balance", "Factory Orders",
    "Industrial Production", "Michigan", "ADP", "Challenger",
)
HIGH_IMPACT = ("FOMC", "CPI", "PPI", "PCE", "GDP", "Nonfarm", "Payroll", "Interest Rate")

_CACHE: dict = {"ts": 0.0, "days": 0, "watch": "", "data": None}


def _get_json(url: str, tries: int = 2) -> dict:
    import requests

    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json() or {}
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5 + i * 0.5)
    if last:
        raise last
    return {}


def _watch_us() -> list[str]:
    try:
        items = json.loads((DATA_DIR / "watch.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    return sorted({str(x.get("code") or "").upper() for x in items or []
                   if x.get("market") == "us" and x.get("code")})


def _et_ts(d: str, hhmm: str = "") -> float:
    """美东日期/时间 → 北京时间戳。"""
    try:
        hh, mm = (hhmm.split(":") + ["0", "0"])[:2] if hhmm else (16, 0)
        dt = datetime.fromisoformat(d).replace(hour=int(hh), minute=int(mm), tzinfo=ET)
        return dt.astimezone(BJ).timestamp()
    except Exception:
        try:
            return datetime.fromisoformat(d).replace(tzinfo=ET).astimezone(BJ).timestamp()
        except Exception:
            return time.time()


# ---------------- FOMC ----------------
def _fomc_events(days: int = 400) -> list[dict]:
    import requests
    from bs4 import BeautifulSoup

    try:
        html = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                            headers={"User-Agent": UA}, timeout=TIMEOUT).text
    except Exception:
        return []
    months = {m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"], 1)}
    out: list[dict] = []
    cutoff = date.today() - timedelta(days=1)
    limit = date.today() + timedelta(days=days)
    for yr in re.finditer(r">(\d{4}) FOMC Meetings<", html):
        year = int(yr.group(1))
        nxt = re.search(r">(\d{4}) FOMC Meetings<", html[yr.end():])
        seg = html[yr.end(): yr.end() + nxt.start()] if nxt else html[yr.end():]
        soup = BeautifulSoup(seg, "html.parser")
        for mt in soup.select(".fomc-meeting"):
            mnode = mt.select_one(".fomc-meeting__month")
            dnode = mt.select_one(".fomc-meeting__date")
            if not mnode or not dnode:
                continue
            mon = months.get(mnode.get_text(strip=True))
            nums = re.findall(r"\d{1,2}", dnode.get_text(" ", strip=True))
            if not mon or not nums:
                continue
            for day in nums[-1:]:
                try:
                    d = date(year, mon, int(day))
                except ValueError:
                    continue
                if d < cutoff or d > limit:
                    continue
                out.append({
                    "date": d.isoformat(),
                    "time": "14:00",
                    "ts": _et_ts(d.isoformat(), "14:00"),
                    "kind": "fomc",
                    "title": f"FOMC 利率决议（{d.month}月会议）",
                    "importance": 3,
                    "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                    "detail": "美东 14:00 声明，14:30 新闻发布会",
                })
    return out


# ---------------- 经济数据 ----------------
def _econ_day(d: str) -> list[dict]:
    try:
        rows = ((_get_json(f"https://api.nasdaq.com/api/calendar/economicevents?date={d}") or {})
                .get("data") or {}).get("rows") or []
    except Exception:
        return []
    out = []
    for x in rows:
        if x.get("country") != "United States":
            continue
        name = str(x.get("eventName") or "").strip()
        if not name or not any(k.lower() in name.lower() for k in KEY_EVENTS):
            continue
        tm = str(x.get("gmt") or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", tm):
            tm = ""
        hi = any(k.lower() in name.lower() for k in HIGH_IMPACT)
        out.append({
            "date": d,
            "time": tm,
            "ts": _et_ts(d, tm),
            "kind": "econ",
            "title": name,
            "importance": 3 if hi else 2,
            "consensus": str(x.get("consensus") or "").replace("&nbsp;", "").strip(),
            "previous": str(x.get("previous") or "").replace("&nbsp;", "").strip(),
            "actual": str(x.get("actual") or "").replace("&nbsp;", "").strip(),
            "url": "https://www.nasdaq.com/market-activity/economic-calendar",
        })
    return out


# ---------------- 财报 ----------------
def _earnings_day(d: str, watch: set[str]) -> list[dict]:
    try:
        rows = ((_get_json(f"https://api.nasdaq.com/api/calendar/earnings?date={d}") or {})
                .get("data") or {}).get("rows") or []
    except Exception:
        return []
    out = []
    for x in rows:
        sym = str(x.get("symbol") or "").upper()
        if not sym or sym not in watch:
            continue
        sess = str(x.get("time") or "")
        label = {"time-pre-market": "盘前", "time-after-hours": "盘后"}.get(sess, "时间待定")
        ts = _et_ts(d, "08:00" if sess == "time-pre-market" else "16:00")
        eps = str(x.get("epsForecast") or "").strip()
        out.append({
            "date": d,
            "time": "" if sess not in ("time-pre-market", "time-after-hours") else ("08:00" if sess == "time-pre-market" else "16:00"),
            "ts": ts,
            "kind": "earnings",
            "symbol": sym,
            "title": f"{sym} 财报（{label}）",
            "importance": 3,
            "detail": f"预期 EPS {eps}" if eps else "",
            "url": f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}/earnings",
        })
    return out


def _fetch_days(days: int, watch: set[str]) -> list[dict]:
    today = date.today()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(max(1, days))]
    events: list[dict] = []

    def task(d: str, kind: str):
        return _econ_day(d) if kind == "econ" else _earnings_day(d, watch)

    # 每个（日期, 类型）一个任务：60 个请求并行，别让一个慢请求拖住一天
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = []
        for d in dates:
            futs.append(ex.submit(task, d, "econ"))
            if watch:
                futs.append(ex.submit(task, d, "earnings"))
        for f in as_completed(futs):
            try:
                events += f.result() or []
            except Exception:
                continue
    # 同一时间重复的条目（Nasdaq 会把 CPI 的同比/环比拆成多行）
    seen: set[tuple] = set()
    out = []
    for e in events:
        key = (e.get("kind"), e.get("date"), e.get("time"), e.get("symbol"),
               e.get("title"), e.get("consensus"), e.get("previous"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: (x.get("ts") or 0, x.get("kind") or ""))
    return out


def us_calendar(days: int = 45, force: bool = False) -> dict:
    days = max(1, min(int(days or 45), 120))
    watch = _watch_us()
    sig = ",".join(watch)
    now = time.time()
    if (not force and _CACHE["data"] is not None and now - _CACHE["ts"] < CACHE_TTL
            and _CACHE["days"] >= days and _CACHE["watch"] == sig):
        return _CACHE["data"]
    if not force and CACHE_FILE.exists() and now - CACHE_FILE.stat().st_mtime < CACHE_TTL:
        try:
            d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if d.get("watch") == sig and int(d.get("days") or 0) >= days:
                _CACHE.update(ts=CACHE_FILE.stat().st_mtime, days=int(d["days"]), watch=sig, data=d)
                return d
        except Exception:
            pass

    events = _fomc_events(max(days, 365))
    events += _fetch_days(days, set(watch))
    events.sort(key=lambda x: (x.get("ts") or 0, x.get("kind") or ""))
    result = {
        "events": events,
        "days": days,
        "watch": watch,
        "watch_sig": sig,
        "updated": time.time(),
        "sources": {
            "fomc": "federalreserve.gov",
            "econ": "api.nasdaq.com/calendar/economicevents",
            "earnings": "api.nasdaq.com/calendar/earnings",
        },
    }
    _CACHE.update(ts=now, days=days, watch=sig, data=result)
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return result


_WARMING = {"on": False}


def cached_calendar(days: int = 30, allow_stale: bool = True) -> dict | None:
    """只读缓存，不发网络请求。"""
    days = max(1, int(days or 30))
    sig = ",".join(_watch_us())
    now = time.time()
    if _CACHE["data"] is not None and _CACHE["days"] >= days and _CACHE["watch"] == sig:
        d = _CACHE["data"]
        if allow_stale or now - _CACHE["ts"] < CACHE_TTL:
            return d
    if CACHE_FILE.exists():
        try:
            d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_sig = d.get("watch")
            if isinstance(cached_sig, list):
                cached_sig = ",".join(cached_sig)
            if int(d.get("days") or 0) >= days and cached_sig == sig:
                if allow_stale or now - CACHE_FILE.stat().st_mtime < CACHE_TTL:
                    _CACHE.update(ts=CACHE_FILE.stat().st_mtime, days=int(d["days"]),
                                  watch=sig, data=d)
                    return d
        except Exception:
            pass
    return None


def start_warm(days: int = 30) -> bool:
    """后台构建日历，接口立即返回旧缓存/空列表。"""
    if _WARMING["on"]:
        return False
    _WARMING["on"] = True

    def run():
        try:
            us_calendar(days=days, force=True)
        except Exception:
            pass
        finally:
            _WARMING["on"] = False

    threading.Thread(target=run, daemon=True, name="us-calendar").start()
    return True


def calendar_view(days: int = 30, refresh: bool = False) -> dict:
    """给接口用：有缓存先返回，后台再更新。"""
    days = max(1, min(int(days or 30), 120))
    c = cached_calendar(days, allow_stale=True)
    age = time.time() - float((c or {}).get("updated") or 0)
    need = refresh or c is None or age > CACHE_TTL
    if need:
        start_warm(days)
    if c is None:
        return {"events": [], "days": days, "pending": True, "refreshing": True,
                "updated": 0, "watch": _watch_us()}
    return {**c, "pending": False, "refreshing": need, "age": round(age, 1)}
