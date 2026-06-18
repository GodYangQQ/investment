#!/usr/bin/env python3
"""
板块景气度评估模块（sector_heat.py）

通过腾讯行情API获取个股实时数据，聚合计算板块景气度。

用法:
    python core/sector_heat.py                           # 全板块排名
    python core/sector_heat.py 半导体                     # 指定板块
    python core/sector_heat.py --stock 603893             # 查某只股票所属板块景气度
    python core/sector_heat.py --json output/pool_scores.json  # 从量化评分JSON聚合
"""

import json
import logging
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import requests
import re

log = logging.getLogger("sector_heat")

# ============================================================================
# 腾讯行情API（已验证可用）
# ============================================================================

TENCENT_BATCH_URL = "https://qt.gtimg.cn/q="


def _fetch_tencent_quotes(codes: list[str], timeout: int = 15) -> list[dict]:
    """批量获取腾讯实时行情。"""
    results = []
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        prefix = ""
        for c in batch:
            if c.startswith(("60", "68")):
                prefix += f"sh{c},"
            else:
                prefix += f"sz{c},"
        url = f"{TENCENT_BATCH_URL}{prefix.rstrip(',')}"
        try:
            r = requests.get(url, timeout=timeout)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                m = re.search(r'="(.+?)"', line)
                if not m:
                    continue
                f = m.group(1).split("~")
                if len(f) < 45:
                    continue
                code_raw = f[2]
                code = code_raw if code_raw else ""
                results.append({
                    "code": code,
                    "name": f[1],
                    "price": float(f[3]) if f[3] else 0,
                    "pct_change": float(f[32]) if f[32] else 0,
                    "pe_ttm": float(f[39]) if f[39] else 0,
                    "volume": float(f[6]) if f[6] else 0,
                })
        except Exception as e:
            log.warning(f"获取行情失败: {e}")
            continue
    return results


def _fetch_kline_pcts(codes: list[str], days: int = 5) -> dict[str, float]:
    """批量获取N日涨跌幅（通过腾讯K线API）。"""
    results = {}
    for code in codes:
        prefix = "sh" if code.startswith(("60", "68")) else "sz"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{days+5},qfq"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            klines = data.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday", [])
            if not klines:
                klines = data.get("data", {}).get(f"{prefix}{code}", {}).get("day", [])
            if klines and len(klines) >= days + 1:
                start_close = float(klines[-(days+1)][2])
                end_close = float(klines[-1][2])
                pct = (end_close - start_close) / start_close * 100 if start_close > 0 else 0
                results[code] = round(pct, 2)
        except Exception:
            continue
    return results


# ============================================================================
# 股票→板块映射表（代码前缀 + 名称关键词）
# ============================================================================

# 板块代表性股票（每个板块10-20只代表股）
SECTOR_STOCKS = {
    "半导体": [
        "603893", "688981", "688012", "002049", "603986", "600703",
        "688256", "688041", "688047", "688072", "002185", "600584",
        "300373", "688608", "603501", "002371", "688019", "300623",
        "688249", "603005",
    ],
    "PCB/元件": [
        "002463", "600183", "603228", "002916", "002384", "300476",
        "002938", "300782", "603920", "688183",
    ],
    "光模块/通信": [
        "300308", "300502", "002281", "300394", "300570", "688498",
        "603083", "300620", "002902", "300548",
    ],
    "消费电子": [
        "002475", "002241", "601138", "002600", "300433", "002456",
        "300115", "002273", "300136", "002681",
    ],
    "军工": [
        "600760", "600893", "002013", "600391", "000768", "600316",
        "002023", "600038", "300034", "300424",
    ],
    "化工": [
        "600309", "600426", "600989", "002064", "603599", "002408",
        "600160", "002493", "000830", "002092",
        "605589", "601208", "300481", "002915", "603002", "300236",
    ],
    "稀土/有色": [
        "600111", "000831", "002600", "000970", "002378", "601899",
        "600549", "603993", "002155", "000975",
    ],
    "航运港口": [
        "601919", "601872", "600026", "002320", "600428", "601598",
        "600018", "601975", "603871", "601881",
    ],
    "新能源/电力": [
        "600900", "601985", "000591", "601012", "600438", "002129",
        "601865", "600674", "002202", "301155",
    ],
    "锂电": [
        "002466", "002460", "002407", "300750", "300014", "002709",
        "600884", "300073", "002074", "300390",
    ],
    "光伏": [
        "601012", "600438", "002129", "688599", "300274", "002459",
        "688223", "300118", "002610", "300763",
    ],
    "医药": [
        "600276", "300347", "300760", "603259", "600085", "002821",
        "300759", "688180", "603456", "002007",
    ],
    "汽车零部件": [
        "601689", "002920", "603596", "300496", "300750", "002906",
        "002050", "603786", "002085", "600741",
    ],
    "软件/AI应用": [
        "600570", "300033", "300253", "300624", "688111", "300377",
        "300454", "002230", "600588", "688088",
    ],
    "液冷/散热": [
        "002837", "300499", "300990", "301310", "002126", "300124",
        "002255", "603191", "301128",
    ],
    "电网设备": [
        "601567", "600406", "600869", "002028", "300124", "600580",
        "300750", "002121", "601877", "002339",
    ],
    "白酒": [
        "600519", "000858", "000568", "002304", "000596", "600809",
        "600702", "603369", "600559", "000799",
    ],
}

