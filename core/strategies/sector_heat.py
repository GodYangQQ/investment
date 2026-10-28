#!/usr/bin/env python3
"""
半导体细分赛道景气度（SOXX 池）
==============================
用法:
    python core/sector_heat.py
    python core/sector_heat.py GPU/AI加速
    python core/sector_heat.py --stock NVDA
    python core/sector_heat.py --json output/pool_scores.json
"""

import json
import logging
import sys
import time
from collections import defaultdict
from typing import Optional

import numpy as np

log = logging.getLogger("sector_heat")

from core.filters.market_filter import get_market, resolve_industry, ALL_MARKETS, TICKER_INDUSTRY
from core.datasets.universe import get_soxx_universe, get_soxx_tickers
from core.datasets.us_data import fetch_batch_quotes, fetch_daily_kline


def _build_sector_stocks(live: bool = False) -> dict[str, list[str]]:
    sectors = defaultdict(list)
    for s in get_soxx_universe(top_n=20, live=live):
        ind = get_market(s["code"]) or "其他"
        sectors[ind].append(s["code"])
    return dict(sectors)


SECTOR_STOCKS = _build_sector_stocks(live=False)
CODE_SECTOR_MAP: dict[str, str] = dict(TICKER_INDUSTRY)
SECTOR_ALIAS = {
    "gpu": "GPU/AI加速", "ai": "GPU/AI加速", "nvidia": "GPU/AI加速",
    "memory": "存储", "dram": "存储", "nand": "存储",
    "equip": "设备", "设备": "设备",
    "foundry": "代工", "tsmc": "代工", "代工": "代工",
    "analog": "模拟/MCU", "mcu": "模拟/MCU",
    "connect": "连接/高速接口", "连接": "连接/高速接口",
    "power": "电源管理", "电源": "电源管理",
    "qcom": "移动SoC/无线", "无线": "移动SoC/无线",
    "intel": "CPU/逻辑", "cpu": "CPU/逻辑",
    "存储": "存储", "gpu/ai加速": "GPU/AI加速",
}


def _fetch_tencent_quotes(codes: list[str], timeout: int = 15) -> list[dict]:
    """兼容旧函数名：批量行情。"""
    quotes = fetch_batch_quotes(codes)
    results = []
    for c in codes:
        q = quotes.get(c) or quotes.get(str(c).upper())
        if not q:
            continue
        results.append({
            "code": q.get("code", c),
            "name": q.get("name", c),
            "price": q.get("price", 0),
            "pct_change": q.get("pct_change", 0),
            "pe_ttm": q.get("pe_ttm", 0),
            "volume": q.get("volume", 0),
        })
    return results


_KL_CACHE: dict[str, "object"] = {}


def _fetch_kline_pcts(codes: list[str], days: int = 5) -> dict[str, float]:
    results = {}
    for code in codes:
        try:
            df = _KL_CACHE.get(code)
            if df is None:
                df = fetch_daily_kline(code, days=35)
                _KL_CACHE[code] = df
            if df is None or len(df) < days + 1:
                continue
            start = float(df["close"].iloc[-(days + 1)])
            end = float(df["close"].iloc[-1])
            results[code] = round((end - start) / start * 100, 2) if start else 0.0
        except Exception:
            continue
    return results


def resolve_sector(name: str, code: str = "") -> str:
    return resolve_industry(name, code)


def get_sector_stocks(sector: str) -> list[str]:
    stocks = SECTOR_STOCKS.get(sector)
    if stocks:
        return stocks
    # 动态刷新一次
    rebuilt = _build_sector_stocks(live=False)
    return rebuilt.get(sector, [])


def list_sectors() -> list[str]:
    return list(SECTOR_STOCKS.keys()) or ALL_MARKETS


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
    try:
        quotes = _fetch_tencent_quotes([stock_code])
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
