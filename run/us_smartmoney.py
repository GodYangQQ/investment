#!/usr/bin/env python3
"""
美股"聪明钱"追踪
================
数据源：SEC EDGAR（公开）。
  - 内部人 Form 4：高管/董事买卖（2 个交易日内披露，含公开市场买卖 code P/S）
  - 机构 13F：> $1亿 机构持仓（季度披露，延迟 45 天）

用法:
    python run/us_smartmoney.py insider NVDA          # 内部人最近交易
    python run/us_smartmoney.py insider NVDA --limit 20
    python run/us_smartmoney.py fund 1067983          # 伯克希尔 13F 列表
    python run/us_smartmoney.py fund --name berkshire
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKER_CACHE = os.path.join(ROOT_DIR, "data", "sec_tickers.json")

HEADERS = {"User-Agent": "investment-research/1.0 (research; contact user@example.com)",
           "Accept-Encoding": "gzip, deflate"}

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# 超级投资者 CIK（13F）
SUPERINVESTORS = {
    "berkshire": 1067983, "buffett": 1067983,
    "pershing": 1336528, "ackman": 1336528,
    "scion": 1649339, "burry": 1649339,
    "bridgewater": 1350694, "dalio": 1350694,
    "ark": 1697748, "wood": 1697748,
    "greenlight": 1079114, "icahn": 921669,
    "duquesne": 1009280, "tepper": 1009280,
    "baupost": 1061768, "klarman": 1061768,
}

TRANS_CODE = {"P": "公开市场买入", "S": "公开市场卖出", "A": "授予", "M": "期权行权",
              "G": "赠与", "F": "缴税", "D": "处置", "C": "转换", "X": "行权"}


def _sec_get(url: str, retries: int = 4) -> Optional[requests.Response]:
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except Exception as e:
            log.debug("sec get fail %s: %s", url, str(e)[:60])
        time.sleep(0.8 * (i + 1))
    return None


def ticker_to_cik(ticker: str) -> Optional[tuple[int, str]]:
    data = None
    if os.path.exists(TICKER_CACHE):
        try:
            data = json.load(open(TICKER_CACHE, encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        r = _sec_get("https://www.sec.gov/files/company_tickers.json")
        if not r:
            return None
        data = r.json()
        try:
            os.makedirs(os.path.dirname(TICKER_CACHE), exist_ok=True)
            json.dump(data, open(TICKER_CACHE, "w", encoding="utf-8"))
        except Exception:
            pass
    t = ticker.upper()
    for _, v in data.items():
        if str(v.get("ticker", "")).upper() == t:
            return int(v["cik_str"]), v.get("title", "")
    return None


def _recent_filings(cik: int, form: str, limit: int = 20) -> list[tuple[str, str]]:
    r = _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if r:
        try:
            recent = (r.json().get("filings") or {}).get("recent") or {}
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accs = recent.get("accessionNumber", [])
            out = []
            for i, f in enumerate(forms):
                if f == form:
                    out.append((dates[i], accs[i]))
                if len(out) >= limit:
                    break
            if out:
                return out
        except Exception:
            pass
    # 兜底：browse-edgar atom feed
    return _recent_filings_atom(cik, form, limit)


def _recent_filings_atom(cik: int, form: str, limit: int = 20) -> list[tuple[str, str]]:
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={cik:010d}&type={form}&dateb=&owner=include&count=40&output=atom")
    r = _sec_get(url)
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in root.findall("a:entry", ns):
        updated = entry.findtext("a:updated", default="", namespaces=ns)
        link = ""
        for ln in entry.findall("a:link", ns):
            link = ln.get("href", "")
        if not link:
            continue
        import re
        m = re.search(r"/(\d{10}-\d{2}-\d{6})", link)
        acc = m.group(1) if m else ""
        out.append((updated[:10], acc))
        if len(out) >= limit:
            break
    return out


def _form4_url(cik: int, accession: str) -> Optional[str]:
    acc = accession.replace("-", "")
    idx = _sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json")
    if idx:
        try:
            items = idx.json().get("directory", {}).get("item", [])
            for it in items:
                name = it.get("name", "")
                if name.endswith(".xml") and "primary" in name.lower() or name.endswith(".xml"):
                    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}"
        except Exception:
            pass
    return None


def _parse_form4(url: str) -> list[dict]:
    r = _sec_get(url)
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []

    def txt(node, path):
        el = node.find(path)
        return el.text.strip() if el is not None and el.text else ""

    owner = ""
    for el in root.iter():
        if el.tag.endswith("rptOwnerName"):
            owner = (el.text or "").strip()
            break
    is_director = any(el.tag.endswith("isDirector") and (el.text or "").strip() in ("1", "true") for el in root.iter())
    is_officer = any(el.tag.endswith("isOfficer") and (el.text or "").strip() in ("1", "true") for el in root.iter())
    title = ""
    for el in root.iter():
        if el.tag.endswith("officerTitle") and el.text:
            title = el.text.strip()
            break
    role = "董事" if is_director else ("高管" + (f"({title})" if title else "") if is_officer else "股东")

    rows = []
    for txn in root.iter():
        if not txn.tag.endswith("nonDerivativeTransaction"):
            continue
        code = ""
        for el in txn.iter():
            if el.tag.endswith("transactionCode") and el.text:
                code = el.text.strip()
        shares = price = None
        for el in txn.iter():
            if el.tag.endswith("transactionShares"):
                v = el.find("{*}value")
                if v is None:
                    v = el.find("value")
                if v is not None and v.text:
                    shares = float(v.text)
            if el.tag.endswith("transactionPricePerShare"):
                v = el.find("{*}value")
                if v is None:
                    v = el.find("value")
                if v is not None and v.text:
                    price = float(v.text)
        date = ""
        for el in txn.iter():
            if el.tag.endswith("transactionDate"):
                v = el.find("{*}value") or el.find("value")
                if v is not None and v.text:
                    date = v.text.strip()
        if shares:
            rows.append({"owner": owner, "role": role, "code": code,
                         "action": TRANS_CODE.get(code, code), "shares": shares,
                         "price": price, "date": date})
    return rows


def insider(ticker: str, limit: int = 15):
    res = ticker_to_cik(ticker)
    if not res:
        print(f"找不到 {ticker} 的 CIK")
        return
    cik, title = res
    print(f"\n{'='*94}\n  {ticker.upper()} ({title})  内部人交易 Form 4  (CIK {cik})\n{'='*94}")
    filings = _recent_filings(cik, "4", limit=limit)
    if not filings:
        print("  未取到 Form 4（SEC 接口可能限流，稍后重试或检查网络）")
        return
    print(f"  最近 {len(filings)} 份 Form 4：")
    allrows = []
    for date, acc in filings[:limit]:
        url = _form4_url(cik, acc)
        rows = _parse_form4(url) if url else []
        for r in rows:
            r["filing_date"] = date
            allrows.append(r)
    # 重点：公开市场买卖
    buys = [r for r in allrows if r["code"] == "P"]
    sells = [r for r in allrows if r["code"] == "S"]
    print(f"\n  【公开市场买入 P】{len(buys)} 笔")
    for r in buys[:10]:
        val = f"${r['shares']*r['price']:,.0f}" if r.get("price") else "?"
        print(f"    {r['date']}  {r['owner'][:24]:<24} {r['role'][:10]:<10} {r['shares']:,.0f}股 @ {r['price']}  价值 {val}")
    print(f"\n  【公开市场卖出 S】{len(sells)} 笔")
    for r in sells[:10]:
        val = f"${r['shares']*r['price']:,.0f}" if r.get("price") else "?"
        print(f"    {r['date']}  {r['owner'][:24]:<24} {r['role'][:10]:<10} {r['shares']:,.0f}股 @ {r['price']}  价值 {val}")
    print(f"\n  其他（授予/行权/缴税等）{len(allrows)-len(buys)-len(sells)} 笔")
    print("  提示：Form 4 是内部人买卖，'P'(公开买入) 信号最强；单笔大额买入比卖出更有信息量。")


def fund(cik: Optional[int], name: Optional[str], limit: int = 12):
    if name:
        cik = SUPERINVESTORS.get(name.lower())
        if not cik:
            print(f"未收录 {name}，可用 CIK 直接查，或用: {', '.join(sorted(set(SUPERINVESTORS)))}")
            return
    if not cik:
        print("请提供 --name 或 --cik")
        return
    print(f"\n{'='*80}\n  机构 13F-HR 列表  (CIK {cik})\n{'='*80}")
    filings = _recent_filings(cik, "13F-HR", limit=limit)
    if not filings:
        print("  未取到 13F（SEC 接口限流或该 CIK 无 13F）")
        return
    for date, acc in filings:
        acc_d = acc.replace("-", "")
        link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_d}/"
        print(f"  {date}  13F-HR  {link}")
    print("\n  注：13F 季度披露、延迟 45 天、仅多头；持仓明细在 filing 里的 information table XML。")
    print("  想直接看组合：Dataroma(dataroma.com) / WhaleWisdom 更直观（本环境偶发不可达）。")


def main():
    ap = argparse.ArgumentParser(description="美股聪明钱：内部人 Form 4 / 机构 13F")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("insider"); p1.add_argument("ticker"); p1.add_argument("--limit", type=int, default=15)
    p2 = sub.add_parser("fund"); p2.add_argument("--name", default=None); p2.add_argument("--cik", type=int, default=None)
    p2.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()
    if args.cmd == "insider":
        insider(args.ticker, args.limit)
    else:
        fund(args.cik, args.name, args.limit)


if __name__ == "__main__":
    main()