# 反向索引：股票代码 → 板块
CODE_SECTOR_MAP: dict[str, str] = {}
for _sector, _codes in SECTOR_STOCKS.items():
    for _c in _codes:
        CODE_SECTOR_MAP[_c] = _sector

# 板块名称关键词 → 板块标准名
SECTOR_ALIAS = {
    "半导体": "半导体", "芯片": "半导体", "集成电路": "半导体",
    "pcb": "PCB/元件", "元件": "PCB/元件", "印制电路板": "PCB/元件",
    "光模块": "光模块/通信", "光通信": "光模块/通信", "通信": "光模块/通信",
    "消费电子": "消费电子", "苹果": "消费电子",
    "军工": "军工", "国防": "军工", "航发": "军工", "船舶": "军工",
    "化工": "化工", "化学": "化工", "制冷剂": "化工", "新材料": "化工",
    "稀土": "稀土/有色", "有色": "稀土/有色", "黄金": "稀土/有色",
    "航运": "航运港口", "港口": "航运港口", "油运": "航运港口",
    "电力": "新能源/电力", "核电": "新能源/电力", "新能源": "新能源/电力",
    "锂电": "锂电", "锂": "锂电", "能源金属": "锂电",
    "光伏": "光伏", "太阳能": "光伏",
    "医药": "医药", "生物": "医药", "cro": "医药",
    "汽车": "汽车零部件", "新能源车": "汽车零部件",
    "软件": "软件/AI应用", "ai": "软件/AI应用", "算力": "软件/AI应用",
    "液冷": "液冷/散热", "散热": "液冷/散热",
    "电网": "电网设备", "电气": "电网设备",
    "白酒": "白酒", "酒": "白酒", "食品饮料": "白酒",
}


def resolve_sector(name: str, code: str = "") -> str:
    """根据名称或代码查找所属板块。"""
    # 先查硬编码映射
    if code and code in CODE_SECTOR_MAP:
        return CODE_SECTOR_MAP[code]
    # 关键词匹配
    combined = name.lower()
    for kw, sector in SECTOR_ALIAS.items():
        if kw in combined:
            return sector
    return "其他"


def get_sector_stocks(sector: str) -> list[str]:
    """获取板块的代表性股票列表。"""
    return SECTOR_STOCKS.get(sector, [])


def list_sectors() -> list[str]:
    """列出所有支持的板块。"""
    return list(SECTOR_STOCKS.keys())


# ============================================================================
# 景气度计算引擎
# ============================================================================

def calc_sector_heat(sector: str) -> dict:
    """
    计算板块景气度（0-100），基于实时行情数据 + 多时间框架趋势。

    七维评估（原六维 + 趋势方向）：
    - 板块5日涨幅 (20%)
    - 板块14日涨幅 (两周) (15%)
    - 板块30日涨幅 (10%)
    - 强势股占比 (20%)
    - 领涨龙头 (15%)
    - RSI健康度 (10%)
    - PE合理性 (10%)
    - 景气度趋势方向 (-5 ~ +5 加速/减速修正)
    """
    codes = get_sector_stocks(sector)
    if not codes:
        return {
            "sector": sector, "score": 0, "level": "无数据", "label": "板块无数据",
            "details": {}, "top_stocks": [], "summary": "板块无数据",
            "trend_signal": "", "multi_tf": {},
        }

    quotes = _fetch_tencent_quotes(codes)
    if not quotes:
        return {
            "sector": sector, "score": 0, "level": "无数据", "label": "行情获取失败",
            "details": {}, "top_stocks": [], "summary": "无法获取板块行情",
            "trend_signal": "", "multi_tf": {},
        }

    n = len(quotes)
    details = {}

    # --- 获取多时间框架涨跌幅 ---
    kline_pcts_5 = _fetch_kline_pcts(codes, days=5)
    kline_pcts_10 = _fetch_kline_pcts(codes, days=10)
    kline_pcts_14 = _fetch_kline_pcts(codes, days=14)
    kline_pcts_20 = _fetch_kline_pcts(codes, days=20)
    kline_pcts_30 = _fetch_kline_pcts(codes, days=30)

    def avg_pct(pcts): return round(np.mean([v for v in pcts.values() if v != 0]), 2) if pcts else 0

    avg_5d = avg_pct(kline_pcts_5)
    avg_10d = avg_pct(kline_pcts_10)
    avg_14d = avg_pct(kline_pcts_14)
    avg_20d = avg_pct(kline_pcts_20)
    avg_30d = avg_pct(kline_pcts_30)

    multi_tf = {
        "5日": avg_5d, "10日": avg_10d, "14日(两周)": avg_14d,
        "20日": avg_20d, "30日": avg_30d,
    }

    # --- 1. 板块5日涨幅 (20%) ---
    momentum_score = _pct_to_score(avg_5d)
    details["5日涨幅"] = {"value": f"{avg_5d:+.1f}%", "score": momentum_score, "weight": 20}

    # --- 2. 板块14日涨幅(两周) (15%) ---
    ts_14 = _pct_to_score(avg_14d, thresholds=[10, 5, 2, 0])
    details["14日涨幅(两周)"] = {"value": f"{avg_14d:+.1f}%", "score": ts_14, "weight": 15}

    # --- 3. 板块30日涨幅 (10%) ---
    ts_30 = _pct_to_score(avg_30d, thresholds=[15, 8, 3, 0])
    details["30日涨幅"] = {"value": f"{avg_30d:+.1f}%", "score": ts_30, "weight": 10}

    # --- 4. 强势股占比 (20%) ---
    strong_count = 0
    for q in quotes:
        code = q["code"]
        pct = q["pct_change"]
        pct_5d = kline_pcts_5.get(code, 0)
        if pct > 0 or pct_5d > 2:
            strong_count += 1
    strong_pct = strong_count / n * 100 if n > 0 else 0
    flow_score = _threshold_score(strong_pct, [50, 30, 15, 5])
    details["强势股占比"] = {"value": f"{strong_pct:.0f}%({strong_count}/{n})", "score": flow_score, "weight": 20}

    # --- 5. 领涨龙头 (15%) ---
    leader_count = sum(1 for _, pct in kline_pcts_14.items() if pct > 10)
    if leader_count >= 3:
        leader_score = 5
    elif leader_count >= 1:
        leader_score = 4
    else:
        mild = sum(1 for _, pct in kline_pcts_14.items() if pct > 5)
        leader_score = 3 if mild >= 1 else 1
    details["领涨龙头(14日)"] = {"value": f"{leader_count}只涨幅>10%", "score": leader_score, "weight": 15}

    # --- 6. RSI健康度 (10%) ---
    daily_changes = [abs(q["pct_change"]) for q in quotes if q["pct_change"] != 0]
    avg_abs_chg = np.mean(daily_changes) if daily_changes else 0
    rsi_score = _threshold_score(avg_abs_chg, [3, 5, 7, 10], reverse=True)
    details["RSI健康度"] = {"value": f"avg_abs_chg={avg_abs_chg:.1f}%", "score": rsi_score, "weight": 10}

    # --- 7. PE合理性 (10%) ---
    pes = [q["pe_ttm"] for q in quotes if 0 < q["pe_ttm"] < 500]
    avg_pe = np.mean(pes) if pes else 100
    pe_score = _threshold_score(avg_pe, [25, 40, 80, 150], reverse=True)
    details["PE合理性"] = {"value": f"avg PE={avg_pe:.1f}", "score": pe_score, "weight": 10}

    # --- 总分 ---
    total_score = sum(d["score"] / 5 * d["weight"] for d in details.values())
    total_score = round(total_score, 1)

    # --- 景气度趋势修正：比较不同时间窗口的涨跌幅方向 ---
    trend_adjustment = 0
    trend_reasons = []
    # 短期 vs 中期：5日 vs 14日
    if avg_5d > 0 and avg_14d > 0 and avg_5d > avg_14d:
        trend_adjustment += 3
        trend_reasons.append("5日加速>14日 → 热度上升中")
    elif avg_5d > 0 > avg_14d:
        trend_adjustment += 5
        trend_reasons.append("短正长负 → 刚启动反转")
    elif avg_14d > 0 and avg_5d < avg_14d * 0.5 and avg_5d > 0:
        trend_adjustment -= 4
        trend_reasons.append(f"5日({avg_5d:.1f}%)远弱于14日({avg_14d:.1f}%) → 热度快速冷却(警惕)")
    elif avg_14d > 0 > avg_5d:
        trend_adjustment -= 5
        trend_reasons.append("短负长正 → 热度快速冷却(危险)")
    elif avg_5d < 0 and avg_14d < 0 and avg_5d < avg_14d:
        trend_adjustment -= 3
        trend_reasons.append("5日加速下跌>14日 → 降温加速")
    # 中期 vs 长期：14日 vs 30日
    if avg_14d > 0 and avg_30d > 0 and avg_14d > avg_30d:
        trend_adjustment += 2
        trend_reasons.append("14日加速>30日 → 中期升温确认")
    elif avg_14d < 0 and avg_30d < 0 and avg_14d < avg_30d:
        trend_adjustment -= 2
        trend_reasons.append("14日跌幅扩大>30日 → 中期降温确认")
    # 30日仍为负但5日已转正 → 底部反转信号
    if avg_30d < 0 and avg_5d > 0 and avg_14d > 0:
        trend_adjustment += 3
        trend_reasons.append("30日仍负但5/14日已转正 → 底部V型反转")

    trend_adjustment = max(-5, min(5, trend_adjustment))
    final_score = max(0, min(100, total_score + trend_adjustment))

    # 趋势信号文字
    if trend_adjustment >= 4:
        trend_signal = "加速上行"
    elif trend_adjustment >= 1:
        trend_signal = "温和回升"
    elif trend_adjustment <= -4:
        trend_signal = "加速下行"
    elif trend_adjustment <= -1:
        trend_signal = "温和降温"
    else:
        trend_signal = "横盘/方向不明"

    # --- 分级 ---
    if final_score >= 75:
        level, label = "高景气", "高景气"
    elif final_score >= 60:
        level, label = "中等景气", "中等景气"
    elif final_score >= 40:
        level, label = "低景气", "低景气"
    else:
        level, label = "极低景气", "极低景气"

    # --- 板块最强股 ---
    top_stocks = sorted(quotes, key=lambda x: x["pct_change"], reverse=True)[:5]
    top_list = [(s["code"], s["name"], s["pct_change"]) for s in top_stocks]

    # --- 总结 ---
    if final_score >= 75:
        summary = f"板块热度高，{strong_count}只强势股，{trend_signal}，可正常仓位操作"
    elif final_score >= 60:
        summary = f"板块温和偏热，{trend_signal}，建议半仓操作"
    elif final_score >= 40:
        summary = f"板块偏冷，{trend_signal}，建议观察不买入"
    else:
        summary = f"板块极冷，{trend_signal}，一票否决"

    return {
        "sector": sector,
        "score": round(final_score, 1),
        "base_score": round(total_score, 1),
        "trend_adjustment": trend_adjustment,
        "trend_signal": trend_signal,
        "trend_reasons": trend_reasons,
        "level": level, "label": label,
        "details": details,
        "multi_tf": multi_tf,
        "top_stocks": top_list,
        "summary": summary,
        "stock_count": n,
        "avg_5d": avg_5d,
        "avg_14d": avg_14d,
        "avg_30d": avg_30d,
        "avg_pe": round(avg_pe, 1),
    }


def _pct_to_score(pct: float, thresholds: list = None) -> int:
    """涨跌幅 → 1-5分"""
    if thresholds is None:
        thresholds = [10, 5, 2, 0]
    if pct > thresholds[0]: return 5
    if pct > thresholds[1]: return 4
    if pct > thresholds[2]: return 3
    if pct > thresholds[3]: return 2
    return 1


def _threshold_score(value: float, thresholds: list, reverse: bool = False) -> int:
    """按阈值打分，reverse=True时越小分越高"""
    if reverse:
        thresholds = list(reversed(thresholds))
        scores = [5, 4, 3, 2, 1]
    else:
        scores = [5, 4, 3, 2, 1]
    for i, t in enumerate(thresholds):
        if reverse:
            if value < t: return scores[i]
        else:
            if value > t: return scores[i]
    return 1


def calc_sector_heat_from_json(scores_json_path: str) -> dict:
    """从量化评分JSON中计算各板块景气度。"""
    with open(scores_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    stocks = data if isinstance(data, list) else data.get("scores", [])
    if not stocks:
        log.warning("JSON中无股票数据")
        return {}

    sectors = defaultdict(list)
    for s in stocks:
        code = s.get("code", "")
        name = s.get("name", "")
        sig = s.get("signals", {})
        q = s.get("quote", {})
        sc = s.get("score", {})

        sector = resolve_sector(name, code)
        sectors[sector].append({
            "code": code,
            "name": name,
            "price": q.get("price", 0),
            "pe_ttm": q.get("pe_ttm", 0),
            "pct_5d": sig.get("pct_5d", 0),
            "pct_20d": sig.get("pct_20d", 0),
            "rsi14": sig.get("rsi14", 50),
            "total_score": sc.get("total", 0),
            "vol_ratio": sig.get("vol_ratio", 0),
        })

    results = {}
    for sector, stock_list in sectors.items():
        if len(stock_list) < 2:
            continue
        n = len(stock_list)
        details = {}

        avg_5d = np.mean([s.get("pct_5d", 0) for s in stock_list])
        avg_20d = np.mean([s.get("pct_20d", 0) for s in stock_list])

        if avg_5d > 10:
            ms = 5
        elif avg_5d > 5:
            ms = 4
        elif avg_5d > 2:
            ms = 3
        elif avg_5d > 0:
            ms = 2
        else:
            ms = 1
        details["5日涨幅"] = {"value": f"{avg_5d:+.1f}%", "score": ms, "weight": 25}

        if avg_20d > 10:
            ts = 5
        elif avg_20d > 5:
            ts = 3
        elif avg_20d > 0:
            ts = 2
        else:
            ts = 1
        details["20日动量"] = {"value": f"{avg_20d:+.1f}%", "score": ts, "weight": 5}

        strong_count = sum(1 for s in stock_list
                          if s.get("total_score", 0) >= 68
                          and s.get("pct_5d", 0) > 0)
        strong_pct = strong_count / n * 100
        if strong_pct > 50:
            fs = 5
        elif strong_pct > 30:
            fs = 4
        elif strong_pct > 15:
            fs = 3
        elif strong_pct > 5:
            fs = 2
        else:
            fs = 1
        details["强势股占比"] = {"value": f"{strong_pct:.0f}%({strong_count}/{n})", "score": fs, "weight": 25}

        leaders = [s for s in stock_list
                   if s.get("pct_5d", 0) > 10
                   and s.get("vol_ratio", 0) > 1.2]
        leader_count = len(leaders)
        if leader_count >= 3:
            ls = 5
        elif leader_count >= 1:
            ls = 4
        else:
            mild = [s for s in stock_list if s.get("pct_5d", 0) > 5]
            ls = 3 if len(mild) >= 1 else 1
        details["领涨龙头"] = {"value": f"{leader_count}只涨幅>10%", "score": ls, "weight": 15}

        rsis = [s.get("rsi14", 50) for s in stock_list]
        avg_rsi = np.mean(rsis)
        if 45 <= avg_rsi <= 60:
            rs = 5
        elif 40 <= avg_rsi <= 70:
            rs = 4
        elif 30 <= avg_rsi <= 80:
            rs = 3
        else:
            rs = 1
        details["RSI健康度"] = {"value": f"avg RSI={avg_rsi:.1f}", "score": rs, "weight": 10}

        pes = [s.get("pe_ttm", 0) for s in stock_list if 0 < s.get("pe_ttm", 0) < 500]
        avg_pe = np.mean(pes) if pes else 100
        if avg_pe < 25:
            ps = 5
        elif avg_pe < 40:
            ps = 4
        elif avg_pe < 80:
            ps = 3
        elif avg_pe < 150:
            ps = 2
        else:
            ps = 1
        details["PE合理性"] = {"value": f"avg PE={avg_pe:.1f}", "score": ps, "weight": 20}

        total = sum(d["score"] / 5 * d["weight"] for d in details.values())
        total = round(total, 1)

        if total >= 75:
            level, label = "高景气", "高景气"
        elif total >= 60:
            level, label = "中等景气", "中等景气"
        elif total >= 40:
            level, label = "低景气", "低景气"
        else:
            level, label = "极低景气", "极低景气"

        top = sorted(stock_list, key=lambda x: x.get("total_score", 0), reverse=True)[:5]
        results[sector] = {
            "sector": sector,
            "score": total,
            "level": level,
            "label": label,
            "details": details,
            "top_stocks": [(s["code"], s["name"], s["total_score"]) for s in top],
            "summary": "",
        }

    return dict(sorted(results.items(), key=lambda x: x[1]["score"], reverse=True))


def find_stock_sector(stock_code: str) -> str:
    """根据股票代码查找所属板块。"""
    if stock_code in CODE_SECTOR_MAP:
        return CODE_SECTOR_MAP[stock_code]
    # 尝试从名称匹配
    try:
        codes = [stock_code]
        quotes = _fetch_tencent_quotes(codes)
        if quotes:
            return resolve_sector(quotes[0]["name"], stock_code)
    except Exception:
        pass
    return "其他"


# ============================================================================
# 输出格式化
# ============================================================================

def format_heat_output(heat: dict) -> str:
    """格式化板块景气度输出（含多时间框架趋势）。"""
    lines = []
    sector = heat.get("sector", "?")
    score = heat.get("score", 0)
    level = heat.get("level", "?")
    label = heat.get("label", "?")
    base_score = heat.get("base_score", score)
    trend_adj = heat.get("trend_adjustment", 0)
    trend_signal = heat.get("trend_signal", "")

    if score >= 75:
        status_mark = "[高景气]"
    elif score >= 60:
        status_mark = "[中等景气]"
    elif score >= 40:
        status_mark = "[低景气]"
    else:
        status_mark = "[极低景气]"

    adj_str = f"({trend_adj:+d})" if trend_adj != 0 else ""
    lines.append(f"## 板块景气度 — {sector}  {status_mark}")
    lines.append(f"> 景气度总分: {score:.1f}/100 | 基础分: {base_score:.1f} | 趋势修正: {adj_str} | 状态: {label}")
    if trend_signal:
        lines.append(f"> 趋势信号: {trend_signal}")
    lines.append(f"> {heat.get('summary', '')}")
    lines.append("")

    # 多时间框架趋势
    multi_tf = heat.get("multi_tf", {})
    if multi_tf:
        lines.append("### 多时间框架涨跌幅趋势")
        lines.append(f"{'时间窗口':<16} {'板块均涨幅':<12} {'趋势'}")
        lines.append("-" * 45)
        prev_val = None
        for tf_name, tf_val in multi_tf.items():
            direction = ""
            if prev_val is not None:
                if tf_val > prev_val:
                    direction = "加速"
                elif tf_val < prev_val:
                    direction = "减速"
                else:
                    direction = "持平"
            prev_val = tf_val
            lines.append(f"{tf_name:<16} {tf_val:+.1f}%{'':>8} {direction}")
        lines.append("")

    # 趋势修正明细
    trend_reasons = heat.get("trend_reasons", [])
    if trend_reasons:
        lines.append("### 趋势修正明细")
        for r in trend_reasons:
            lines.append(f"  -> {r}")
        lines.append("")

    details = heat.get("details", {})
    if details:
        lines.append("### 七维评分明细")
        lines.append(f"{'指标':<16} {'得分':<5} {'权重':<6} {'数值'}")
        lines.append("-" * 55)
        for key, d in details.items():
            bars = "#" * int(d["score"]) + "-" * (5 - int(d["score"]))
            lines.append(f"{key:<16} [{bars}] {d['weight']}%    {d['value']}")
        lines.append("")

    if heat.get("avg_pe") is not None:
        lines.append(f"- 板块平均PE: {heat['avg_pe']:.1f} | 样本股数: {heat.get('stock_count', '?')}")
        lines.append("")

    top_stocks = heat.get("top_stocks", [])
    if top_stocks:
        lines.append("### 板块最强个股")
        for code, name, val in top_stocks:
            lines.append(f"  {code} {name}  {val:+.1f}%")

    lines.append("")
    lines.append("### 入场决策")
    if score >= 75:
        lines.append("-> 正常仓位买入")
    elif score >= 60:
        lines.append("-> 半仓买入")
    elif score >= 40:
        lines.append("-> 仅观察不买，等板块热起来")
    else:
        lines.append("-> [否决] 极低景气，一票否决，量化分再高也不买")

    return "\n".join(lines)


def format_ranking_table(results: dict) -> str:
    """格式化全板块排名表。"""
    lines = []
    lines.append(f"{'排名':<4} {'板块':<16} {'景气度':<7} {'状态':<10} {'强势股':<8} {'5日均涨幅':<10} {'avg PE':<9}")
    lines.append("-" * 80)

    for i, (sector, heat) in enumerate(results.items(), 1):
        d = heat.get("details", {})
        strong_val = d.get("强势股占比", {}).get("value", "?")
        momentum_val = d.get("5日涨幅", {}).get("value", "?")
        pe_val = d.get("PE合理性", {}).get("value", "?")

        lines.append(
            f"{i:<4} {sector:<16} {heat['score']:<7.1f} "
            f"{heat['level']:<10} {strong_val:<8} {momentum_val:<10} {pe_val:<9}"
        )

    return "\n".join(lines)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="板块景气度评估")
    parser.add_argument("sector", nargs="?", default=None, help="板块名称")
    parser.add_argument("--stock", default=None, help="股票代码（查所属板块景气度）")
    parser.add_argument("--json", default=None, help="从量化评分JSON聚合计算")
    parser.add_argument("--top", type=int, default=15, help="显示前N个板块")

    args = parser.parse_args()

    if args.json:
        results = calc_sector_heat_from_json(args.json)
        if args.sector:
            matched = None
            for kw, std_name in SECTOR_ALIAS.items():
                if args.sector in kw:
                    std_name2 = std_name
                    if std_name2 in results:
                        matched = std_name2
                        break
            if not matched:
                for s in results:
                    if args.sector in s:
                        matched = s
                        break
            if matched and matched in results:
                print(format_heat_output(results[matched]))
            else:
                print(f"未找到板块 '{args.sector}'，可用: {list(results.keys())}")
        else:
            top_n = dict(list(results.items())[:args.top])
            print("\n板块景气度排名 (基于JSON聚合)\n")
            print(format_ranking_table(top_n))
        sys.exit(0)

    if args.stock:
        sector = find_stock_sector(args.stock)
        print(f"[{args.stock}] 所属板块: {sector}")
        if sector == "其他":
            print("未找到所属板块，可能不在映射表中")
            sys.exit(1)
        heat = calc_sector_heat(sector)
        print(format_heat_output(heat))
        sys.exit(0)

    if args.sector:
        sector = SECTOR_ALIAS.get(args.sector, args.sector)
        if sector not in SECTOR_STOCKS:
            print(f"未找到板块 '{args.sector}'，可用板块: {list(SECTOR_STOCKS.keys())}")
            sys.exit(1)
        heat = calc_sector_heat(sector)
        print(format_heat_output(heat))
        sys.exit(0)

    # 全板块排名
    print("\n板块景气度排名 (实时行情聚合)\n")
    results = {}
    sectors = list(SECTOR_STOCKS.keys())
    for sector in sectors:
        try:
            heat = calc_sector_heat(sector)
            if heat.get("score", 0) > 0:
                results[sector] = heat
            time.sleep(0.5)  # 避免请求过快
        except Exception as e:
            log.error(f"{sector} 计算失败: {e}")

    sorted_results = dict(sorted(results.items(), key=lambda x: x[1]["score"], reverse=True))
    top_n = dict(list(sorted_results.items())[:args.top])
    print(format_ranking_table(top_n))
